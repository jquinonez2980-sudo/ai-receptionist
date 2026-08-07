# platform_api/scheduling.py — GET /platform/scheduling/status (dashboard
# Scheduling page, docs/ESMI_DASHBOARD_UX.md Section 5.4).
#
# Read-only: calendar CONNECTION status for the current tenant, plus a
# read-only echo of the booking hours Settings (platform_api/config.py) is
# the actual owner of. This endpoint never writes anything — no buffers, no
# confirmation-toggle fields exist in the data model, and this file does not
# invent any.
#
# Reuses the exact credential-resolution and freebusy-probe pattern api.py's
# /health/calendar and _check_calendar_sync already use — no new OAuth
# plumbing, no new Google API surface.
#
# Isolation (tenants.py's assert_tenant_write_isolation work): this is a READ,
# so that guard doesn't apply directly, but the same rule holds in spirit —
# resolve_google_credentials(tenant_id) already refuses to fall back to the
# default/Orchelix token for a non-default tenant (raises instead), and any
# calendar_id that resolves to Orchelix's shared "primary" alias
# (_is_orchelix_shared_calendar_id) is reported as misconfigured WITHOUT ever
# being probed — a client tenant's status check must never make a live API
# call against Orchelix's own calendar.

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import TenantConfig, _is_orchelix_shared_calendar_id, load_tenant

log = logging.getLogger(__name__)

router = APIRouter()


def _hours_summary(cfg: TenantConfig) -> dict:
    """Read-only echo of what Settings (platform_api/config.py) owns.

    Same fields PUT /platform/config already accepts (business_hours,
    business_days, per-location overrides) — this never writes any of them.
    """
    out: dict = {
        "business_days": list(cfg.business_days),
        "business_hours": list(cfg.business_hours),
        "locations": None,
    }
    if cfg.locations:
        out["locations"] = {
            lid: {
                "name": loc.name,
                "business_days": list(loc.business_days),
                "business_hours": list(loc.business_hours),
                "has_day_overrides": bool(loc.day_hours),
            }
            for lid, loc in cfg.locations.items()
        }
    return out


def _freebusy_probe(service, calendar_id: str) -> None:
    """Same probe api.py's _check_calendar_sync / /health/calendar use —
    raises on any failure, caller decides what that means."""
    today = date.today().isoformat()
    service.freebusy().query(
        body={
            "timeMin": f"{today}T00:00:00Z",
            "timeMax": f"{today}T23:59:59Z",
            "timeZone": "UTC",
            "items": [{"id": calendar_id}],
        }
    ).execute()


def _calendar_statuses(tenant_id: str, cfg: TenantConfig) -> list[dict]:
    """One entry per bookable calendar (cfg.all_calendar_ids()), each
    resolved independently so a multi-location tenant with one broken
    calendar still sees the other(s) as connected."""
    try:
        from tools import resolve_google_credentials

        creds = resolve_google_credentials(tenant_id)
        credentials_detail: Optional[str] = None
    except Exception as e:
        creds = None
        credentials_detail = (
            "Calendar credentials aren't configured for this business yet."
        )
        log.info("Tenant '%s': scheduling status — no calendar credentials (%s).", tenant_id, e)

    service = None
    if creds is not None:
        try:
            from googleapiclient.discovery import build

            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            credentials_detail = "Couldn't start the calendar client."
            log.warning(
                "Tenant '%s': scheduling status — calendar client init failed (%s: %s).",
                tenant_id, type(e).__name__, e,
            )

    out = []
    for location_id, calendar_id in cfg.all_calendar_ids():
        loc = cfg.locations.get(location_id)
        entry = {
            "location_id": location_id,
            "location_name": loc.name if loc else cfg.company_name,
            "calendar_id": calendar_id or None,
            "reachable": False,
            "detail": None,
        }
        if tenant_id != "default" and _is_orchelix_shared_calendar_id(calendar_id):
            # Never probe — this alias is Orchelix's own calendar.
            entry["detail"] = (
                "This location's calendar isn't connected yet — no dedicated "
                "calendar is set up. Ask Orchelix to connect one."
            )
        elif service is None:
            entry["detail"] = credentials_detail
        else:
            try:
                _freebusy_probe(service, calendar_id)
                entry["reachable"] = True
            except Exception as e:
                entry["detail"] = "Calendar check failed — it may not be shared with Esmi."
                log.warning(
                    "Tenant '%s': scheduling status — freebusy probe failed for "
                    "location=%s calendar=%s (%s: %s).",
                    tenant_id, location_id, calendar_id, type(e).__name__, e,
                )
        out.append(entry)
    return out


@router.get("/platform/scheduling/status")
def platform_scheduling_status(request: Request) -> dict:
    """Calendar connection status + read-only hours summary for this tenant.

    Sync `def` on purpose (FastAPI threadpool) — the freebusy probe below is
    a blocking Google API call, same as every other route that touches
    Calendar (platform_api/appointments.py, tools.py).
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    cfg = load_tenant(tenant_id)
    calendars = _calendar_statuses(tenant_id, cfg)
    connected = bool(calendars) and all(c["reachable"] for c in calendars)

    detail = None
    if not connected:
        # First calendar's reason is representative for the top-level
        # summary; each entry still carries its own for a multi-location
        # tenant where only one location is broken.
        broken = next((c for c in calendars if not c["reachable"]), None)
        detail = broken["detail"] if broken else None

    return {
        "tenant_id": tenant_id,
        "connected": connected,
        "detail": detail,
        "calendars": calendars,
        "hours": _hours_summary(cfg),
    }
