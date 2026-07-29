# platform_api/leads.py — GET/PATCH /platform/leads (Leads inbox, dashboard).
#
# DATA SOURCE: the `leads` table (leads.py at the repo root — NOT this
# package) is the real, already-live capture pipeline: graph.py's multi-agent
# booker/closer nodes upsert a row, keyed by LangGraph thread_id, whenever a
# WEB CHAT conversation becomes qualified (booked or escalated). But voice
# bypasses the graph entirely (api.py's /voice/tools calls tools directly),
# so a phone escalation is never recorded there — only emailed.
#
# This endpoint unions the real `leads` rows with synthetic ones derived from
# escalated `calls` rows that have no matching `leads` row yet (voice's
# uncaptured half — see platform_api/call_log.py::derive_outcome, where
# escalate_to_human is what flips a call to outcome='escalated'). A derived
# row's id is `voice:<call id>`, which cannot collide with a real (tenant-
# namespaced) thread_id — see tenants.namespaced_thread.
#
# PATCH promotes a derived lead to a real row on first write (upsert on the
# thread_id primary key), so a status change always has somewhere durable to
# land, and a later web-chat continuation of that thread (if the tenant ever
# starts a Chat leg for the same conversation) won't reset it.

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from platform_api.security import require_tenant, verify_platform_secret

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_LIMIT = 200
_VOICE_PREFIX = "voice:"
STATUSES = ("new", "contacted", "won", "lost")


def _row_to_lead(r) -> dict:
    return {
        "id": r["thread_id"],
        "contact": r["contact"],
        "summary": r["summary"],
        "lead_score": r["lead_score"],
        "qualified": bool(r["qualified"]),
        "status": r["status"],
        "last_updated": r["last_updated"].isoformat() if r["last_updated"] else None,
        "source_call_id": str(r["source_call_id"]) if r["source_call_id"] else None,
        "derived": bool(r["derived"]),
    }


