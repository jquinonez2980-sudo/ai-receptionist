# platform_api/call_log.py — VAPI end-of-call-report → calls table.
#
# Everything here is defensive: VAPI payload shapes vary by version and by
# which artifacts are enabled, so every field is optional and every extractor
# tolerates missing/renamed keys. A parse gap must degrade to a sparser row,
# never to a lost call.

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from tenants import resolve_vapi_tenant

log = logging.getLogger(__name__)

OUTCOMES = ("booked", "info", "escalated", "voicemail", "abandoned", "other")

# Tool names that mean the call was handed to a human.
_ESCALATION_TOOLS = {"escalate_to_human", "transfercall", "transfer_call"}
# Substrings in a book_appointment tool RESULT that mean the booking failed.
_BOOKING_FAILURE_MARKERS = (
    "error", "sorry", "unable", "couldn't", "could not", "cannot",
    "not available", "no availability", "failed", "fully booked",
)


def _dt(value: Any) -> Optional[datetime]:
    """Parse a VAPI timestamp (ISO string or epoch ms) to aware UTC datetime."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):  # epoch millis
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        s = str(value).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _first(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", {}, []):
            return v
    return None


def _tool_activity(messages: list) -> tuple[set[str], dict[str, list[str]]]:
    """Collect (called tool names, tool name → result strings) from VAPI
    artifact messages. Tolerates both toolCalls-array and tool_call_result
    message shapes."""
    called: set[str] = set()
    results: dict[str, list[str]] = {}
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        for tc in m.get("toolCalls") or m.get("tool_calls") or []:
            if isinstance(tc, dict):
                name = (tc.get("function") or {}).get("name") or tc.get("name")
                if name:
                    called.add(str(name).lower())
        role = str(m.get("role") or "").lower()
        if role in ("tool_call_result", "tool"):
            name = str(m.get("name") or "").lower()
            if name:
                called.add(name)
                result = m.get("result")
                if not isinstance(result, str):
                    result = json.dumps(result) if result is not None else ""
                results.setdefault(name, []).append(result)
    return called, results


def _user_turns(messages: list, transcript_text: str) -> int:
    n = sum(
        1
        for m in messages or []
        if isinstance(m, dict) and str(m.get("role") or "").lower() in ("user", "human")
    )
    if n == 0 and transcript_text:
        n = sum(
            1
            for line in transcript_text.splitlines()
            if line.strip().lower().startswith(("user:", "customer:"))
        )
    return n


def derive_outcome(
    ended_reason: str,
    tools_called: set[str],
    tool_results: dict[str, list[str]],
    user_turns: int,
) -> str:
    """Heuristic outcome for the dashboard. Precedence:
    voicemail > booked > escalated > other(error) > abandoned > info."""
    reason = (ended_reason or "").lower()
    if "voicemail" in reason:
        return "voicemail"

    book_results = tool_results.get("book_appointment", [])
    booked_ok = any(
        not any(marker in r.lower() for marker in _BOOKING_FAILURE_MARKERS)
        for r in book_results
    )
    # Tool was invoked but no result captured in the artifact — assume booked
    # rather than losing the signal; the appointment row will disambiguate later.
    if booked_ok or ("book_appointment" in tools_called and not book_results):
        return "booked"

    if tools_called & _ESCALATION_TOOLS or "forwarded" in reason:
        return "escalated"
    if "error" in reason:
        return "other"
    if user_turns == 0:
        return "abandoned"
    return "info"


def parse_end_of_call(payload: dict) -> Optional[dict]:
    """Extract a calls-table row dict from an end-of-call-report payload.

    Returns None when the payload has no call id (nothing to key the upsert
    on). All other fields are best-effort nullable.
    """
    msg = (payload or {}).get("message") or {}
    call = msg.get("call") or {}
    artifact = msg.get("artifact") or {}

    call_id = _first(call.get("id"), msg.get("callId"), (msg.get("callObject") or {}).get("id"))
    if not call_id:
        return None

    messages = _first(artifact.get("messages"), msg.get("messages")) or []
    transcript_text = str(_first(artifact.get("transcript"), msg.get("transcript")) or "")
    analysis = msg.get("analysis") or {}

    started = _dt(_first(msg.get("startedAt"), call.get("startedAt")))
    ended = _dt(_first(msg.get("endedAt"), call.get("endedAt")))
    duration = _num(_first(msg.get("durationSeconds"), call.get("durationSeconds")))
    if duration is None and msg.get("durationMs") is not None:
        duration = _num(msg["durationMs"])
        duration = duration / 1000.0 if duration is not None else None
    if duration is None and started and ended:
        duration = max(0.0, (ended - started).total_seconds())

    cost_breakdown = msg.get("costBreakdown") or {}
    tools_called, tool_results = _tool_activity(messages)
    user_turns = _user_turns(messages, transcript_text)

    return {
        "vapi_call_id": str(call_id),
        "vapi_phone_number_id": _first(
            msg.get("phoneNumberId"), call.get("phoneNumberId"),
            (call.get("phoneNumber") or {}).get("id"),
        ),
        "caller_e164": _first(
            (msg.get("customer") or {}).get("number"),
            (call.get("customer") or {}).get("number"),
        ),
        "started_at": started,
        "ended_at": ended,
        "duration_sec": int(round(duration)) if duration is not None else None,
        "outcome": derive_outcome(
            str(msg.get("endedReason") or ""), tools_called, tool_results, user_turns
        ),
        # Keep both the flat text and the structured turns — the dashboard
        # renders text now, and richer views can use messages later.
        "transcript": json.dumps({"text": transcript_text, "messages": messages}),
        "summary": _first(analysis.get("summary"), msg.get("summary")),
        # TODO(R2): recording URLs from VAPI expire — Phase 1 copies the file
        # to R2 and stores the object key here. Until then store the raw URL.
        "recording_key": _first(
            artifact.get("recordingUrl"),
            (artifact.get("recording") or {}).get("url"),
            msg.get("recordingUrl"),
            msg.get("stereoRecordingUrl"),
        ),
        "cost_vapi": _num(_first(msg.get("cost"), cost_breakdown.get("total"))),
        "cost_llm": _num(cost_breakdown.get("llm")),
    }


def upsert_call(tenant_id: str, row: dict) -> bool:
    """Insert-or-update the call row, keyed on vapi_call_id.

    Returns False (after logging) when the platform DB is unavailable —
    the webhook still 200s; the call is recoverable later from VAPI's
    call-history API.
    """
    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        log.error(
            "VAPI call %s (tenant %s): DATABASE_URL not set — call NOT logged.",
            row["vapi_call_id"], tenant_id,
        )
        return False

    with engine.begin() as conn:
        # The FK target must exist even for tenants created after the importer
        # ran. Bare row: status/plan defaults apply.
        conn.execute(
            text("INSERT INTO tenants (id) VALUES (:tid) ON CONFLICT (id) DO NOTHING"),
            {"tid": tenant_id},
        )
        conn.execute(
            text(
                """
                INSERT INTO calls (
                    tenant_id, vapi_call_id, vapi_phone_number_id, caller_e164,
                    started_at, ended_at, duration_sec, outcome,
                    transcript, summary, recording_key, cost_vapi, cost_llm
                ) VALUES (
                    :tenant_id, :vapi_call_id, :vapi_phone_number_id, :caller_e164,
                    :started_at, :ended_at, :duration_sec, :outcome,
                    CAST(:transcript AS jsonb), :summary, :recording_key,
                    :cost_vapi, :cost_llm
                )
                ON CONFLICT (vapi_call_id) DO UPDATE SET
                    ended_at = EXCLUDED.ended_at,
                    duration_sec = EXCLUDED.duration_sec,
                    outcome = EXCLUDED.outcome,
                    transcript = EXCLUDED.transcript,
                    summary = EXCLUDED.summary,
                    recording_key = EXCLUDED.recording_key,
                    cost_vapi = EXCLUDED.cost_vapi,
                    cost_llm = EXCLUDED.cost_llm
                """
            ),
            {**row, "tenant_id": tenant_id},
        )
    return True


def record_end_of_call(payload: dict) -> Optional[dict]:
    """Full pipeline: type-check → tenant → parse → derive → upsert.

    Returns a small status dict for logging, or None when the payload was
    ignored (wrong type / no call id). Exceptions propagate to the route
    handler, which logs them and still returns 200.
    """
    msg_type = ((payload or {}).get("message") or {}).get("type")
    if msg_type != "end-of-call-report":
        log.info("VAPI webhook: ignoring message type %r.", msg_type)
        return None

    tenant_id = resolve_vapi_tenant(payload)
    row = parse_end_of_call(payload)
    if row is None:
        log.warning("VAPI webhook: end-of-call-report without a call id — skipped.")
        return None

    stored = upsert_call(tenant_id, row)
    if stored:
        log.info(
            "VAPI call logged: tenant=%s call=%s outcome=%s duration=%ss cost=%s",
            tenant_id, row["vapi_call_id"], row["outcome"],
            row["duration_sec"], row["cost_vapi"],
        )
    return {
        "tenant_id": tenant_id,
        "call_id": row["vapi_call_id"],
        "outcome": row["outcome"],
        "stored": stored,
    }
