# platform_api/onboarding.py — admin onboarding queue + approve-to-activate
# gate (Phase 4 ticket 4.1, stage 3).
#
#   GET   /platform/admin/onboarding                     the queue
#   GET   /platform/admin/onboarding/{tenant_id}         detail + checklist
#   PATCH /platform/admin/onboarding/{tenant_id}/steps/{step}
#   POST  /platform/admin/onboarding/{tenant_id}/approve
#   POST  /platform/admin/onboarding/{tenant_id}/reject
#
# Same two-layer access model as platform_api/admin.py: the page only renders
# for the Orchelix staff Clerk org, AND these routes require
# X-Platform-Admin-Secret (distinct from the client-facing PLATFORM_API_SECRET,
# so a leaked dashboard secret can never approve a tenant).
#
# The step PATCH is keyed by tenant_id, not job_id (which is what the original
# design sketched): the UI always has the tenant id in hand, and a tenant has
# exactly one live job, so routing through the tenant avoids making the client
# carry a uuid it never otherwise needs. The job is resolved server-side.
#
# APPROVAL is the only thing in the codebase that sets onboarding_status =
# 'active', which is what tenants.tenant_is_active() gates voice, chat and
# booking on. It is blocked until every provisioning step is resolved
# (done/skipped) so the manual VAPI / number / calendar / KB work cannot be
# silently skipped, and it clears the tenant cache inline so the gate flips
# within the request instead of at the next 60s TTL expiry.

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from platform_api import provisioning as prov
from platform_api.admin import ACCOUNT_STATUSES
from platform_api.plans import PLANS
from platform_api.security import verify_platform_admin_secret
from tenants import clear_tenant_cache, normalize_tenant_id

log = logging.getLogger(__name__)

router = APIRouter()

# Onboarding states that belong in the admin queue.
PENDING_ONBOARDING_STATUSES = ("draft", "submitted", "provisioning", "review")
# Billing status a tenant lands on when approved, unless the admin picks
# another. 'live' because approving IS the go-live decision; the admin can
# choose 'trial' in the UI when the client hasn't started paying yet.
DEFAULT_APPROVE_STATUS = "live"

_MAX_REASON_LEN = 1000
_MAX_DETAIL_VALUE_LEN = 500


class StepUpdate(BaseModel):
    status: str
    # Free-form result data merged onto the step's existing detail — e.g.
    # {"assistant_id": "..."} for vapi_assistant, {"e164": "+1..."} for
    # phone_number. Merged, not replaced, so the manual note survives.
    detail: Optional[dict] = None
    error: Optional[str] = None


class ApproveRequest(BaseModel):
    plan: str
    status: Optional[str] = None


class RejectRequest(BaseModel):
    reason: str


def _engine_or_503():
    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")
    return engine


def _norm_or_404(tenant_id: str) -> str:
    """Normalize a path tenant id, 404ing rather than silently mapping to
    'default' the way normalize_tenant_id() does on invalid input."""
    tid = normalize_tenant_id(tenant_id)
    if tid != (tenant_id or "").strip().lower():
        raise HTTPException(status_code=404, detail=f"Unknown tenant '{tenant_id}'")
    return tid


_TENANT_COLUMNS = """
    id, company_name, business_tz, status, plan, onboarding_status,
    clerk_org_id, contact_name, contact_email, contact_phone, requested_plan,
    submitted_at, approved_at, approved_by, rejected_reason, activated_at,
    created_at
"""


