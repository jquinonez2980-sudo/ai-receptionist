# platform_api/calls.py — GET /platform/calls (Call Log data for the dashboard)
# and GET /platform/calls/{id}/recording/export (WhatsApp-friendly MP3 download).

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from platform_api.call_log import OUTCOMES
from platform_api.security import require_tenant, verify_platform_secret

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_LIMIT = 200
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _parse_date(name: str, value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD")


_CALL_COLUMNS = (
    "id, vapi_call_id, caller_e164, started_at, ended_at, "
    "duration_sec, outcome, language, summary, transcript, "
    "recording_key, cost_vapi, cost_llm, created_at"
)


def _row_to_call(r) -> dict:
    """Shared row → API-shape mapping for both the list and detail routes —
    one place decides what a call looks like on the wire, so the two
    endpoints can never quietly drift apart on field names/types."""
    from platform_api.recordings import playable_recording_url

    return {
        "id": str(r["id"]),
        "vapi_call_id": r["vapi_call_id"],
        "caller": r["caller_e164"],
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
        "duration_sec": r["duration_sec"],
        "outcome": r["outcome"],
        # None ("Unknown" in the dashboard) for calls logged before the
        # language column existed, or with no transcript to detect from.
        "language": r["language"],
        "summary": r["summary"],
        "transcript": r["transcript"],
        # R2 object keys are presigned into short-lived URLs here; legacy
        # raw VAPI URLs pass through (see platform_api/recordings.py).
        "recording_url": playable_recording_url(r["recording_key"]),
        "cost_vapi": float(r["cost_vapi"]) if r["cost_vapi"] is not None else None,
        "cost_llm": float(r["cost_llm"]) if r["cost_llm"] is not None else None,
    }


@router.get("/platform/calls")
def platform_calls(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    outcome: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    language: Optional[str] = None,
    has_recording: Optional[bool] = None,
) -> dict:
    """Tenant-scoped call log, newest first.

    Auth: X-Platform-Secret + X-Tenant-Id headers (see security.py).
    Filters: outcome (booked|info|escalated|voicemail|abandoned|other),
    from_date / to_date (YYYY-MM-DD, inclusive, on started_at), language
    (es|en — matches platform_api.call_log._detect_language's output),
    has_recording (true/false).
    Sync `def` on purpose: FastAPI runs it in the threadpool, keeping the
    blocking SQLAlchemy queries off the event loop.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    limit = max(1, min(int(limit), _MAX_LIMIT))
    offset = max(0, int(offset))
    if outcome is not None and outcome not in OUTCOMES:
        raise HTTPException(
            status_code=400, detail=f"outcome must be one of: {', '.join(OUTCOMES)}"
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
    if language:
        where.append("language = :language")
        params["language"] = language.strip().lower()
    if has_recording is not None:
        where.append("recording_key IS NOT NULL" if has_recording else "recording_key IS NULL")
    where_sql = " AND ".join(where)

    with engine.connect() as conn:
        total = conn.execute(
            text(f"SELECT count(*) FROM calls WHERE {where_sql}"), params
        ).scalar_one()
        rows = conn.execute(
            text(
                f"""
                SELECT {_CALL_COLUMNS}
                FROM calls
                WHERE {where_sql}
                ORDER BY started_at DESC NULLS LAST, created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        ).mappings().all()

    calls = [_row_to_call(r) for r in rows]
    return {
        "tenant_id": tenant_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "calls": calls,
    }


@router.get("/platform/calls/{call_id}")
def platform_call_detail(request: Request, call_id: str) -> dict:
    """Single call, tenant-scoped. 404 for a missing id AND for another
    tenant's call id (never distinguish the two — see security.py's
    require_tenant convention elsewhere in this package: a cross-tenant
    lookup must read identically to a nonexistent one).

    The list endpoint above already returns every field a row has (it's
    the dashboard's only fetch today — CallLog.tsx expands a list row
    in place rather than re-fetching), so this exists for direct
    deep-links to one call rather than because the list is missing data.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if not _UUID_RE.match(call_id or ""):
        raise HTTPException(status_code=404, detail="Call not found")

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {_CALL_COLUMNS} FROM calls WHERE id = :id AND tenant_id = :tenant_id"),
            {"id": uuid.UUID(call_id), "tenant_id": tenant_id},
        ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")

    return {"tenant_id": tenant_id, "call": _row_to_call(row)}


@router.get("/platform/calls/{call_id}/recording/export")
def platform_call_recording_export(
    request: Request,
    call_id: str,
    format: str = "mp3",
) -> dict:
    """WhatsApp-friendly recording export (lazy MP3 sidecar in R2).

    Auth: X-Platform-Secret + X-Tenant-Id (same as GET /platform/calls).
    Returns a short-lived presigned download URL for an MP3 — never streams
    audio bytes through this API (keeps Vercel proxy limits out of the path).

    First request for a call converts the archived WAV → mono 64kbps MP3 and
    stores a permanent sibling object in R2; later requests only presign.
    In-dashboard WAV playback is unchanged.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    fmt = (format or "mp3").strip().lower()
    if fmt != "mp3":
        raise HTTPException(
            status_code=400,
            detail="Only format=mp3 is supported for WhatsApp export",
        )
    if not _UUID_RE.match(call_id or ""):
        raise HTTPException(status_code=400, detail="call_id must be a UUID")

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, vapi_call_id, recording_key, started_at
                FROM calls
                WHERE id = :id AND tenant_id = :tenant_id
                """
            ),
            {"id": uuid.UUID(call_id), "tenant_id": tenant_id},
        ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Call not found")

    recording_key = row["recording_key"]
    if not recording_key:
        raise HTTPException(status_code=404, detail="No recording for this call")

    from platform_api.recordings import (
        RecordingExportError,
        RecordingUnavailable,
        downloadable_mp3_url,
        ensure_mp3_export,
        r2_configured,
    )

    if not r2_configured():
        raise HTTPException(
            status_code=503,
            detail="Recording storage is not configured — cannot export MP3.",
        )

    try:
        mp3_key = ensure_mp3_export(
            tenant_id,
            row["vapi_call_id"],
            recording_key,
        )
    except RecordingUnavailable as e:
        # Expired / missing source — permanent for the client (410).
        log.info(
            "Recording export unavailable tenant=%s call=%s: %s",
            tenant_id,
            call_id,
            e,
        )
        raise HTTPException(status_code=410, detail=str(e) or "Recording no longer available")
    except RecordingExportError as e:
        msg = str(e) or "Could not prepare MP3 export"
        # Misconfiguration (no R2 / no ffmpeg) is 503; other convert failures 500.
        status = 503 if "not configured" in msg.lower() or "not installed" in msg.lower() else 500
        log.warning(
            "Recording export failed tenant=%s call=%s (%s)",
            tenant_id,
            call_id,
            e,
        )
        raise HTTPException(status_code=status, detail=msg)

    started = row["started_at"]
    stamp = started.strftime("%Y-%m-%d-%H%M") if started is not None else call_id[:8]
    filename = f"esmi-call-{stamp}.mp3"

    try:
        url, expires_in = downloadable_mp3_url(mp3_key, filename)
    except RecordingExportError as e:
        log.warning("Presign after export failed call=%s: %s", call_id, e)
        raise HTTPException(status_code=500, detail=str(e) or "Could not create download link")

    return {
        "url": url,
        "filename": filename,
        "content_type": "audio/mpeg",
        "expires_in": expires_in,
    }
