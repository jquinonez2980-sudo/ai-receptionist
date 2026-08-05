# platform_api/chats.py — GET /platform/chats (list) and GET /platform/chats/{id}
# (detail + transcript) for the dashboard.
#
# List is parallel to platform_api/calls.py, minus the recording/export
# surface — chat_sessions carries no recording_key, and (see chat_log.py's
# module comment) the LangGraph checkpointer stays the transcript source of
# truth. Detail reads that checkpointer directly — see _load_transcript.

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from platform_api.chat_log import CHAT_OUTCOMES
from platform_api.security import require_tenant, verify_platform_secret

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_LIMIT = 200
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# How many checkpoints to walk when reconstructing a transcript. Generous for
# a chat conversation (each turn is at most a couple of graph-node steps);
# bounds the read against a pathological/runaway thread.
_MAX_CHECKPOINTS = 500


def _parse_date(name: str, value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD")


@router.get("/platform/chats")
def platform_chats(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    outcome: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> dict:
    """Tenant-scoped chat session log, newest first.

    Auth: X-Platform-Secret + X-Tenant-Id headers (see security.py).
    Filters: outcome (booked|escalated), from_date / to_date (YYYY-MM-DD,
    inclusive, on started_at).
    Sync `def` on purpose: FastAPI runs it in the threadpool, keeping the
    blocking SQLAlchemy queries off the event loop.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    limit = max(1, min(int(limit), _MAX_LIMIT))
    offset = max(0, int(offset))
    if outcome is not None and outcome not in CHAT_OUTCOMES:
        raise HTTPException(
            status_code=400, detail=f"outcome must be one of: {', '.join(CHAT_OUTCOMES)}"
        )
    d_from = _parse_date("from_date", from_date)
    d_to = _parse_date("to_date", to_date)

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    where = ["tenant_id = :tenant_id"]
    params: dict = {"tenant_id": tenant_id}
    if outcome:
        where.append("outcome = :outcome")
        params["outcome"] = outcome
    if d_from:
        where.append("started_at >= :d_from")
        params["d_from"] = d_from
    if d_to:
        where.append("started_at < :d_to_excl")  # inclusive end date
        params["d_to_excl"] = d_to + timedelta(days=1)
    where_sql = " AND ".join(where)

    with engine.connect() as conn:
        total = conn.execute(
            text(f"SELECT count(*) FROM chat_sessions WHERE {where_sql}"), params
        ).scalar_one()
        rows = conn.execute(
            text(
                f"""
                SELECT id, thread_id, channel, started_at, last_at,
                       message_count, outcome, summary
                FROM chat_sessions
                WHERE {where_sql}
                ORDER BY last_at DESC NULLS LAST, started_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        ).mappings().all()

    chats = [
        {
            "id": str(r["id"]),
            "thread_id": r["thread_id"],
            "channel": r["channel"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "last_at": r["last_at"].isoformat() if r["last_at"] else None,
            "message_count": r["message_count"],
            "outcome": r["outcome"],
            "summary": r["summary"],
        }
        for r in rows
    ]
    return {
        "tenant_id": tenant_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "chats": chats,
    }


# ── detail + transcript ────────────────────────────────────────────────────

_TRANSCRIPT_ROLES = {"human": "user", "ai": "assistant"}


def _extract_content(content) -> str:
    """Duplicated from api.py's _extract_content (not imported) — handles str
    and list-of-content-block message formats. Same tiny-helper-duplication
    convention as usage_alerts.py._sendgrid_key: platform_api doesn't reach
    into the agent runtime for something this small."""
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


def _transcript_from_checkpoints(checkpoint_tuples: list) -> list[dict]:
    """Reconstruct an ordered, deduped transcript from a thread's full
    checkpoint history (oldest to newest — callers must reverse langgraph's
    newest-first alist() order first).

    Walking the FULL history, not just the latest checkpoint, matters:
    graph._compress_node removes older messages from state once it folds
    them into conversation_summary (a RemoveMessage), so reading only the
    latest checkpoint would silently drop the earlier half of a long
    conversation. Each message is recorded once, the first checkpoint it
    appears in — the closest thing to a per-message timestamp the
    checkpointer actually has, since LangChain message objects carry none
    themselves. Tool calls, tool results, and empty-content turns (a pure
    tool-call step with nothing user-visible to say) are dropped — this is
    a human-readable transcript, not a raw event log.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for ct in checkpoint_tuples:
        ts = ct.checkpoint.get("ts")
        messages = (ct.checkpoint.get("channel_values") or {}).get("messages") or []
        for m in messages:
            mid = getattr(m, "id", None)
            if mid is None or mid in seen:
                continue
            role = _TRANSCRIPT_ROLES.get(getattr(m, "type", None))
            if role is None:
                continue  # ToolMessage, SystemMessage, RemoveMessage, ...
            text = _extract_content(getattr(m, "content", ""))
            if not text:
                continue
            seen.add(mid)
            out.append({"role": role, "content": text, "timestamp": ts})
    return out


async def _load_transcript(thread_id: str) -> list[dict]:
    """Read the full message history for a thread from the LIVE LangGraph
    checkpointer (graph.graph.checkpointer).

    chat_sessions is metadata only — the Postgres checkpointer api.py's
    /chat already writes to is the one place full message content lives
    (see chat_log.py's module note). Deliberately imports graph.py here:
    the one place in platform_api that reaches into the agent runtime,
    because this endpoint's entire job is reading that runtime's own
    persisted state — there is no independent copy of it to read instead —
    and reusing the live checkpointer (already-open connection pool,
    guaranteed identical config to the write path) beats standing up a
    second, duplicate one just to preserve the usual separation.
    """
    import graph as _graph_module

    checkpointer = getattr(_graph_module.graph, "checkpointer", None)
    if checkpointer is None:
        return []

    config = {"configurable": {"thread_id": thread_id}}
    tuples = [
        ct async for ct in checkpointer.alist(config, limit=_MAX_CHECKPOINTS)
    ]
    tuples.reverse()  # alist() is newest-first; walk oldest->newest to build in order
    return _transcript_from_checkpoints(tuples)


@router.get("/platform/chats/{chat_id}")
async def platform_chat_detail(chat_id: str, request: Request) -> dict:
    """Chat session detail: metadata + the full message transcript.

    Auth: X-Platform-Secret + X-Tenant-Id (same as GET /platform/chats).
    Async route (unlike the rest of platform_api) because the transcript
    read goes through LangGraph's async checkpointer API; the chat_sessions
    lookup is still the usual blocking SQLAlchemy call, so it's run off the
    event loop via asyncio.to_thread rather than switching the whole module
    to an async DB driver for one route.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if not _UUID_RE.match(chat_id or ""):
        raise HTTPException(status_code=400, detail="chat_id must be a UUID")

    def _load_session():
        from sqlalchemy import text

        from platform_db import get_engine

        engine = get_engine()
        if engine is None:
            raise HTTPException(status_code=503, detail="Platform DB not configured.")
        with engine.connect() as conn:
            return conn.execute(
                text(
                    """
                    SELECT id, thread_id, channel, started_at, last_at,
                           message_count, outcome, summary
                    FROM chat_sessions
                    WHERE id = :id AND tenant_id = :tenant_id
                    """
                ),
                {"id": chat_id, "tenant_id": tenant_id},
            ).mappings().first()

    row = await asyncio.to_thread(_load_session)
    # Scoped by tenant_id in the WHERE clause above, not just id — a real
    # chat_id belonging to another tenant 404s exactly like an unknown one
    # (fail closed on tenant mismatch), rather than leaking that it exists.
    if row is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    messages = await _load_transcript(row["thread_id"])

    return {
        "tenant_id": tenant_id,
        "id": str(row["id"]),
        "thread_id": row["thread_id"],
        "channel": row["channel"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "last_active_at": row["last_at"].isoformat() if row["last_at"] else None,
        "message_count": row["message_count"],
        "outcome": row["outcome"],
        "summary": row["summary"],
        "messages": messages,
    }
