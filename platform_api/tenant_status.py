# platform_api/tenant_status.py — GET /platform/tenant-status (Phase 4 ticket
# 4.1, stage A). Tiny client-facing read that tells the dashboard shell whether
# this tenant is actually live, so a pending tenant sees a persistent
# "not live yet" banner instead of a dashboard that looks fully operational.
#
# Its own module rather than a route inside onboarding.py: every route in that
# file is gated on verify_platform_admin_secret, and dropping a
# verify_platform_secret route in among them is exactly the kind of
# neighborhood where someone later copies the wrong verify call. One file, one
# auth model.
#
# can_serve_traffic is computed by calling tenants.tenant_is_active() — the
# SAME function api.py's _resolve_tenant / _resolve_tenant_strict and
# tenants.resolve_vapi_tenant gate real traffic on. Deliberately not
# re-derived from onboarding_status here: if the banner and the runtime gate
# ever disagreed, the dashboard would be lying about whether the phone works.

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import tenant_is_active, tenant_onboarding_status

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/platform/tenant-status")
def platform_tenant_status(request: Request) -> dict:
    """Account + onboarding state for the dashboard shell's banner.

    Sync `def` on purpose (FastAPI threadpool) — tenant_is_active() may do a
    blocking DB read on cache miss, same convention as the other /platform
    routes. Both values it reads share the 60s tenant cache, so this is
    normally zero queries.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    onboarding_status = tenant_onboarding_status(tenant_id)
    can_serve = tenant_is_active(tenant_id)

    account_status = None
    plan = None
    from platform_db import get_engine

    engine = get_engine()
    if engine is not None:
        try:
            from sqlalchemy import text

            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT status, plan FROM tenants WHERE id = :tid"),
                    {"tid": tenant_id},
                ).first()
            if row is not None:
                account_status, plan = row[0], row[1]
        except Exception as e:
            # Non-fatal: the banner only needs onboarding_status /
            # can_serve_traffic, and those came from the cached tenant lookup
            # above. A DB blip must not break the dashboard shell.
            log.warning(
                "tenant-status: plan/status lookup failed for %s (%s: %s).",
                tenant_id, type(e).__name__, e,
            )

    return {
        "tenant_id": tenant_id,
        "onboarding_status": onboarding_status,
        "can_serve_traffic": can_serve,
        "account_status": account_status,
        "plan": plan,
    }
