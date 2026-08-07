# platform_api/config.py — GET/PUT /platform/config (self-serve Business
# Profile + Hours + Services + Greeting, PLATFORM_BLUEPRINT.md Phase 2).
#
# GET returns the safe, editable subset of the tenant's current config,
# sourced from tenants.load_tenant() (same 60s-cached, DB-first-then-file
# resolution every other /platform/* route already relies on).
#
# PUT validates the request against an explicit allow-list of fields, merges
# it onto the tenant's current RAW config JSON (so untouched fields — vapi
# ids, calendar_id, sms_templates, pricing cards, slot_minutes, business_tz —
# survive byte-for-byte), and appends a new version to tenant_configs with
# published=true. tenant_configs is append-only (see alembic 0001 + tenants.py
# load_tenant): the highest published version always wins, so this never
# needs to touch or unpublish older rows — that is what "keep full version
# history" means here.
#
# Deliberately NOT editable (CLAUDE.md hard rules + PLATFORM_BLUEPRINT.md
# "Deliberately NOT tenant-editable"): model, temperature, raw prompts, tool
# wiring, vapi assistant/phone ids, calendar_id, Orchelix's own SaaS _PRICING
# (tools.py — that is a different concept from a tenant's own service prices
# here), and the sender ("from") notification email address (tied to SendGrid
# domain verification, not something a dashboard edit should be able to break).
#
# Locations: a PUT may only EDIT an existing location's non-wiring fields
# (name, address, phone, hours) — it can never add or remove a location,
# because a new location has no calendar_id and calendar wiring is not
# self-serve. Services have no such wiring dependency, so a PUT may freely
# add, edit, or remove services.
#
# business_tz IS editable, but it is the highest-consequence field here and the
# UI treats it as such. It is not a display string: tools.py reads it to
# compute availability (1163), to guard against double-booking (1425) and as
# the timeZone written onto every Google Calendar event (1482); api.py:938 uses
# it for booking end-time math and overview.py:117 for after-hours bucketing.
# Because business_hours are stored as CLOCK TIMES, changing the zone
# reinterprets them — 9-17 means something different afterwards. Already-booked
# calendar events keep their absolute times and do not move, so a mid-week
# change leaves old and new appointments following different rules. The
# dashboard requires an explicit confirmation before saving one; see the amber
# panel in app/dashboard/settings/SettingsForm.tsx.

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from platform_api.security import require_tenant, verify_platform_secret
from tenants import LocationConfig, ServiceConfig, TenantConfig, clear_tenant_cache, load_tenant

log = logging.getLogger(__name__)

router = APIRouter()

_MAX_GREETING_LEN = 500
_MAX_NAME_LEN = 200
_MAX_VERSIONS = 100

# Voice Studio field constraints (docs/ESMI_DASHBOARD_UX.md Section 3.4).
# _VOICE_SPEED_MIN/MAX match the dashboard slider's 0.85x-1.15x range exactly
# — keep these in sync if that range ever changes.
_VOICE_SPEED_MIN = 0.85
_VOICE_SPEED_MAX = 1.15
_LANGUAGE_PREFS = {"auto", "en", "es"}
_MAX_VOICE_ID_LEN = 64

# Top-level config.json keys the version-history diff considers — the same
# allow-list PUT /platform/config writes to. A human-readable label for each,
# used to build the one-line "what changed" summary.
_DIFF_LABELS = {
    "company_name": "business name",
    "business_tz": "timezone",
    "greeting": "greeting",
    "transfer_phone": "transfer number",
    "business_hours": "hours",
    "business_days": "days open",
    "emails": "notification emails",
    "locations": "locations/hours",
    "services": "services",
    "voice_id": "voice",
    "speed": "speech speed",
    "language_pref": "language preference",
}


