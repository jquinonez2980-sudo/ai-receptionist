# platform_api/admin.py — internal-only tenant plan/status/Stripe assignment
# (Phase 3 tickets 3.5 + 3.6). GET /platform/admin/tenants, PATCH .../plan,
# PATCH .../stripe.
#
# Access model (two independent layers — see platform_api/security.py):
#   1. The frontend page only renders for a signed-in Orchelix staff member
#      (active Clerk org slug == "default").
#   2. These routes additionally require X-Platform-Admin-Secret, a secret
#      DISTINCT from the client-facing PLATFORM_API_SECRET, so a leaked
#      client-dashboard secret can never reach admin actions.
#
# Every plan/status write is recorded in tenant_plan_changes (insert-only —
# see alembic 0004) before returning, mirroring how tenant_configs already
# tracks created_by/created_at for config edits. Stripe ID changes (ticket
# 3.6) are logged (log.info below) rather than given their own audit table —
# smallest useful slice; add one later if a real audit trail is needed.
# Stripe IDs are identifiers only, pasted in from the Stripe Dashboard —
# this app makes no live Stripe API calls anywhere.

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from platform_api.plans import PLANS
from platform_api.security import verify_platform_admin_secret
from platform_api.usage import compute_tenant_usage
from tenants import clear_tenant_cache, tenant_exists

log = logging.getLogger(__name__)

router = APIRouter()

ACCOUNT_STATUSES = ("trial", "live", "past_due", "suspended", "archived")


class PlanUpdate(BaseModel):
    plan: str
    status: str | None = None


class StripeUpdate(BaseModel):
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None


def _tenant_admin_row(tenant_id: str, data: dict) -> dict:
    """Shared response shape for the list endpoint and both PATCH endpoints."""
    return {
        "tenant_id": tenant_id,
        "account_status": data["account_status"],
        "calls": data["calls"],
        "minutes": data["minutes"],
        "period_start": data["period_start"],
        "period_end": data["period_end"],
        "plan": data["plan"],
        "stripe_customer_id": data["stripe_customer_id"],
        "stripe_subscription_id": data["stripe_subscription_id"],
        "billing_mode": data["billing_mode"],
    }


@router.get("/platform/admin/tenants")
def list_admin_tenants(request: Request) -> dict:
    """Every tenant in the registry with its plan/status, Stripe linkage,
    and this month's usage summary. Sync `def` on purpose — blocking
    SQLAlchemy queries, same convention as the other /platform/* read routes.
    """
    verify_platform_admin_secret(request)

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id FROM tenants ORDER BY id")
        ).all()

    tenants_out = [
        _tenant_admin_row(tenant_id, compute_tenant_usage(tenant_id))
        for (tenant_id,) in rows
    ]
    return {"tenants": tenants_out}


