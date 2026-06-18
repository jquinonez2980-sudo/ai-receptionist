"""FastAPI chat endpoint for Esmi — replaces Streamlit as the HTTP interface.

Exposes:
  GET  /health        — liveness check
  POST /chat          — SSE streaming chat (LangGraph astream_events)
  POST /voice/tools   — VAPI.ai tool execution webhook

Run locally:  uvicorn api:app --reload --port 8000
Railway:      see railway.toml
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import graph as _graph_module
from _text_utils import (
    _clean_response,
    _enhance_slots_for_voice,
    _parse_time_slots,
    _strip_slots_from_text,
)
from tenants import load_tenant, namespaced_thread, resolve_vapi_tenant, tenant_exists
from tools import (
    book_appointment,
    cancel_appointment,
    find_booking,
    get_pricing,
    list_available_slots,
    reschedule_appointment,
    search_knowledge_base,
)

log = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize the production graph with AsyncPostgresSaver at startup."""
    _graph_module.graph = await _graph_module.build_graph_async()
    yield


app = FastAPI(title="Esmi API", version="1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Comma-separated list of allowed CORS origins, e.g.:
#   ALLOWED_ORIGINS=https://orchelix.com,https://www.orchelix.com
# Falls back to ["*"] when unset (local dev only).
_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Chat-Secret"],
)


# ── Auth helpers ──────────────────────────────────────────────────────────────

VAPI_SERVER_SECRET = os.environ.get("VAPI_SERVER_SECRET")

# Shared secret the Next.js proxy sends on every /chat request.
# Fail-open when unset (logs a warning) so the endpoint stays usable before
# the secret is wired up in Railway + Vercel. Once set, missing/wrong header → 401.
CHAT_PROXY_SECRET = os.environ.get("CHAT_PROXY_SECRET")

# Set ALLOW_UNAUTHENTICATED_VOICE=1 only in local dev when you don't want to
# configure the full VAPI secret. Never set this on Railway/production.
_VOICE_UNAUTH_DEV = os.environ.get("ALLOW_UNAUTHENTICATED_VOICE") == "1"


def _verify_vapi_secret(request: Request) -> None:
    """Reject VAPI webhook calls that don't carry the shared server secret.

    Fail-closed: if VAPI_SERVER_SECRET is unset in production, voice endpoints
    return 503 rather than silently allowing unauthenticated requests. This
    prevents a misconfiguration from quietly re-opening the endpoint.

    To bypass in LOCAL DEV ONLY: set ALLOW_UNAUTHENTICATED_VOICE=1.
    """
    if not VAPI_SERVER_SECRET:
        if _VOICE_UNAUTH_DEV:
            log.warning(
                "ALLOW_UNAUTHENTICATED_VOICE=1 is set — /voice endpoints are "
                "UNAUTHENTICATED. Never use this in production."
            )
            return
        log.error(
            "VAPI_SERVER_SECRET is not set and ALLOW_UNAUTHENTICATED_VOICE is not 1. "
            "Refusing voice request. Set VAPI_SERVER_SECRET in Railway."
        )
        raise HTTPException(
            status_code=503,
            detail="Voice endpoint not configured — set VAPI_SERVER_SECRET.",
        )
    provided = request.headers.get("x-vapi-secret", "")
    if not hmac.compare_digest(provided, VAPI_SERVER_SECRET):
        log.warning("Rejected /voice request: bad or missing X-Vapi-Secret header.")
        raise HTTPException(status_code=401, detail="Unauthorized")


def _verify_chat_secret(request: Request) -> None:
    """Reject /chat requests missing the shared proxy secret.

    Fail-open when CHAT_PROXY_SECRET is not set in Railway (logs a warning so the
    misconfiguration is visible). Once the secret IS set, any request without the
    correct X-Chat-Secret header is rejected with 401.
    """
    if not CHAT_PROXY_SECRET:
        log.warning(
            "CHAT_PROXY_SECRET not set — /chat is open to direct access. "
            "Set CHAT_PROXY_SECRET in Railway and X-Chat-Secret in the Next.js proxy."
        )
        return
    provided = request.headers.get("X-Chat-Secret", "")
    if not hmac.compare_digest(provided, CHAT_PROXY_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Pydantic schema ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., max_length=4000)
    thread_id: str
    tenant_id: str | None = None   # optional body fallback; X-Tenant-Id header preferred