# ── request models (the safe, self-serve subset only) ────────────────────────


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    business_hours: Optional[list[int]] = None  # [open_hour, close_hour)
    business_days: Optional[list[int]] = None  # Mon=0 ... Sun=6
    booking_days: Optional[list[int]] = None
    day_hours: Optional[dict[str, list[int]]] = None  # {"0": [10, 19], ...}


class ServiceUpdate(BaseModel):
    name: str
    duration_min: int = Field(gt=0, le=480)
    price: str = ""
    price_by_location: dict[str, str] = Field(default_factory=dict)
    name_es: str = ""


class EmailsUpdate(BaseModel):
    booking_to: Optional[str] = None
    escalation_to: Optional[str] = None


class ConfigUpdate(BaseModel):
    company_name: Optional[str] = None
    business_tz: Optional[str] = None
    greeting: Optional[str] = None
    transfer_phone: Optional[str] = None
    business_hours: Optional[list[int]] = None
    business_days: Optional[list[int]] = None
    locations: Optional[dict[str, LocationUpdate]] = None
    services: Optional[dict[str, ServiceUpdate]] = None
    emails: Optional[EmailsUpdate] = None
    # Voice Studio (docs/ESMI_DASHBOARD_UX.md Section 3). Saving these today
    # only changes what this endpoint returns — there is no VAPI sync yet, so
    # it does not change what live callers hear. See tenants.py's voice_id
    # field comment before wiring a "Save" button in the UI to this.
    voice_id: Optional[str] = None
    speed: Optional[float] = None
    language_pref: Optional[str] = None
    # Optimistic concurrency: if set, must match the version this edit was
    # loaded from, or the write is rejected (409) rather than silently
    # clobbering a concurrent edit.
    expected_version: Optional[int] = None


# ── serialization: TenantConfig -> safe response dict ────────────────────────


def _location_out(loc: LocationConfig) -> dict:
    return {
        "name": loc.name,
        "address": loc.address,
        "phone": loc.phone,
        "business_hours": list(loc.business_hours),
        "business_days": list(loc.business_days),
        "booking_days": list(loc.booking_days) if loc.booking_days is not None else None,
        "day_hours": {str(k): list(v) for k, v in loc.day_hours.items()},
    }


def _service_out(svc: ServiceConfig) -> dict:
    return {
        "name": svc.name,
        "duration_min": svc.duration_min,
        "price": svc.price,
        "price_by_location": dict(svc.price_by_location),
        "name_es": svc.name_es,
    }


def _safe_config_out(cfg: TenantConfig) -> dict:
    return {
        "company_name": cfg.company_name,
        "business_tz": cfg.business_tz,
        "greeting": cfg.greeting,
        "transfer_phone": cfg.transfer_phone,
        "business_hours": list(cfg.business_hours),
        "business_days": list(cfg.business_days),
        "has_locations": len(cfg.locations) > 0,
        "locations": {lid: _location_out(loc) for lid, loc in cfg.locations.items()},
        "services": {sid: _service_out(svc) for sid, svc in cfg.services.items()},
        "emails": {
            "booking_to": cfg.email_booking_to,
            "escalation_to": cfg.email_escalation_to,
        },
        "voice_id": cfg.voice_id,
        "speed": cfg.speed,
        "language_pref": cfg.language_pref,
    }


# ── DB helpers ─────────────────────────────────────────────────────────────


