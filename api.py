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
import re
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import graph as _graph_module
from tools import (
    book_appointment,
    cancel_appointment,
    find_booking,
    get_pricing,
    list_available_slots,
    reschedule_appointment,
    search_knowledge_base,
)
from tenants import tenant_exists, resolve_vapi_tenant, load_tenant, namespaced_thread

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
    message: str
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


# ── Text utilities (copied from streamlit_app.py to avoid Streamlit import) ──

def _clean_response(text: str) -> str:
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(?!\s)(.+?)(?<!\s)\*", r"\1", text)
    text = re.sub(r"_(?!\s)(.+?)(?<!\s)_", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\n[-*_]{3,}\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_time_slots(text: str) -> tuple[str | None, list[str]]:
    slot_pattern = re.compile(
        r"\b(\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM))\b"
    )
    slots = slot_pattern.findall(text)
    date_pattern = re.compile(
        r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2}(?:,?\s+\d{4})?)",
        re.IGNORECASE,
    )
    date_match = date_pattern.search(text)
    date_label = date_match.group(1) if date_match else None
    return date_label, [s.strip() for s in slots]


def _strip_slots_from_text(text: str) -> str:
    text = re.sub(
        r"\n\s*[-•]\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM)",
        "",
        text,
    )
    text = re.sub(
        r"\n\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM)",
        "",
        text,
    )
    text = re.sub(r"Which of these works best for you\?\s*", "", text)
    return text.strip()


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

    except Exception as exc:
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
            to_emails="jquinonez2980@gmail.com",
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
    import json, tempfile, base64
    steps = {}

    # Step 1: resolve token data from whichever env var is present (matches tools.py priority)
    token_data = None
    token_b64 = os.environ.get("GOOGLE_TOKEN_B64")
    token_json_env = os.environ.get("GOOGLE_TOKEN_JSON")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    if token_b64:
        steps["source"] = "GOOGLE_TOKEN_B64"
        try:
            token_data = json.loads(base64.b64decode(token_b64).decode("utf-8"))
            steps["decode"] = "ok"
        except Exception as e:
            steps["decode"] = f"FAILED: {e}"
            return {"status": "error", "steps": steps}
    elif refresh_token and os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
        steps["source"] = "individual env vars"
        token_data = {
            "refresh_token": refresh_token,
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "token_uri": os.environ.get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }
    elif token_json_env:
        steps["source"] = "GOOGLE_TOKEN_JSON"
        try:
            token_data = json.loads(token_json_env.strip().strip("'\""))
            steps["decode"] = "ok"
        except Exception as e:
            steps["decode"] = f"FAILED: {e}"
            return {"status": "error", "steps": steps}
    else:
        steps["source"] = "none"
        return {"status": "error", "steps": steps, "detail": "No Google credential env vars found (checked GOOGLE_TOKEN_B64, GOOGLE_REFRESH_TOKEN, GOOGLE_TOKEN_JSON)"}

    steps["has_refresh_token"] = bool(token_data.get("refresh_token"))
    steps["has_client_id"] = bool(token_data.get("client_id"))
    expiry = token_data.get("expiry")
    steps["token_expiry"] = expiry

    # Step 3: build credentials
    try:
        from google.oauth2.credentials import Credentials
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(token_data, f)
            tmp_path = f.name
        creds = Credentials.from_authorized_user_file(tmp_path)
        os.unlink(tmp_path)
        steps["credentials_loaded"] = "ok"
        steps["creds_valid"] = creds.valid
        steps["creds_expired"] = creds.expired
        steps["has_token"] = bool(creds.token)
    except Exception as e:
        steps["credentials_loaded"] = f"FAILED: {e}"
        return {"status": "error", "steps": steps}

    # Step 4: call the API
    try:
        from googleapiclient.discovery import build
        from datetime import date
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        today = date.today().isoformat()
        result = service.freebusy().query(body={
            "timeMin": f"{today}T00:00:00Z",
            "timeMax": f"{today}T23:59:59Z",
            "timeZone": "America/Toronto",
            "items": [{"id": "primary"}],
        }).execute()
        steps["api_call"] = "ok"
        return {"status": "ok", "steps": steps, "busy_count": len(result["calendars"]["primary"]["busy"])}
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


def _enhance_slots_for_voice(slots_text: str, tenant_id: str = "default") -> str:
    """Append ISO timestamps to each slot line so the voice agent can pass them to book_appointment.

    Input line:  "Tuesday, May 27 10:00 AM – 10:30 AM"
    Output line: "Tuesday, May 27 10:00 AM – 10:30 AM | start_iso=2026-05-27T10:00:00-04:00 end_iso=2026-05-27T10:30:00-04:00"
    """
    import pytz
    from datetime import datetime, date as _date

    tz = pytz.timezone(load_tenant(tenant_id).business_tz)
    year = _date.today().year
    slot_re = re.compile(
        r"^(.+?,\s+\w+\s+\d+)\s+(\d{1,2}:\d{2}\s*(?:AM|PM))\s*[–\-]\s*(\d{1,2}:\d{2}\s*(?:AM|PM))$",
        re.IGNORECASE,
    )
    lines = []
    for line in slots_text.splitlines():
        m = slot_re.match(line.strip())
        if m:
            date_part, start_str, end_str = m.group(1), m.group(2).strip(), m.group(3).strip()
            try:
                start_dt = datetime.strptime(f"{date_part} {start_str} {year}", "%A, %B %d %I:%M %p %Y")
                start_dt = tz.localize(start_dt)
                end_dt = start_dt.replace(
                    hour=datetime.strptime(end_str, "%I:%M %p").hour,
                    minute=datetime.strptime(end_str, "%I:%M %p").minute,
                )
                lines.append(
                    f"{line.strip()} | start_iso={start_dt.isoformat()} end_iso={end_dt.isoformat()}"
                )
                continue
            except Exception:
                pass
        lines.append(line)
    return "\n".join(lines)


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
        return str(book_appointment.invoke({
            "summary": params.get("summary", load_tenant(tenant_id).voice_default_summary),
            "start_time": params["start_time"],
            "end_time": params["end_time"],
            "attendee_email": params.get("attendee_email") or params.get("caller_phone"),
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

    # ── New format: tool-calls ────────────────────────────────────────────
    if msg_type == "tool-calls" or "toolCallList" in msg:
        results = []
        for call in msg.get("toolCallList", []):
            call_id = call.get("id", "")
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
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