def _resolve_tenant(request: Request, req: "ChatRequest | None" = None) -> str:
    """Determine the tenant for a web request.

    Order: X-Tenant-Id header → request body tenant_id → 'default'. An unknown
    tenant id falls back to 'default' (logged) rather than erroring, so a
    misconfigured widget degrades gracefully to the base experience.
    """
    raw = request.headers.get("X-Tenant-Id")
    if not raw and req is not None:
        raw = req.tenant_id
    tid = (raw or "default").strip().lower() or "default"
    if not tenant_exists(tid):
        log.warning("Unknown tenant_id '%s' — falling back to default.", tid)
        return "default"
    return tid


class VapiToolRequest(BaseModel):
    message: dict = {}  # VAPI server message — format varies by API version


# _clean_response, _parse_time_slots, _strip_slots_from_text, _enhance_slots_for_voice
# are imported from _text_utils above.


# ── SSE generator ─────────────────────────────────────────────────────────────

def _extract_content(content) -> str:
    """Normalise AIMessageChunk content — handles str and list-of-blocks formats."""
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


async def _stream_chat(
    message: str, thread_id: str, tenant_id: str = "default"
) -> AsyncGenerator[str, None]:
    # Namespace the checkpoint thread per tenant so two tenants can never share
    # a conversation. 'default' is left unprefixed to keep existing Orchelix
    # threads addressable byte-for-byte across this deploy.
    ns_thread = namespaced_thread(tenant_id, thread_id)
    config = {"configurable": {"thread_id": ns_thread, "tenant_id": tenant_id}}
    full_text = ""
    chain_end_text = ""  # fallback if streaming tokens are empty

    try:
        async for event in _graph_module.graph.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            context={"tenant_id": tenant_id},   # reaches the prompt via runtime.context
            version="v2",
            include_subgraphs=True,
        ):
            kind = event["event"]
            log.debug("SSE event: %s | name: %s", kind, event.get("name", "-"))

            if kind == "on_tool_start":
                tool_name = event.get("name", "tool")
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"

            elif kind == "on_tool_end":
                yield f"data: {json.dumps({'type': 'tool_end'})}\n\n"

            elif kind == "on_chat_model_stream":
                # Drop the internal router classifier's tokens — its one-word
                # answer ("booker"/"informer"/"closer") must never reach the user.
                if "esmi-router-internal" in (event.get("tags") or []):
                    continue
                chunk = event["data"].get("chunk")
                if chunk is None:
                    continue
                text = _extract_content(getattr(chunk, "content", ""))
                if text:
                    full_text += text
                    yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"

            elif kind == "on_chain_end":
                # Fallback: extract final AI message from the outer graph's output.
                # Used when on_chat_model_stream tokens don't surface (e.g. nested graphs).
                output = event.get("data", {}).get("output")
                if isinstance(output, dict):
                    msgs = output.get("messages", [])
                    if msgs:
                        last = msgs[-1]
                        text = _extract_content(getattr(last, "content", ""))
                        if text:
                            chain_end_text = text  # keep the most recent non-empty one

        # If token streaming produced nothing, fall back to chain_end capture
        if not full_text and chain_end_text:
            log.warning(
                "Token streaming yielded nothing — using on_chain_end fallback for thread %s",
                thread_id,
            )
            full_text = chain_end_text
            yield f"data: {json.dumps({'type': 'token', 'content': full_text})}\n\n"

        # Build final done event
        cleaned = _clean_response(full_text)
        date_label, slots = _parse_time_slots(full_text)

        done: dict = {"type": "done", "full_text": cleaned}
        if slots:
            done["slots"] = slots
            done["full_text"] = _strip_slots_from_text(cleaned)
            if date_label:
                done["date_label"] = date_label

        yield f"data: {json.dumps(done)}\n\n"

    except Exception:
        log.exception("Stream error for thread %s", thread_id)
        yield f"data: {json.dumps({'type': 'error', 'message': 'Something went wrong — please try again.'})}\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "agent": "esmi"}