def _tenant_row(conn, tenant_id: str):
    from sqlalchemy import text

    row = conn.execute(
        text(f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE id = :tid"),
        {"tid": tenant_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown tenant '{tenant_id}'")
    return row


def _tenant_out(row, job: Optional[dict]) -> dict:
    def iso(v):
        return v.isoformat() if v else None

    steps = (job or {}).get("steps") or []
    return {
        "tenant_id": row["id"],
        "company_name": row["company_name"],
        "business_tz": row["business_tz"],
        "onboarding_status": row["onboarding_status"],
        "account_status": row["status"],
        "plan": row["plan"],
        "requested_plan": row["requested_plan"],
        "clerk_org_id": row["clerk_org_id"],
        "contact_name": row["contact_name"],
        "contact_email": row["contact_email"],
        "contact_phone": row["contact_phone"],
        "submitted_at": iso(row["submitted_at"]),
        "approved_at": iso(row["approved_at"]),
        "approved_by": row["approved_by"],
        "activated_at": iso(row["activated_at"]),
        "rejected_reason": row["rejected_reason"],
        "created_at": iso(row["created_at"]),
        "job": job,
        "steps_total": len(steps),
        "steps_resolved": len(steps) - len(prov.unresolved_steps(steps)),
        "unresolved_steps": prov.unresolved_steps(steps),
        # Single source of truth for the UI's Approve button. Computing it
        # here (rather than re-deriving the rule in TypeScript) keeps the
        # button and the server-side guard from ever disagreeing.
        "can_approve": bool(steps) and not prov.unresolved_steps(steps)
        and row["onboarding_status"] != "active",
    }


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/platform/admin/onboarding")
def list_onboarding(request: Request, include: str = "pending") -> dict:
    """The onboarding queue. `include=all` also returns approved/rejected
    tenants, for looking up something already actioned.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy, same
    convention as the other /platform routes.
    """
    verify_platform_admin_secret(request)
    engine = _engine_or_503()

    from sqlalchemy import text

    where = ""
    params: dict = {}
    if include != "all":
        where = "WHERE onboarding_status = ANY(:statuses)"
        params["statuses"] = list(PENDING_ONBOARDING_STATUSES)

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {_TENANT_COLUMNS} FROM tenants {where} "
                "ORDER BY submitted_at DESC NULLS LAST, created_at DESC"
            ),
            params,
        ).mappings().all()
        out = [_tenant_out(r, prov.latest_job(conn, r["id"])) for r in rows]

    return {"tenants": out, "include": include}


@router.get("/platform/admin/onboarding/{tenant_id}")
def get_onboarding(tenant_id: str, request: Request) -> dict:
    verify_platform_admin_secret(request)
    tid = _norm_or_404(tenant_id)
    engine = _engine_or_503()

    with engine.connect() as conn:
        row = _tenant_row(conn, tid)
        return _tenant_out(row, prov.latest_job(conn, tid))


@router.patch("/platform/admin/onboarding/{tenant_id}/steps/{step}")
def update_step(tenant_id: str, step: str, body: StepUpdate, request: Request) -> dict:
    """Mark a provisioning step done/skipped/failed (or back to manual).

    This is how the four manual steps get resolved: an admin does the work in
    the VAPI/Google/Stripe dashboards, then records the resulting id here.
    """
    verify_platform_admin_secret(request)
    tid = _norm_or_404(tenant_id)

    if step not in prov.STEPS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown step '{step}'. Valid: {', '.join(prov.STEPS)}",
        )
    if body.status not in prov.STEP_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(prov.STEP_STATUSES)}",
        )
    detail = body.detail or None
    if detail is not None:
        if not all(isinstance(k, str) for k in detail):
            raise HTTPException(status_code=400, detail="detail keys must be strings")
        for k, v in detail.items():
            if v is not None and len(str(v)) > _MAX_DETAIL_VALUE_LEN:
                raise HTTPException(
                    status_code=400, detail=f"detail.{k} is too long"
                )

    engine = _engine_or_503()
    changed_by = request.headers.get("X-Platform-User", "dashboard")

    with engine.begin() as conn:
        _tenant_row(conn, tid)
        job = prov.latest_job(conn, tid)
        if job is None:
            raise HTTPException(
                status_code=409, detail=f"Tenant '{tid}' has no provisioning job."
            )
        prov.set_step(
            conn, job["job_id"], step, body.status,
            detail=detail,
            error=(body.error or None) and body.error[:_MAX_REASON_LEN],
            updated_by=changed_by,
        )
        prov.recompute_job_status(conn, job["job_id"])
        row = _tenant_row(conn, tid)
        out = _tenant_out(row, prov.latest_job(conn, tid))

    log.info(
        "Provisioning step updated: tenant=%s step=%s status=%s by=%s",
        tid, step, body.status, changed_by,
    )
    return out


