# platform_api/calls.py — GET /platform/calls (Call Log data for the dashboard).

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from platform_api.call_log import OUTCOMES
from platform_api.security import require_tenant, verify_platform_secret

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_LIMIT = 200


def _parse_date(name: str, value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD")


@router.get("/platform/calls")
def platform_calls(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    outcome: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> dict:
    """Tenant-scoped call log, newest first.

    Auth: X-Platform-Secret + X-Tenant-Id headers (see security.py).
    Filters: outcome (booked|info|escalated|voicemail|abandoned|other),
    from_date / to_date (YYYY-MM-DD, inclusive, on started_at).
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
    where_sql = " AND ".join(where)

    with engine.connect() as conn:
        total = conn.execute(
            text(f"SELECT count(*) FROM calls WHERE {where_sql}"), params
        ).scalar_one()
        rows = conn.execute(
            text(
                f"""
                SELECT id, vapi_call_id, caller_e164, started_at, ended_at,
                       duration_sec, outcome, summary, transcript,
                       recording_key, cost_vapi, cost_llm, created_at
                FROM calls
                WHERE {where_sql}
                ORDER BY started_at DESC NULLS LAST, created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        ).mappings().all()

    calls = [
        {
            "id": str(r["id"]),
            "vapi_call_id": r["vapi_call_id"],
            "caller": r["caller_e164"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            "duration_sec": r["duration_sec"],
            "outcome": r["outcome"],
            "summary": r["summary"],
            "transcript": r["transcript"],
            # TODO(R2): becomes a signed R2 URL once recordings are copied out
            # of VAPI (see call_log.parse_end_of_call).
            "recording_url": r["recording_key"],
            "cost_vapi": float(r["cost_vapi"]) if r["cost_vapi"] is not None else None,
            "cost_llm": float(r["cost_llm"]) if r["cost_llm"] is not None else None,
        }
        for r in rows
    ]
    return {
        "tenant_id": tenant_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "calls": calls,
    }