@app.get("/health/env")
async def health_env(request: Request) -> dict:
    """Diagnostic: list env var names and show safe Railway metadata values.

    Enumerating env var names reveals which secrets exist, so this endpoint is
    gated behind the same VAPI server secret used for the voice webhook.
    """
    _verify_vapi_secret(request)
    all_keys = sorted(os.environ.keys())
    safe_values = {
        k: os.environ[k] for k in [
            "RAILWAY_GIT_COMMIT_SHA", "RAILWAY_GIT_BRANCH",
            "RAILWAY_ENVIRONMENT_NAME",
        ] if k in os.environ
    }
    return {"all_env_keys": all_keys, "total_env_vars": len(os.environ), "railway_meta": safe_values}


@app.get("/health/sendgrid")
async def health_sendgrid(request: Request) -> dict:
    """Diagnostic: send a test escalation email and report the result."""
    _verify_vapi_secret(request)
    from tools import _get_sendgrid_key
    api_key = _get_sendgrid_key()
    if not api_key:
        return {"status": "error", "detail": "SendGrid key not found (checked SENDGRID_API_KEY and SENDGRID_API_KEY_B64)"}
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        message = Mail(
            from_email="info@orchelix.com",
            to_emails=load_tenant("default").email_booking_to,
            subject="[Esmi Test] SendGrid diagnostic",
            html_content="<p>SendGrid diagnostic test from Esmi health endpoint.</p>",
        )
        response = SendGridAPIClient(api_key).send(message)
        return {"status": "ok", "http_status": response.status_code}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/health/calendar")
async def health_calendar(request: Request) -> dict:
    """Diagnostic: test Google Calendar auth step by step."""
    _verify_vapi_secret(request)
    from tools import resolve_google_credentials
    steps: dict = {}

    try:
        creds = resolve_google_credentials("default")
        steps["credentials_loaded"] = "ok"
        steps["creds_valid"] = creds.valid
        steps["creds_expired"] = creds.expired
        steps["has_token"] = bool(creds.token)
    except Exception as e:
        steps["credentials_loaded"] = f"FAILED: {e}"
        return {"status": "error", "steps": steps}

    try:
        from datetime import date as _date

        from googleapiclient.discovery import build
        cal_id = load_tenant("default").calendar_id
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        today = _date.today().isoformat()
        result = service.freebusy().query(body={
            "timeMin": f"{today}T00:00:00Z",
            "timeMax": f"{today}T23:59:59Z",
            "timeZone": "America/Toronto",
            "items": [{"id": cal_id}],
        }).execute()
        steps["api_call"] = "ok"
        return {"status": "ok", "steps": steps, "busy_count": len(result["calendars"][cal_id]["busy"])}
    except Exception as e:
        steps["api_call"] = f"FAILED: {e}"
        return {"status": "error", "steps": steps}


