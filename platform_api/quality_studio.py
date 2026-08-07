# platform_api/quality_studio.py — POST /platform/quality-studio/run
# (Quality Studio, docs/ESMI_DASHBOARD_UX.md Section 3.6).
#
# Runs a FIXED scripted scenario (quality_studio_scenarios.py) through the
# real production graph — the exact same graph.py object /chat uses,
# respecting USE_MULTI_AGENT, with the exact same per-request prompt
# assembly (agents.py's middleware reads the tenant's current SAVED
# voice_id/greeting/KB fresh on every call already, so a scenario
# automatically reflects "current draft config" — no special plumbing
# needed for that half of the requirement).
#
# Real tool/KB behavior, fake side effects: search_knowledge_base,
# get_pricing, list_available_slots, and find_booking run for real — that's
# the actual value of a practice run (does the KB really answer this,
# does availability logic really work). book_appointment, escalate_to_human,
# and the reschedule/cancel tools get `scenario_mode: True` threaded through
# config.configurable (same mechanism tenant_id already uses — see
# tools._scenario_mode_from_config), which lets their real business-rule
# checks (closed-day/closed-hours, "booking not found") run for real but
# short-circuits before any Calendar write, SendGrid send, or Twilio SMS.
#
# Isolation: each run gets a uniquely-namespaced, throwaway thread_id
# (through the same namespaced_thread() tenant-isolation helper every real
# chat thread uses) and NEVER calls platform_api.chat_log.record_chat_turn —
# so a practice run leaves a checkpointer row (harmless, same as any short-
# lived test thread) but never appears in the tenant's real Chats list.
#
# No persistence beyond the HTTP response — v1 is stateless. "Replay last
# run" is a frontend-only concept (keep the last response in React state).

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import BaseModel

import graph as _graph_module
from platform_api.chat_log import derive_outcome
from platform_api.security import require_tenant, verify_platform_secret
from quality_studio_scenarios import get_scenario
from tenants import namespaced_thread

log = logging.getLogger(__name__)

router = APIRouter()

# Must match tools.py's book_appointment_core/reschedule_appointment/
# cancel_appointment scenario_mode markers exactly — this is how a run
# distinguishes "the tool would have really booked/moved/canceled" from
# "the tool correctly refused" (e.g. after_hours' closed-hours rejection)
# without needing structured tool-result data threaded back through the
# LLM's plain-text ToolMessage content.
_PRACTICE_MARKER = "[Practice run"

_KB_TOOLS = {"search_knowledge_base", "get_pricing", "list_available_slots", "find_booking"}

_SPANISH_CHARS = set("áéíóúñ¿¡")
_SPANISH_WORDS = (
    " el ", " la ", " los ", " las ", " gracias", " hola", " está", " puedo",
    " cita", " para ", " sí ", " qué ", " cómo ", " disponible",
)


