# platform_api/usage.py — GET /platform/usage (Phase 3 ticket 3.1).
#
# Current-calendar-month rollup from the existing `calls` table: no new
# table, no Stripe, no plan limits — just surfacing usage that's already
# being captured by the VAPI end-of-call webhook (call_log.py). Later Phase 3
# tickets (usage_records, metered Stripe reporting, plan limits) build on top
# of this once the underlying numbers are trusted.

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import load_tenant

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/platform/usage")
def platform_usage(request: Request) -> dict:
    """Tenant usage for the current calendar month, in the tenant's own
    timezone: call count, voice minutes, VAPI cost, LLM cost.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query,
    same convention as overview.py / calls.py.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    cfg = load_tenant(tenant_id)
    try:
        tz = ZoneInfo(cfg.business_tz)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT
                    count(*) AS calls,
                    COALESCE(SUM(duration_sec), 0) AS seconds,
                    SUM(cost_vapi) AS cost_vapi,
                    SUM(cost_llm) AS cost_llm
                FROM calls
                WHERE tenant_id = :tid AND started_at >= :period_start
                """
            ),
            {"tid": tenant_id, "period_start": period_start},
        ).one()

    return {
        "tenant_id": tenant_id,
        "business_tz": cfg.business_tz,
        "period_start": period_start.date().isoformat(),
        "period_end": now.isoformat(),
        "calls": row.calls,
        "minutes": round((row.seconds or 0) / 60.0, 1),
        "cost_vapi": float(row.cost_vapi) if row.cost_vapi is not None else None,
        "cost_llm": float(row.cost_llm) if row.cost_llm is not None else None,
    }