@app.post("/chat")
@limiter.limit("10/minute")
async def chat(request: Request, req: ChatRequest) -> StreamingResponse:
    _verify_chat_secret(request)
    tenant_id = _resolve_tenant(request, req)
    return StreamingResponse(
        _stream_chat(req.message, req.thread_id, tenant_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _unresolved(value: str | None) -> bool:
    """True when value is absent, empty, a VAPI template literal, or the sentinel."""
    if not value:
        return True
    v = value.strip()
    return v.startswith("{{") or v == "Phone not captured"


def _run_voice_tool(name: str, params: dict, tenant_id: str = "default") -> str:
    """Execute a named voice tool and return a plain-text result.

    The voice path calls tools directly (bypassing the graph), so it must pass
    config={"configurable":{"tenant_id":...}} on every .invoke for the tool's
    RunnableConfig injection to resolve the right tenant.
    """
    cfg = {"configurable": {"tenant_id": tenant_id}}
    if name == "get_current_date":
        from datetime import date
        today = date.today()
        return (
            f"Today is {today.strftime('%A, %B %d, %Y')}. "
            f"ISO format: {today.isoformat()}."
        )
    if name == "get_pricing":
        return str(get_pricing.invoke({}, config=cfg))
    if name == "search_knowledge_base":
        return str(search_knowledge_base.invoke({"query": params["query"]}, config=cfg))
    if name == "list_available_slots":
        raw = str(list_available_slots.invoke({
            "start_date": params["start_date"],
            "end_date": params["end_date"],
        }, config=cfg))
        return _enhance_slots_for_voice(raw, tenant_id)
    if name == "book_appointment":
        # Prefer the first non-unresolved contact; real phone injected upstream takes precedence.
        contact = next(
            (v for v in (params.get("attendee_email"), params.get("caller_phone")) if not _unresolved(v)),
            None,
        )
        return str(book_appointment.invoke({
            "summary": params.get("summary", load_tenant(tenant_id).voice_default_summary),
            "start_time": params["start_time"],
            "end_time": params["end_time"],
            "attendee_email": contact,
        }, config=cfg))
    if name == "find_booking":
        return str(find_booking.invoke({
            "contact": params.get("contact") or params.get("attendee_email") or params.get("caller_phone"),
        }, config=cfg))
    if name == "reschedule_appointment":
        return str(reschedule_appointment.invoke({
            "event_id": params["event_id"],
            "new_start_time": params["new_start_time"],
            "new_end_time": params["new_end_time"],
        }, config=cfg))
    if name == "cancel_appointment":
        return str(cancel_appointment.invoke({"event_id": params["event_id"]}, config=cfg))
    log.warning("VAPI sent unknown tool: %s", name)
    return "Unknown tool."


@app.post("/voice/tools")
async def voice_tools(request: Request) -> dict:
    """VAPI.ai webhook — executes tools on behalf of the voice agent.

    Handles both VAPI formats:
      Old: message.functionCall.{name, parameters}
      New: message.toolCallList[].{id, function.{name, arguments}}
    """
    _verify_vapi_secret(request)
    body = await request.json()
    log.info("VAPI raw payload: %s", json.dumps(body))

    # Each tenant has its own VAPI assistant / phone number; map it to a tenant.
    tenant_id = resolve_vapi_tenant(body)
    log.info("VAPI tenant resolved: %s", tenant_id)

    msg = body.get("message", body)  # some VAPI versions omit the outer wrapper
    msg_type = msg.get("type", "")

    # Real caller number from VAPI payload — used to fill in missing/broken args.
    caller_number = (msg.get("call", {}).get("customer", {}).get("number") or "").strip()

    # ── New format: tool-calls ────────────────────────────────────────────
    if msg_type == "tool-calls" or "toolCallList" in msg:
        results = []
        for call in msg.get("toolCallList", []):
            call_id = call.get("id", "")
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            # Inject real caller number when the LLM's caller_phone arg is unresolved.
            if name == "book_appointment" and caller_number and _unresolved(params.get("caller_phone")):
                params["caller_phone"] = caller_number
            log.info("VAPI tool-calls: %s | params: %s", name, params)
            try:
                result = await asyncio.to_thread(_run_voice_tool, name, params, tenant_id)
            except Exception:
                log.exception("VAPI tool %s failed", name)
                result = "Something went wrong — I'll connect you with our team."
            results.append({"toolCallId": call_id, "result": result})
        return {"results": results}

    # ── Old format: function-call ─────────────────────────────────────────
    fn = msg.get("functionCall", {})
    name = fn.get("name", "")
    params = fn.get("parameters", {})
    if name == "book_appointment" and caller_number and _unresolved(params.get("caller_phone")):
        params["caller_phone"] = caller_number
    log.info("VAPI function-call: %s | params: %s", name, params)
    try:
        result = await asyncio.to_thread(_run_voice_tool, name, params, tenant_id)
    except Exception:
        log.exception("VAPI tool %s failed", name)
        result = "Something went wrong — I'll connect you with our team."
    return {"result": result}


@app.post("/voice/debug")
async def voice_debug(request: Request) -> dict:
    """Diagnostic: echo the raw VAPI payload so we can inspect the format."""
    _verify_vapi_secret(request)
    body = await request.json()
    log.info("VAPI debug payload: %s", json.dumps(body))
    return {"received": body}
