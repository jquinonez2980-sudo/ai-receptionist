# platform_api/usage.py — GET /platform/usage (Phase 3 tickets 3.1 + 3.2).
#
# Current-calendar-month rollup from the existing `calls` table: no new
# table, no Stripe — just surfacing usage that's already being captured by
# the VAPI end-of-call webhook (call_log.py). Ticket 3.2 adds the tenant's
# plan (tenants.plan, already in the schema but unread until now) and a SOFT
# usage-vs-limit status (platform_api/plans.py) — display only, nothing here
# blocks a call. compute_tenant_usage() is shared with platform_api/billing.py
# (ticket 3.3) and platform_api/admin.py so all three run one query instead
# of several. Ticket 3.6 adds tenants.stripe_customer_id/subscription_id
# (identifiers only — no Stripe API calls anywhere in this app) and derives
# billing_mode from whether a subscription id is set. Later Phase 3 tickets
# (usage_records, metered Stripe reporting, hard enforcement) build on top
# of this once the underlying numbers are trusted.

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from platform_api.plans import get_plan, usage_status
from platform_api.security import require_tenant, verify_platform_secret
from tenants import load_tenant

log = logging.getLogger(__name__)

router = APIRouter()


def compute_tenant_usage(tenant_id: str) -> dict:
    """Current-calendar-month usage + plan status for one tenant, in its own
    timezone: call count, voice minutes, VAPI cost, LLM cost, plan/limit
    status, and the account status (tenants.status).

    Raises HTTPException(503) if the platform DB isn't configured — callers
    are FastAPI route handlers, so letting it propagate is correct.
    """
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
        # No row (tenant not yet created in `tenants`, e.g. never had a VAPI
        # call land) falls back to the `managed`/unlimited plan and a "live"
        # account status below — same as an explicit but unrecognized value.
        tenant_row = conn.execute(
            text(
                "SELECT plan, status, stripe_customer_id, stripe_subscription_id "
                "FROM tenants WHERE id = :tid"
            ),
            {"tid": tenant_id},
        ).first()

    minutes = round((row.seconds or 0) / 60.0, 1)
    plan = get_plan(tenant_row[0] if tenant_row else None)
    account_status = (tenant_row[1] if tenant_row else None) or "live"
    stripe_customer_id = tenant_row[2] if tenant_row else None
    stripe_subscription_id = tenant_row[3] if tenant_row else None
    # "stripe" only once a subscription actually exists — linking a Stripe
    # customer record during onboarding (e.g. before checkout completes)
    # must not flip billing_mode before there's a real subscription behind it.
    billing_mode = "stripe" if stripe_subscription_id else "managed"

    return {
        "business_tz": cfg.business_tz,
        "period_start": period_start.date().isoformat(),
        "period_end": now.isoformat(),
        "calls": row.calls,
        "minutes": minutes,
        "cost_vapi": float(row.cost_vapi) if row.cost_vapi is not None else None,
        "cost_llm": float(row.cost_llm) if row.cost_llm is not None else None,
        "account_status": account_status,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "billing_mode": billing_mode,
        "plan": {
            "key": plan.key,
            "label": plan.label,
            **usage_status(minutes, plan),
        },
    }


@router.get("/platform/usage")
def platform_usage(request: Request) -> dict:
    """Tenant usage for the current calendar month: call count, voice
    minutes, VAPI cost, LLM cost, plan + soft-limit status.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query,
    same convention as overview.py / calls.py.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    data = compute_tenant_usage(tenant_id)
    return {
        "tenant_id": tenant_id,
        "business_tz": data["business_tz"],
        "period_start": data["period_start"],
        "period_end": data["period_end"],
        "calls": data["calls"],
        "minutes": data["minutes"],
        "cost_vapi": data["cost_vapi"],
        "cost_llm": data["cost_llm"],
        "plan": data["plan"],
    }