def _current_row(engine, tenant_id: str) -> Optional[tuple[dict, int]]:
    """Highest published (config, version) for tenant_id, or None."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT config, version FROM tenant_configs "
                "WHERE tenant_id = :tid AND published "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"tid": tenant_id},
        ).first()
    if row is None:
        return None
    data = row[0]
    if isinstance(data, str):
        data = json.loads(data)
    return data, row[1]


def _summarize_change(prev: Optional[dict], curr: dict) -> str:
    """One-line "what changed" for the version-history list.

    Compares only the self-serve allow-list (the same keys PUT writes) so an
    out-of-band field (vapi ids, pricing cards, ...) never shows up as noise
    here. None `prev` means this is the oldest row this tenant has.
    """
    if prev is None:
        return "Initial config"
    changed = [k for k in _DIFF_LABELS if prev.get(k) != curr.get(k)]
    if not changed:
        return "No changes to editable fields"
    return ", ".join(_DIFF_LABELS[k] for k in changed) + " changed"


def _validate_hours_pair(pair: list[int], label: str) -> None:
    if len(pair) != 2 or not (0 <= pair[0] < pair[1] <= 24):
        raise HTTPException(
            status_code=400,
            detail=f"{label} must be [open_hour, close_hour) with 0 <= open < close <= 24",
        )


def _validate_days(days: list[int], label: str) -> None:
    if not days or any(d not in range(7) for d in days):
        raise HTTPException(
            status_code=400, detail=f"{label} must be weekday numbers 0 (Mon) - 6 (Sun)"
        )


def _apply_update(raw: dict, body: ConfigUpdate) -> dict:
    """Merge the validated safe subset onto raw (a tenant_configs.config dict,
    or the tenants/<id>/config.json shape) and return the new dict. raw is not
    mutated in place."""
    out = dict(raw)

    if body.company_name is not None:
        name = body.company_name.strip()
        if not name or len(name) > _MAX_NAME_LEN:
            raise HTTPException(status_code=400, detail="company_name must be 1-200 characters")
        out["company_name"] = name

    if body.business_tz is not None:
        # Same validation and message as signup.py's _validate_signup: a bad
        # zone must fail here as a clear 400 on the field that caused it, not
        # later as an opaque pytz error on the tenant's next booking.
        tz = body.business_tz.strip()
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise HTTPException(
                status_code=400,
                detail=f"business_tz '{body.business_tz}' is not a known IANA timezone "
                "(e.g. America/Toronto).",
            )
        out["business_tz"] = tz

    if body.greeting is not None:
        greeting = body.greeting.strip()
        if len(greeting) > _MAX_GREETING_LEN:
            raise HTTPException(
                status_code=400, detail=f"greeting must be at most {_MAX_GREETING_LEN} characters"
            )
        out["greeting"] = greeting

    if body.transfer_phone is not None:
        out["transfer_phone"] = body.transfer_phone.strip()

    if body.voice_id is not None:
        voice_id = body.voice_id.strip().lower()
        if len(voice_id) > _MAX_VOICE_ID_LEN:
            raise HTTPException(
                status_code=400, detail=f"voice_id must be at most {_MAX_VOICE_ID_LEN} characters"
            )
        # Deliberately NOT validated against the voice catalog here — the
        # catalog (lib/voice/voices.ts) is frontend-owned and not duplicated
        # backend-side. An unrecognized id is harmless to store; the dashboard
        # is responsible for only ever sending an id from its own library.
        out["voice_id"] = voice_id

    if body.speed is not None:
        if not (_VOICE_SPEED_MIN <= body.speed <= _VOICE_SPEED_MAX):
            raise HTTPException(
                status_code=400,
                detail=f"speed must be between {_VOICE_SPEED_MIN} and {_VOICE_SPEED_MAX}",
            )
        out["speed"] = body.speed

    if body.language_pref is not None:
        pref = body.language_pref.strip().lower()
        if pref not in _LANGUAGE_PREFS:
            raise HTTPException(
                status_code=400,
                detail=f"language_pref must be one of: {', '.join(sorted(_LANGUAGE_PREFS))}",
            )
        out["language_pref"] = pref

    if body.business_hours is not None:
        _validate_hours_pair(body.business_hours, "business_hours")
        out["business_hours"] = list(body.business_hours)

    if body.business_days is not None:
        _validate_days(body.business_days, "business_days")
        out["business_days"] = list(body.business_days)

    if body.emails is not None:
        emails = dict(out.get("emails") or {})
        if body.emails.booking_to is not None:
            emails["booking_to"] = body.emails.booking_to.strip()
        if body.emails.escalation_to is not None:
            emails["escalation_to"] = body.emails.escalation_to.strip()
        out["emails"] = emails

    if body.locations is not None:
        existing = dict(out.get("locations") or {})
        if not existing:
            raise HTTPException(
                status_code=400,
                detail="This business has no locations configured — locations can only be "
                "edited, not added, through self-serve settings.",
            )
        unknown = set(body.locations.keys()) - set(existing.keys())
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown location id(s): {', '.join(sorted(unknown))}. "
                f"Existing locations: {', '.join(sorted(existing.keys()))}.",
            )
        merged_locations = dict(existing)
        for lid, loc_update in body.locations.items():
            current = dict(existing[lid])  # preserves calendar_id + anything else
            if loc_update.name is not None:
                current["name"] = loc_update.name.strip() or lid
            if loc_update.address is not None:
                current["address"] = loc_update.address.strip()
            if loc_update.phone is not None:
                current["phone"] = loc_update.phone.strip()
            if loc_update.business_hours is not None:
                _validate_hours_pair(loc_update.business_hours, f"locations.{lid}.business_hours")
                current["business_hours"] = list(loc_update.business_hours)
            if loc_update.business_days is not None:
                _validate_days(loc_update.business_days, f"locations.{lid}.business_days")
                current["business_days"] = list(loc_update.business_days)
            if loc_update.booking_days is not None:
                _validate_days(loc_update.booking_days, f"locations.{lid}.booking_days")
                current["booking_days"] = list(loc_update.booking_days)
            if loc_update.day_hours is not None:
                for wd, pair in loc_update.day_hours.items():
                    if wd not in {"0", "1", "2", "3", "4", "5", "6"}:
                        raise HTTPException(
                            status_code=400,
                            detail=f"locations.{lid}.day_hours keys must be '0'-'6'",
                        )
                    _validate_hours_pair(pair, f"locations.{lid}.day_hours.{wd}")
                current["day_hours"] = {k: list(v) for k, v in loc_update.day_hours.items()}
            merged_locations[lid] = current
        out["locations"] = merged_locations

    if body.services is not None:
        new_services = {}
        for sid, svc in body.services.items():
            name = svc.name.strip()
            if not name or len(name) > _MAX_NAME_LEN:
                raise HTTPException(
                    status_code=400, detail=f"services.{sid}.name must be 1-200 characters"
                )
            new_services[sid] = {
                "name": name,
                "duration_min": svc.duration_min,
                "price": svc.price.strip(),
                "price_by_location": {k: v.strip() for k, v in svc.price_by_location.items()},
                "name_es": svc.name_es.strip(),
            }
        out["services"] = new_services

    return out


# ── routes ────────────────────────────────────────────────────────────────


@router.get("/platform/config")
def platform_get_config(request: Request) -> dict:
    """Return the safe, editable subset of the tenant's current published config.

    Sync `def` on purpose (FastAPI threadpool) — load_tenant() may do a
    blocking DB read on cache miss.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    cfg = load_tenant(tenant_id)
    version: Optional[int] = None
    if tenant_id != "default":
        from platform_db import get_engine

        engine = get_engine()
        if engine is not None:
            row = _current_row(engine, tenant_id)
            if row is not None:
                version = row[1]

    return {"tenant_id": tenant_id, "version": version, "config": _safe_config_out(cfg)}


