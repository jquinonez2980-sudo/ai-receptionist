"""FastAPI chat endpoint for Esmi — replaces Streamlit as the HTTP interface.

Exposes:
  GET  /health  — liveness check
  POST /chat    — SSE streaming chat (LangGraph astream_events)

Run locally:  uvicorn api:app --reload --port 8000
Railway:      see railway.toml
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from graph import graph

log = logging.getLogger(__name__)

app = FastAPI(title="Esmi API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Next.js proxies all requests; browser never hits this directly
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


# ── Pydantic schema ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    thread_id: str


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


async def _stream_chat(message: str, thread_id: str) -> AsyncGenerator[str, None]:
    config = {"configurable": {"thread_id": thread_id}}
    full_text = ""
    chain_end_text = ""  # fallback if streaming tokens are empty

    try:
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
            version="v2",
        ):
            kind = event["event"]
            log.debug("SSE event: %s | name: %s", kind, event.get("name", "-"))

            if kind == "on_tool_start":
                tool_name = event.get("name", "tool")
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"

            elif kind == "on_tool_end":
                yield f"data: {json.dumps({'type': 'tool_end'})}\n\n"

            elif kind == "on_chat_model_stream":
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
async def health_env() -> dict:
    """Diagnostic: list env var names and show safe Railway metadata values."""
    import os
    keys = [k for k in os.environ if any(x in k.upper() for x in ["GOOGLE", "RAILWAY", "PORT", "TOKEN"])]
    safe_values = {
        k: os.environ[k] for k in [
            "RAILWAY_GIT_COMMIT_SHA", "RAILWAY_GIT_BRANCH",
            "RAILWAY_GIT_COMMIT_MESSAGE", "RAILWAY_SNAPSHOT_ID",
            "RAILWAY_ENVIRONMENT_NAME",
        ] if k in os.environ
    }
    return {"visible_keys": sorted(keys), "total_env_vars": len(os.environ), "railway_meta": safe_values}


@app.get("/health/calendar")
async def health_calendar() -> dict:
    """Diagnostic: test Google Calendar auth step by step."""
    import os, json, tempfile
    steps = {}

    # Step 1: check env var presence
    token_json_env = os.environ.get("GOOGLE_TOKEN_JSON")
    steps["env_var_set"] = token_json_env is not None
    steps["env_var_length"] = len(token_json_env) if token_json_env else 0

    # Step 2: parse JSON
    if token_json_env:
        try:
            token_data = json.loads(token_json_env)
            steps["json_parse"] = "ok"
            steps["has_refresh_token"] = bool(token_data.get("refresh_token"))
            steps["has_client_id"] = bool(token_data.get("client_id"))
        except Exception as e:
            steps["json_parse"] = f"FAILED: {e}"
            return {"status": "error", "steps": steps}
    else:
        return {"status": "error", "steps": steps, "detail": "GOOGLE_TOKEN_JSON env var not found"}

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
async def chat(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _stream_chat(req.message, req.thread_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