@router.post("/platform/admin/onboarding/{tenant_id}/approve")
def approve_tenant(tenant_id: str, body: ApproveRequest, request: Request) -> dict:
    """Approve a tenant and take it live.

    The only path that sets onboarding_status='active'. Blocked unless every
    provisioning step is resolved — the manual VAPI / number / calendar / KB
    work is exactly what this gate exists to make un-skippable.
    """
    verify_platform_admin_secret(request)
    tid = _norm_or_404(tenant_id)

    if body.plan not in PLANS:
        raise HTTPException(
            status_code=400, detail=f"plan must be one of: {', '.join(sorted(PLANS))}"
        )
    new_status = body.status or DEFAULT_APPROVE_STATUS
    if new_status not in ACCOUNT_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(ACCOUNT_STATUSES)}",
        )

    engine = _engine_or_503()
    approved_by = request.headers.get("X-Platform-User", "dashboard")

    from sqlalchemy import text

    with engine.begin() as conn:
        row = _tenant_row(conn, tid)
        if row["onboarding_status"] == "active":
            raise HTTPException(
                status_code=409, detail=f"Tenant '{tid}' is already active."
            )

        job = prov.latest_job(conn, tid)
        if job is None:
            raise HTTPException(
                status_code=409, detail=f"Tenant '{tid}' has no provisioning job."
            )
        outstanding = prov.unresolved_steps(job["steps"])
        if outstanding:
            labels = ", ".join(prov.STEP_LABELS.get(s, s) for s in outstanding)
            raise HTTPException(
                status_code=409,
                detail=f"Cannot approve — these provisioning steps are unresolved: {labels}.",
            )

        if row["onboarding_status"] == "rejected":
            # Allowed on purpose: a mis-click on Reject must be recoverable.
            log.warning("Tenant %s is being approved after a prior rejection.", tid)

        old_plan, old_status = row["plan"], row["status"]

        conn.execute(
            text(
                """
                UPDATE tenants SET
                    onboarding_status = 'active',
                    plan            = :plan,
                    status          = :status,
                    approved_at     = now(),
                    approved_by     = :by,
                    activated_at    = COALESCE(activated_at, now()),
                    rejected_reason = NULL
                WHERE id = :tid
                """
            ),
            {"plan": body.plan, "status": new_status, "by": approved_by, "tid": tid},
        )
        # Same audit table platform_api/admin.py writes for manual plan edits,
        # so approval-time plan assignment shows up in one timeline with every
        # later change rather than in a parallel Phase-4-only log.
        conn.execute(
            text(
                """
                INSERT INTO tenant_plan_changes
                    (tenant_id, old_plan, new_plan, old_status, new_status, changed_by)
                VALUES (:tid, :old_plan, :new_plan, :old_status, :new_status, :by)
                """
            ),
            {
                "tid": tid,
                "old_plan": old_plan,
                "new_plan": body.plan,
                "old_status": old_status,
                "new_status": new_status,
                "by": f"approve:{approved_by}",
            },
        )
        prov.recompute_job_status(conn, job["job_id"])
        out = _tenant_out(_tenant_row(conn, tid), prov.latest_job(conn, tid))

    # Flip the gate NOW rather than up to 60s from now — tenant_is_active()
    # caches onboarding_status, and an admin who clicks Approve then
    # immediately places a test call must reach the tenant, not 'default'.
    clear_tenant_cache(tid)

    log.info(
        "Tenant APPROVED: tenant=%s plan=%s->%s status=%s->%s by=%s",
        tid, old_plan, body.plan, old_status, new_status, approved_by,
    )
    return out


@router.post("/platform/admin/onboarding/{tenant_id}/reject")
def reject_tenant(tenant_id: str, body: RejectRequest, request: Request) -> dict:
    """Decline a pending tenant, recording why.

    The row and its slug are KEPT (the approved decision): the slug stays
    claimed so it can't be silently re-registered by someone else, and the
    record of the decision survives.

    `status` (the billing lifecycle) is deliberately left alone — a rejected
    signup never billed anything, and overloading 'archived' here would make
    the billing timeline lie. onboarding_status is what gates traffic.
    """
    verify_platform_admin_secret(request)
    tid = _norm_or_404(tenant_id)

    reason = (body.reason or "").strip()
    if not reason or len(reason) > _MAX_REASON_LEN:
        raise HTTPException(
            status_code=400, detail=f"reason must be 1-{_MAX_REASON_LEN} characters"
        )

    engine = _engine_or_503()
    rejected_by = request.headers.get("X-Platform-User", "dashboard")

    from sqlalchemy import text

    with engine.begin() as conn:
        row = _tenant_row(conn, tid)
        if row["onboarding_status"] == "active":
            raise HTTPException(
                status_code=409,
                detail=f"Tenant '{tid}' is already active — suspend it from the "
                "Tenants admin page instead of rejecting it here.",
            )
        conn.execute(
            text(
                "UPDATE tenants SET onboarding_status = 'rejected', "
                "rejected_reason = :reason WHERE id = :tid"
            ),
            {"reason": reason, "tid": tid},
        )
        out = _tenant_out(_tenant_row(conn, tid), prov.latest_job(conn, tid))

    clear_tenant_cache(tid)
    log.info("Tenant REJECTED: tenant=%s by=%s reason=%s", tid, rejected_by, reason)
    return out
