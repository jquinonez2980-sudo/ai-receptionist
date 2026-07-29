# platform_api/appointments.py — GET /platform/appointments (dashboard).
#
# DATA SOURCE NOTE (per Ticket 5's "prefer the appointments table"):
# The blueprint's `appointments` DB table does not exist yet — migration 0001
# was deliberately scoped to tenants/tenant_configs/calls/chat_sessions, and
# nothing writes appointment rows today. The authoritative store for every
# booking Esmi makes (voice tools, web chat, AND the website /bookings REST)
# is Google Calendar: book_appointment_core() stamps each event with
# extendedProperties.private {tenant_id, location_id, service_id, source} and
# a structured description ("Source:/Service:/Name:/Caller contact:").
# Reading the tenant's calendars therefore returns COMPLETE, current data —
# including reschedules and cancellations, which a derived table would miss.
#
# The calls-derived fallback (outcome='booked') was considered and rejected:
# a booked call row records when the CALL happened, not when the APPOINTMENT
# is — there is no slot datetime to show. Phase 2 dual-writes an appointments
# table at booking time; this endpoint keeps its response shape so only the
# internals swap.

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import TenantConfig, load_tenant

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_LIMIT = 200
_PAST_DAYS = 90     # how far back the "past" list reaches
_FUTURE_DAYS = 180  # how far forward we look

_DESC_FIELD = re.compile(r"^(Source|Service|Name|Caller contact)\s*:\s*(.+)$", re.M)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _parse_event(event: dict, cfg: TenantConfig, location_id: str) -> Optional[dict]:
    """Normalize one Google Calendar event into an appointment row.

    Best-effort: Esmi-booked events carry structured props/description;
    manually-added events on the same calendar still show with whatever the
    summary offers (they ARE the tenant's appointments too).
    """
    start_raw = (event.get("start") or {}).get("dateTime")
    end_raw = (event.get("end") or {}).get("dateTime")
    if not start_raw:
        return None  # all-day events (birthdays, closures) are not appointments

    props = (event.get("extendedProperties") or {}).get("private") or {}
    desc_fields = {m.group(1): m.group(2).strip() for m in _DESC_FIELD.finditer(event.get("description") or "")}

    summary = (event.get("summary") or "").strip()
    # book_appointment_core writes "Service — Name" summaries
    name = desc_fields.get("Name") or (summary.split("—", 1)[1].strip() if "—" in summary else "")

    service = desc_fields.get("Service") or ""
    if not service and props.get("service_id"):
        svc = cfg.services.get(props["service_id"])
        service = svc.name if svc else props["service_id"]
    if not service and "—" in summary:
        service = summary.split("—", 1)[0].strip()

    attendees = event.get("attendees") or []
    email = next((a.get("email") for a in attendees if a.get("email")), None)
    contact_raw = desc_fields.get("Caller contact") or ""
    phone = contact_raw if contact_raw and not _EMAIL_RE.match(contact_raw) else None
    if not email and contact_raw and _EMAIL_RE.match(contact_raw):
        email = contact_raw

    loc_id = props.get("location_id") or location_id
    loc_name = loc_id
    if loc_id in cfg.locations:
        loc_name = cfg.locations[loc_id].name
    elif cfg.locations:
        pass  # keep raw id
    else:
        loc_name = cfg.default_location().name

    return {
        "id": event.get("id"),
        "starts_at": start_raw,
        "ends_at": end_raw,
        "customer_name": name or summary or "Unknown",
        "contact_phone": phone,
        "contact_email": email,
        "service": service or None,
        "location": loc_name,
        "source": (props.get("source") or desc_fields.get("Source") or "").lower() or None,
        "esmi_booked": bool(props.get("tenant_id")),
    }


def _matches(appt: dict, needle: str) -> bool:
    if not needle:
        return True
    n = needle.strip().lower()
    if n in (appt["customer_name"] or "").lower():
        return True
    nd = _digits(n)
    return bool(nd) and nd in _digits(appt["contact_phone"] or "")


@router.get("/platform/appointments")
def platform_appointments(
    request: Request,
    status: str = "all",
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Tenant appointments from the tenant's Google Calendar(s).

    status: upcoming | past | all (all = upcoming soonest-first, then past
    most-recent-first — the order the dashboard renders).
    Sync `def` on purpose: googleapiclient is blocking; FastAPI threadpools it.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if status not in ("upcoming", "past", "all"):
        raise HTTPException(status_code=400, detail="status must be upcoming, past, or all")
    limit = max(1, min(int(limit), _MAX_LIMIT))
    offset = max(0, int(offset))

    from tools import _get_calendar_service  # late import: heavy module, already loaded in-process

    cfg = load_tenant(tenant_id)
    try:
        cal_service = _get_calendar_service(tenant_id)
    except Exception as e:
        log.warning("Tenant '%s': calendar unavailable for /platform/appointments (%s)", tenant_id, e)
        raise HTTPException(status_code=503, detail="Calendar is not configured for this business.")

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=_PAST_DAYS)).isoformat()
    time_max = (now + timedelta(days=_FUTURE_DAYS)).isoformat()

    appts: list[dict] = []
    for loc_id, cal_id in cfg.all_calendar_ids():
        try:
            resp = (
                cal_service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=2500,
                )
                .execute()
            )
        except Exception as e:
            log.warning("Tenant '%s': listing calendar %s failed (%s)", tenant_id, loc_id, e)
            continue
        for event in resp.get("items", []):
            row = _parse_event(event, cfg, loc_id)
            if row:
                appts.append(row)

    for a in appts:
        try:
            starts = datetime.fromisoformat(a["starts_at"])
        except ValueError:
            starts = now
        a["_starts"] = starts
        a["status"] = "upcoming" if starts >= now else "past"

    needle = (search or "").strip()
    appts = [a for a in appts if _matches(a, needle)]
    if status != "all":
        appts = [a for a in appts if a["status"] == status]

    upcoming = sorted((a for a in appts if a["status"] == "upcoming"), key=lambda a: a["_starts"])
    past = sorted((a for a in appts if a["status"] == "past"), key=lambda a: a["_starts"], reverse=True)
    ordered = upcoming + past

    total = len(ordered)
    page = ordered[offset : offset + limit]
    for a in page:
        a.pop("_starts", None)

    return {
        "tenant_id": tenant_id,
        "total": total,
        "upcoming_count": len(upcoming),
        "limit": limit,
        "offset": offset,
        "appointments": page,
    }
