# platform_api/billing.py — GET /platform/billing (Phase 3 ticket 3.3).
#
# Reuses compute_tenant_usage() (platform_api/usage.py) for the same
# calls-table + plan-status computation, then adds account_status
# (tenants.status — already in the schema, unread until now) and a constant
# billing_mode: no per-tenant Stripe customer/subscription linkage exists yet
# (see PLATFORM_BLUEPRINT.md), so "managed" (billed manually) is the only
# honest value today. Deliberately omits cost_vapi/cost_llm: those are
# Orchelix's own internal costs, not the tenant's bill, and don't belong on
# a client-facing billing page even though the Usage page already shows them
# (labeled) as an FYI estimate.

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from platform_api.security import require_tenant, verify_platform_secret
from platform_api.usage import compute_tenant_usage

log = logging.getLogger(__name__)

router = APIRouter()

BILLING_MODE = "managed"


@router.get("/platform/billing")
def platform_billing(request: Request) -> dict:
    """Tenant billing snapshot: account status, plan, usage vs limit.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query
    inside compute_tenant_usage(), same convention as the other /platform/*
    read routes.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)
    data = compute_tenant_usage(tenant_id)
    return {
        "tenant_id": tenant_id,
        "account_status": data["account_status"],
        "billing_mode": BILLING_MODE,
        "period_start": data["period_start"],
        "period_end": data["period_end"],
        "calls": data["calls"],
        "minutes": data["minutes"],
        "plan": data["plan"],
    }