def _extract_content(content) -> str:
    """Normalise AIMessage content — handles str and list-of-blocks formats.
    Duplicated from api.py's _extract_content (small, and importing from
    api.py here would be a circular import — see rate_limit.py's docstring
    for why that pattern is fragile and avoided elsewhere in this repo)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _extract_turn(new_messages: list) -> tuple[str, set[str], list[str]]:
    """From the messages added during one graph turn, return (reply_text,
    tool_names_called, tool_result_texts)."""
    tool_names: set[str] = set()
    tool_results: list[str] = []
    reply_text = ""
    for m in new_messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name:
                    tool_names.add(str(name).lower())
            content = _extract_content(m.content)
            if content:
                reply_text = content
        elif isinstance(m, ToolMessage):
            content = _extract_content(m.content)
            if content:
                tool_results.append(content)
    return reply_text, tool_names, tool_results


def _looks_spanish(text: str) -> bool:
    lowered = f" {text.lower()} "
    if any(ch in lowered for ch in _SPANISH_CHARS):
        return True
    return any(w in lowered for w in _SPANISH_WORDS)


def _evaluate_success(
    scenario_id: str, tools_called: set[str], booked_for_real: bool, esmi_replies: list[str]
) -> bool:
    if scenario_id == "new_lead_books":
        return booked_for_real
    if scenario_id == "faq_only":
        return bool(tools_called & {"search_knowledge_base", "get_pricing"}) and not booked_for_real
    if scenario_id == "spanish_caller":
        return bool(esmi_replies) and all(_looks_spanish(t) for t in esmi_replies)
    if scenario_id == "after_hours":
        return not booked_for_real
    if scenario_id == "angry_urgent":
        return "escalate_to_human" in tools_called
    if scenario_id == "existing_client_reschedule":
        # All three security-flow steps in order: look the booking up, send/
        # verify a confirmation code, only then move it — never skip straight
        # to reschedule_appointment on the caller's say-so alone.
        return {"find_booking", "request_cancellation_code", "reschedule_appointment"} <= tools_called
    return False


# Shown appended to the run's `note` only when success is False, so an
# operator sees exactly what SHOULD have happened, not just "failed".
_SOFT_FAIL_HINTS = {
    "angry_urgent": (
        "This caller was frustrated and asked for a person — Esmi should have "
        "called escalate_to_human. Check the transcript to see why it didn't."
    ),
    "existing_client_reschedule": (
        "This caller wanted to move an existing booking — Esmi should look the "
        "booking up, send a confirmation code, and only then reschedule. Check "
        "the transcript to see which step it skipped."
    ),
}


def _note_for(scenario_id: str, success: bool) -> str:
    base = (
        "Practice run — uses your current saved voice, greeting, and knowledge "
        "base. Not a real customer call; no booking, email, or SMS was sent."
    )
    if success:
        return base
    hint = _SOFT_FAIL_HINTS.get(scenario_id)
    return f"{base} {hint}" if hint else base


class QualityStudioRunRequest(BaseModel):
    scenario_id: str


@router.post("/platform/quality-studio/run")
async def platform_quality_studio_run(body: QualityStudioRunRequest, request: Request) -> dict:
    """Run one fixed scenario end-to-end and return its full transcript +
    disposition. Async def (unlike the sync voice_sync.py/voice_preview.py
    routes) because graph.ainvoke is async — same reasoning api.py's /chat
    has for awaiting the LangGraph checkpointer.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    scenario = get_scenario(body.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=400, detail=f"Unknown scenario_id '{body.scenario_id}'.")

    g = _graph_module.graph
    if g is None:
        raise HTTPException(status_code=503, detail="Agent is not ready yet — try again shortly.")

    run_thread_id = f"quality-studio-{uuid.uuid4().hex}"
    ns_thread = namespaced_thread(tenant_id, run_thread_id)
    config = {
        "configurable": {
            "thread_id": ns_thread,
            "tenant_id": tenant_id,
            "scenario_mode": True,
        }
    }

    transcript: list[dict] = []
    all_tools: set[str] = set()
    all_tool_results: list[str] = []
    esmi_replies: list[str] = []
    started = time.monotonic()
    prev_len = 0

    try:
        for turn in scenario.turns:
            transcript.append(
                {
                    "speaker": "caller",
                    "text": turn,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tools_called": [],
                }
            )
            state = await g.ainvoke(
                {"messages": [{"role": "user", "content": turn}]},
                config=config,
                context={"tenant_id": tenant_id},
            )
            messages = state.get("messages") or []
            new_messages = messages[prev_len:]
            prev_len = len(messages)

            reply_text, tool_names, tool_results = _extract_turn(new_messages)
            all_tools |= tool_names
            all_tool_results.extend(tool_results)
            if reply_text:
                esmi_replies.append(reply_text)

            transcript.append(
                {
                    "speaker": "esmi",
                    "text": reply_text or "(no reply captured)",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tools_called": sorted(tool_names),
                }
            )
    except Exception:
        log.exception(
            "Quality Studio scenario run failed: tenant=%s scenario=%s", tenant_id, scenario.id
        )
        raise HTTPException(status_code=502, detail="The practice run failed unexpectedly — try again.")

    duration_ms = int((time.monotonic() - started) * 1000)
    booked_for_real = any(_PRACTICE_MARKER in r for r in all_tool_results)
    disposition = derive_outcome(all_tools) or ("info" if all_tools & _KB_TOOLS else "no_signal")
    success = _evaluate_success(scenario.id, all_tools, booked_for_real, esmi_replies)

    return {
        "scenario_id": scenario.id,
        "label": scenario.label,
        "language": scenario.language,
        "tenant_id": tenant_id,
        "transcript": transcript,
        "tools_called": sorted(all_tools),
        "disposition": disposition,
        "success": success,
        "duration_ms": duration_ms,
        "note": _note_for(scenario.id, success),
    }