@router.patch("/platform/admin/tenants/{tenant_id}/plan")
def update_tenant_plan(tenant_id: str, body: PlanUpdate, request: Request) -> dict:
    """Assign a tenant's plan (and optionally account status). Validates both
    against the known value sets, records an audit row, then returns the
    updated tenant + usage summary (same shape as the list endpoint's rows).
    """
    verify_platform_admin_secret(request)

    if not tenant_exists(tenant_id):
        raise HTTPException(status_code=404, detail=f"Unknown tenant '{tenant_id}'")
    if body.plan not in PLANS:
        raise HTTPException(
            status_code=400,
            detail=f"plan must be one of: {', '.join(sorted(PLANS))}",
        )
    if body.status is not None and body.status not in ACCOUNT_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(ACCOUNT_STATUSES)}",
        )

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    changed_by = request.headers.get("X-Platform-User", "dashboard")

    with engine.begin() as conn:
        # ON CONFLICT DO UPDATE (not a bare UPDATE) so admin-assigning a plan
        # doubles as tenant registration — a tenant that has never logged a
        # call yet (no row via call_log.py's upsert) can still be plan-
        # assigned in advance of go-live.
        current = conn.execute(
            text("SELECT plan, status FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        ).first()
        old_plan = current[0] if current else None
        old_status = current[1] if current else None
        new_status = body.status or old_status or "live"

        conn.execute(
            text(
                """
                INSERT INTO tenants (id, plan, status)
                VALUES (:tid, :plan, :status)
                ON CONFLICT (id) DO UPDATE SET plan = :plan, status = :status
                """
            ),
            {"tid": tenant_id, "plan": body.plan, "status": new_status},
        )
        conn.execute(
            text(
                """
                INSERT INTO tenant_plan_changes
                    (tenant_id, old_plan, new_plan, old_status, new_status, changed_by)
                VALUES (:tid, :old_plan, :new_plan, :old_status, :new_status, :changed_by)
                """
            ),
            {
                "tid": tenant_id,
                "old_plan": old_plan,
                "new_plan": body.plan,
                "old_status": old_status,
                "new_status": new_status,
                "changed_by": changed_by,
            },
        )

    # account_status is half the traffic gate (tenants.BLOCKING_ACCOUNT_STATUSES),
    # so this write can take a tenant off the air — it has to land NOW, not at
    # the next 60s TTL expiry. Without this, suspending a tenant would leave it
    # answering calls for up to a minute while the dashboard already said
    # "suspended". Mirrors the same clear in onboarding.py's approve path.
    clear_tenant_cache(tenant_id)

    log.info(
        "Tenant plan changed: tenant=%s %s->%s status=%s->%s by=%s",
        tenant_id, old_plan, body.plan, old_status, new_status, changed_by,
    )

    return _tenant_admin_row(tenant_id, compute_tenant_usage(tenant_id))


@router.patch("/platform/admin/tenants/{tenant_id}/stripe")
def update_tenant_stripe(tenant_id: str, body: StripeUpdate, request: Request) -> dict:
    """Set or clear a tenant's Stripe customer/subscription id.

    Light format validation only (must start with 'cus_' / 'sub_') — no live
    Stripe API call, this app has no Stripe secret key. A field omitted from
    the request body is left unchanged; a field explicitly sent as null
    clears it (distinguished via body.model_fields_set, not just truthiness).
    """
    verify_platform_admin_secret(request)

    if not tenant_exists(tenant_id):
        raise HTTPException(status_code=404, detail=f"Unknown tenant '{tenant_id}'")

    fields_set = body.model_fields_set
    if "stripe_customer_id" in fields_set and body.stripe_customer_id:
        if not body.stripe_customer_id.startswith("cus_"):
            raise HTTPException(
                status_code=400, detail="stripe_customer_id must start with 'cus_'"
            )
    if "stripe_subscription_id" in fields_set and body.stripe_subscription_id:
        if not body.stripe_subscription_id.startswith("sub_"):
            raise HTTPException(
                status_code=400, detail="stripe_subscription_id must start with 'sub_'"
            )

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    changed_by = request.headers.get("X-Platform-User", "dashboard")

    with engine.begin() as conn:
        current = conn.execute(
            text(
                "SELECT stripe_customer_id, stripe_subscription_id FROM tenants WHERE id = :tid"
            ),
            {"tid": tenant_id},
        ).first()
        old_customer_id = current[0] if current else None
        old_subscription_id = current[1] if current else None

        new_customer_id = (
            body.stripe_customer_id if "stripe_customer_id" in fields_set else old_customer_id
        )
        new_subscription_id = (
            body.stripe_subscription_id
            if "stripe_subscription_id" in fields_set
            else old_subscription_id
        )

        # ON CONFLICT DO UPDATE for the same reason update_tenant_plan uses
        # it — this can be the first admin write for a tenant with no
        # `tenants` row yet (plan/status fall back to their column defaults).
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, stripe_customer_id, stripe_subscription_id)
                VALUES (:tid, :customer_id, :subscription_id)
                ON CONFLICT (id) DO UPDATE
                    SET stripe_customer_id = :customer_id,
                        stripe_subscription_id = :subscription_id
                """
            ),
            {
                "tid": tenant_id,
                "customer_id": new_customer_id,
                "subscription_id": new_subscription_id,
            },
        )

    log.info(
        "Tenant Stripe IDs changed: tenant=%s customer=%s->%s subscription=%s->%s by=%s",
        tenant_id, old_customer_id, new_customer_id,
        old_subscription_id, new_subscription_id, changed_by,
    )

    return _tenant_admin_row(tenant_id, compute_tenant_usage(tenant_id))