@router.get("/platform/config/versions")
def platform_config_versions(request: Request) -> dict:
    """List published tenant_configs versions, newest first.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if tenant_id == "default":
        raise HTTPException(
            status_code=400,
            detail="Orchelix's own config is code-managed — no version history.",
        )

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT version, config, created_by, created_at FROM tenant_configs "
                "WHERE tenant_id = :tid AND published ORDER BY version DESC LIMIT :limit"
            ),
            {"tid": tenant_id, "limit": _MAX_VERSIONS},
        ).mappings().all()

    # Oldest-to-newest to diff each version against the one before it, then
    # reverse for the newest-first response the UI wants.
    rows = list(reversed(rows))
    versions = []
    prev_cfg: Optional[dict] = None
    for r in rows:
        cfg = r["config"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        versions.append(
            {
                "version": r["version"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "created_by": r["created_by"],
                "summary": _summarize_change(prev_cfg, cfg),
            }
        )
        prev_cfg = cfg
    versions.reverse()

    return {"tenant_id": tenant_id, "versions": versions}


@router.get("/platform/config/versions/{version}")
def platform_config_version_detail(version: int, request: Request) -> dict:
    """Read-only: a past version's safe config, for the history viewer.

    Sync `def` on purpose (FastAPI threadpool) — blocking SQLAlchemy query.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if tenant_id == "default":
        raise HTTPException(
            status_code=400,
            detail="Orchelix's own config is code-managed — no version history.",
        )

    from sqlalchemy import text

    from platform_db import get_engine
    from tenants import _config_from_file

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT config, created_by, created_at FROM tenant_configs "
                "WHERE tenant_id = :tid AND version = :version AND published"
            ),
            {"tid": tenant_id, "version": version},
        ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")

    data = row["config"]
    if isinstance(data, str):
        data = json.loads(data)

    try:
        cfg = _config_from_file(tenant_id, data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stored version is invalid: {e}")

    return {
        "tenant_id": tenant_id,
        "version": version,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "created_by": row["created_by"],
        "config": _safe_config_out(cfg),
    }


@router.put("/platform/config")
def platform_put_config(body: ConfigUpdate, request: Request) -> dict:
    """Validate + write a new published tenant_configs version.

    Orchelix's own ("default") tenant is code-canonical (tools.py) and
    load_tenant() never reads tenant_configs for it — writing here would be a
    silent no-op, so it is rejected outright.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if tenant_id == "default":
        raise HTTPException(
            status_code=400,
            detail="Orchelix's own config is code-managed, not self-serve editable.",
        )

    from sqlalchemy import text

    from platform_db import get_engine

    engine = get_engine()
    if engine is None:
        raise HTTPException(status_code=503, detail="Platform DB not configured.")

    current = _current_row(engine, tenant_id)
    if current is not None:
        raw, version = current
    else:
        # No DB row yet (tenant not imported / brand new) — fall back to the
        # file config so the first self-serve save doesn't lose anything a
        # tenants/<id>/config.json already has, matching load_tenant()'s own
        # DB-then-file precedence.
        from tenants import _REGISTRY_DIR

        cfg_path = _REGISTRY_DIR / tenant_id / "config.json"
        raw = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        version = 0

    if body.expected_version is not None and body.expected_version != version:
        raise HTTPException(
            status_code=409,
            detail=f"Config changed since you loaded it (now v{version}) — refresh and retry.",
        )

    merged = _apply_update(raw, body)

    # Validate the merged result parses into a real TenantConfig before it is
    # ever written — a bad merge must fail loudly here, not at agent runtime.
    from tenants import _config_from_file

    try:
        new_cfg = _config_from_file(tenant_id, merged)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Resulting config is invalid: {e}")

    new_version = version + 1
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, company_name, business_tz) "
                "VALUES (:id, :company_name, :business_tz) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": tenant_id,
                "company_name": merged.get("company_name"),
                "business_tz": merged.get("business_tz"),
            },
        )
        conn.execute(
            text(
                "INSERT INTO tenant_configs "
                "(tenant_id, version, config, published, created_by) "
                "VALUES (:tid, :version, :config, true, :created_by)"
            ),
            {
                "tid": tenant_id,
                "version": new_version,
                "config": json.dumps(merged),
                "created_by": request.headers.get("X-Platform-User", "dashboard"),
            },
        )

    clear_tenant_cache(tenant_id)
    log.info("Tenant '%s': config v%s published via /platform/config.", tenant_id, new_version)

    return {"tenant_id": tenant_id, "version": new_version, "config": _safe_config_out(new_cfg)}