@router.get("/platform/leads")
def platform_leads(
    request: Request,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Tenant-scoped leads inbox, most recently updated first.

    Auth: X-Platform-Secret + X-Tenant-Id headers (see security.py).
    status: new | contacted | won | lost (omit for all).
    search: matched against contact / summary (case-insensitive).
    Sync `def` on purpose: FastAPI runs it in the threadpool, keeping the
    blocking SQLAlchemy queries off the event loop.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    limit = max(1, min(int(limit), _MAX_LIMIT))
    offset = max(0, int(offset))
    if status is not None and status not in STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of: {', '.join(STATUSES)}"
        )
    needle = (search or "").strip()

    from sqlalchemy import bindparam as sa_bindparam
    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    # `combined` unions real (web chat) leads with synthetic ones derived
    # from escalated calls (voice) that have no leads row yet.
    combined_cte = """
        WITH combined AS (
            SELECT
                l.thread_id, l.contact, l.summary, l.lead_score, l.qualified,
                l.status, l.last_updated,
                NULL::uuid AS source_call_id, FALSE AS derived
            FROM leads l
            WHERE l.tenant_id = :tenant_id

            UNION ALL

            SELECT
                'voice:' || c.id::text AS thread_id,
                c.caller_e164 AS contact, c.summary,
                NULL::integer AS lead_score, TRUE AS qualified,
                'new' AS status, COALESCE(c.started_at, c.created_at) AS last_updated,
                c.id AS source_call_id, TRUE AS derived
            FROM calls c
            WHERE c.tenant_id = :tenant_id
              AND c.outcome = 'escalated'
              AND NOT EXISTS (
                  SELECT 1 FROM leads l2 WHERE l2.thread_id = 'voice:' || c.id::text
              )
        )
    """
    where = ["1=1"]
    params: dict = {"tenant_id": tenant_id}
    if status:
        where.append("status = :status")
        params["status"] = status
    if needle:
        where.append("(contact ILIKE :q OR summary ILIKE :q)")
        params["q"] = f"%{needle}%"
    where_sql = " AND ".join(where)

    with engine.connect() as conn:
        total = conn.execute(
            text(f"{combined_cte} SELECT count(*) FROM combined WHERE {where_sql}"),
            params,
        ).scalar_one()
        rows = conn.execute(
            text(
                f"""
                {combined_cte}
                SELECT * FROM combined
                WHERE {where_sql}
                ORDER BY last_updated DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        ).mappings().all()

        call_ids = [r["source_call_id"] for r in rows if r["source_call_id"]]
        calls_by_id: dict = {}
        if call_ids:
            stmt = text(
                "SELECT id, vapi_call_id, started_at, outcome, summary "
                "FROM calls WHERE id IN :ids"
            ).bindparams(sa_bindparam("ids", expanding=True))
            call_rows = conn.execute(stmt, {"ids": call_ids}).mappings().all()
            calls_by_id = {c["id"]: c for c in call_rows}

    leads = []
    for r in rows:
        lead = _row_to_lead(r)
        call = calls_by_id.get(r["source_call_id"])
        lead["call"] = (
            {
                "id": str(call["id"]),
                "vapi_call_id": call["vapi_call_id"],
                "started_at": call["started_at"].isoformat() if call["started_at"] else None,
                "outcome": call["outcome"],
                "summary": call["summary"],
            }
            if call
            else None
        )
        leads.append(lead)

    return {
        "tenant_id": tenant_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "leads": leads,
    }


class LeadStatusUpdate(BaseModel):
    status: str


@router.patch("/platform/leads/{lead_id}")
def platform_update_lead(lead_id: str, body: LeadStatusUpdate, request: Request) -> dict:
    """Update a lead's status, promoting a derived (voice-only) lead to a
    real row on first write.

    lead_id is either a real leads.thread_id, or — for a not-yet-promoted
    voice escalation — 'voice:<call id>' (see the GET handler above). Either
    way this upserts a leads row keyed on thread_id so later reads (and
    repeat PATCHes) hit the same row.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if body.status not in STATUSES:
        raise HTTPException(
            status_code=400, detail=f"status must be one of: {', '.join(STATUSES)}"
        )

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    with engine.begin() as conn:
        updated = conn.execute(
            text(
                """
                UPDATE leads SET status = :status
                WHERE thread_id = :lead_id AND tenant_id = :tenant_id
                RETURNING thread_id, contact, summary, lead_score, qualified,
                          status, last_updated
                """
            ),
            {"lead_id": lead_id, "tenant_id": tenant_id, "status": body.status},
        ).mappings().first()

        if updated is None and lead_id.startswith(_VOICE_PREFIX):
            call_id = lead_id[len(_VOICE_PREFIX):]
            try:
                uuid.UUID(call_id)
            except ValueError:
                raise HTTPException(status_code=404, detail="Lead not found")

            updated = conn.execute(
                text(
                    """
                    INSERT INTO leads (
                        thread_id, tenant_id, contact, summary, status,
                        qualified, last_updated
                    )
                    SELECT :lead_id, tenant_id, caller_e164, summary, :status,
                           TRUE, COALESCE(started_at, created_at)
                    FROM calls
                    WHERE id = :call_id AND tenant_id = :tenant_id AND outcome = 'escalated'
                    ON CONFLICT (thread_id) DO UPDATE SET status = EXCLUDED.status
                    RETURNING thread_id, contact, summary, lead_score, qualified,
                              status, last_updated
                    """
                ),
                {"lead_id": lead_id, "call_id": call_id, "tenant_id": tenant_id, "status": body.status},
            ).mappings().first()

        if updated is None:
            raise HTTPException(status_code=404, detail="Lead not found")

    lead = _row_to_lead({**updated, "source_call_id": None, "derived": False})
    return {"tenant_id": tenant_id, "lead": lead}
