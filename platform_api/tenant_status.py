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
from tenants import TenantState, tenant_is_active, tenant_state

log = logging.getLogger(__name__)

router = APIRouter()

# Stand-in so the plan lookup below doesn't need its own None branch.
_NO_STATE = TenantState(
    onboarding_status="", account_status="", plan=None, onboarding_voice_previewed_at=None
)


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

    # Exactly two lookups, both normally served from the 60s cache. This used
    # to fire a SECOND SELECT for status/plan on every dashboard page load;
    # those columns now travel with onboarding_status in one cached row.
    #
    # Kept to two on purpose: _UNAVAILABLE is deliberately never cached, so
    # during a DB outage every lookup is a real connection attempt against a
    # 5s connect_timeout. Reading each field through its own accessor would
    # turn one page load into four serial timeouts.
    state = tenant_state(tenant_id) or _NO_STATE

    return {
        "tenant_id": tenant_id,
        "onboarding_status": state.onboarding_status or None,
        "account_status": state.account_status or None,
        # Always from tenant_is_active() — the same function the voice, chat
        # and booking gates call. Never re-derived from the two fields above,
        # or the banner could disagree with what the phone actually does.
        "can_serve_traffic": tenant_is_active(tenant_id),
        # No fallback: a tenant with no row genuinely has no assigned plan.
        "plan": state.plan,
        # Onboarding voice gate (docs/ESMI_DASHBOARD_UX.md Section 7 Step 3) —
        # set server-side by POST /platform/voice/preview on its first 200 for
        # this tenant, never by the frontend directly.
        "onboarding_voice_previewed": state.onboarding_voice_previewed_at is not None,
    }
