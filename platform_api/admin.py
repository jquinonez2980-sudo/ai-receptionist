# platform_api/admin.py — internal-only tenant plan/status assignment
# (Phase 3 ticket 3.5). GET /platform/admin/tenants + PATCH .../plan.
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
# tracks created_by/created_at for config edits.

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from platform_api.plans import PLANS
from platform_api.security import verify_platform_admin_secret
from platform_api.usage import compute_tenant_usage
from tenants import tenant_exists

log = logging.getLogger(__name__)

router = APIRouter()

ACCOUNT_STATUSES = ("trial", "live", "past_due", "suspended", "archived")


class PlanUpdate(BaseModel):
    plan: str
    status: str | None = None


@router.get("/platform/admin/tenants")
def list_admin_tenants(request: Request) -> dict:
    """Every tenant in the registry with its plan/status and this month's
    usage summary. Sync `def` on purpose — blocking SQLAlchemy queries,
    same convention as the other /platform/* read routes.
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

    tenants_out = []
    for (tenant_id,) in rows:
        data = compute_tenant_usage(tenant_id)
        tenants_out.append(
            {
                "tenant_id": tenant_id,
                "account_status": data["account_status"],
                "calls": data["calls"],
                "minutes": data["minutes"],
                "period_start": data["period_start"],
                "period_end": data["period_end"],
                "plan": data["plan"],
            }
        )
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

    log.info(
        "Tenant plan changed: tenant=%s %s->%s status=%s->%s by=%s",
        tenant_id, old_plan, body.plan, old_status, new_status, changed_by,
    )

    data = compute_tenant_usage(tenant_id)
    return {
        "tenant_id": tenant_id,
        "account_status": data["account_status"],
        "calls": data["calls"],
        "minutes": data["minutes"],
        "period_start": data["period_start"],
        "period_end": data["period_end"],
        "plan": data["plan"],
    }
