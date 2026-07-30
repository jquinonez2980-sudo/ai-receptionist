# platform_api/billing.py — GET /platform/billing (Phase 3 tickets 3.3 + 3.6).
#
# Reuses compute_tenant_usage() (platform_api/usage.py) for the same
# calls-table + plan-status computation, then adds account_status
# (tenants.status — already in the schema, unread until now) and billing_mode
# — "stripe" once a tenant has a real Stripe subscription linked (ticket
# 3.6), "managed" otherwise. Deliberately omits cost_vapi/cost_llm (Orchelix's
# own internal costs, not the tenant's bill — the Usage page shows those,
# labeled, as an FYI estimate) AND the raw stripe_customer_id/
# stripe_subscription_id: those aren't secrets, but this is a client-facing
# endpoint and the IDs have no use to a tenant — only /platform/admin/*
# (staff-only, separate admin secret) returns them.

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from platform_api.security import require_tenant, verify_platform_secret
from platform_api.usage import compute_tenant_usage

log = logging.getLogger(__name__)

router = APIRouter()


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
        "billing_mode": data["billing_mode"],
        "period_start": data["period_start"],
        "period_end": data["period_end"],
        "calls": data["calls"],
        "minutes": data["minutes"],
        "plan": data["plan"],
    }
