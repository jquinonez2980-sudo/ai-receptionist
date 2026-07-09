# tenants.py — multi-tenant registry (Phase 1 of the SaaS plan)
#
# A tenant's NON-SECRET config (company name, pricing, hours, email recipients,
# persona overrides, KB) lives on disk under tenants/<id>/. SECRETS never live
# here — they are runtime env vars resolved by tenant_secret() using the
# convention TENANT_<ID>_<NAME> (per-tenant) with the global var as the
# "default" tenant's source. This satisfies CLAUDE.md hard rule #1.
#
# Backward compatibility: tenant_id "default" == Orchelix. Its config is built
# from the existing canonical constants in tools.py (_PRICING, _BUSINESS_TZ,
# _HOURS, _SLOT_MIN) via a late import, so the single live deployment is
# byte-identical. tools.py imports THIS module (one direction only); the late
# import inside load_tenant() runs at request time, after tools.py is fully
# loaded, so there is no import cycle.
#
# Multi-location (Otro Nivel): optional locations map + services map. Existing
# single-location tenants keep working via a synthesized default location from
# the legacy top-level calendar_id / business_hours / business_days fields.

from __future__ import annotations

import json
import logging
import os
import re
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_REGISTRY_DIR = Path(__file__).parent / "tenants"
_TENANT_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")

# ── Default-tenant (Orchelix) non-secret constants that are NOT already in
#    tools.py. Pricing / tz / hours / slot come from tools.py at load time.
_DEFAULT_COMPANY = "Orchelix AI Consulting"
_DEFAULT_EMAIL_FROM = "info@orchelix.com"
_DEFAULT_EMAIL_BOOKING_TO = "info@orchelix.com"
_DEFAULT_EMAIL_ESCALATION_TO = "jquinonez2980@gmail.com"
_DEFAULT_SMS_SIGNATURE = "Orchelix AI Consulting"
_DEFAULT_VOICE_SUMMARY = "Orchelix Intro Call"


@dataclass(frozen=True)
class LocationConfig:
    """One physical shop / bookable calendar for a tenant."""

    id: str
    name: str
    address: str = ""
    calendar_id: str = "primary"
    # Default [start_hour, end_hour) window when day_hours has no entry.
    business_hours: tuple[int, int] = (9, 17)
    # Weekdays the location is OPEN (Python weekday: Mon=0 … Sun=6).
    business_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    # Weekdays that accept APPOINTMENTS. None → same as business_days.
    # Example: open Sat (walk-in) but booking_days excludes Sat.
    booking_days: Optional[tuple[int, ...]] = None
    # Optional per-weekday hour overrides: {0: (10, 17), 1: (10, 19), ...}
    day_hours: dict[int, tuple[int, int]] = field(default_factory=dict)
    phone: str = ""

    @property
    def effective_booking_days(self) -> tuple[int, ...]:
        return self.booking_days if self.booking_days is not None else self.business_days

    def hours_for_day(self, weekday: int) -> tuple[int, int]:
        """Return (open_hour, close_hour) for a Python weekday (Mon=0)."""
        if weekday in self.day_hours:
            return self.day_hours[weekday]
        return self.business_hours


@dataclass(frozen=True)
class ServiceConfig:
    """Bookable service with optional per-location price overrides."""

    id: str
    name: str
    duration_min: int = 30
    price: str = ""
    # location_id → price string (e.g. {"weston": "$50", "keele": "$35–$40"})
    price_by_location: dict[str, str] = field(default_factory=dict)

    def price_for(self, location_id: str) -> str:
        return self.price_by_location.get(location_id) or self.price


@dataclass(frozen=True)
class TenantConfig:
    """Immutable per-tenant configuration. Secrets are NOT stored here."""
    tenant_id: str
    company_name: str
    business_tz: str
    business_hours: tuple[int, int]      # [start_hour, end_hour) 24h local
    slot_minutes: int
    email_from: str
    email_booking_to: str
    email_escalation_to: str
    sms_signature: str
    voice_default_summary: str
    pricing: list = field(default_factory=list)   # list[dict] (see tools._PRICING shape)
    pricing_note: str = ""  # optional footer override for non-SaaS tenants (e.g. per-job pricing)
    vapi_assistant_ids: tuple[str, ...] = ()
    vapi_phone_number_ids: tuple[str, ...] = ()
    calendar_id: str = "primary"  # Google Calendar identifier (legacy single-location)
    # Which weekdays the business is open, Python datetime.weekday() values
    # (Monday=0 ... Sunday=6). Default Mon-Fri for backward compatibility with
    # tenants that predate this field.
    business_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    # Multi-location / multi-service (optional; empty → synthesize default location)
    locations: dict[str, LocationConfig] = field(default_factory=dict)
    services: dict[str, ServiceConfig] = field(default_factory=dict)
    # Optional SMS template overrides (EN/ES). Placeholders: {name} {when} {location} {service}
    sms_templates: dict[str, str] = field(default_factory=dict)
    transfer_phone: str = ""

    @property
    def hours_range(self) -> range:
        return range(self.business_hours[0], self.business_hours[1])

    @property
    def is_multi_location(self) -> bool:
        return len(self.locations) > 1

    def default_location(self) -> LocationConfig:
        """Return the sole location, or synthesize one from legacy tenant fields."""
        if self.locations:
            if len(self.locations) == 1:
                return next(iter(self.locations.values()))
            raise ValueError(
                f"Tenant '{self.tenant_id}' has multiple locations — location is required."
            )
        return LocationConfig(
            id="default",
            name=self.company_name,
            calendar_id=self.calendar_id,
            business_hours=self.business_hours,
            business_days=self.business_days,
            booking_days=self.business_days,
        )

    def resolve_location(self, location: Optional[str] = None) -> LocationConfig:
        """Resolve a location key (id or name, case-insensitive) to LocationConfig.

        Single-location tenants accept None / any empty value and return the
        only (or synthesized) location. Multi-location tenants require a match.
        """
        key = (location or "").strip().lower()
        if not self.locations:
            return self.default_location()
        if not key:
            if len(self.locations) == 1:
                return next(iter(self.locations.values()))
            raise ValueError(
                "Which location? This business has more than one — please specify."
            )
        # Exact id match
        if key in self.locations:
            return self.locations[key]
        # Match by name (e.g. "Weston Road" → weston)
        for loc in self.locations.values():
            if loc.name.lower() == key or loc.id.lower() == key:
                return loc
            if key in loc.name.lower() or key in loc.id.lower():
                return loc
        known = ", ".join(sorted(self.locations.keys()))
        raise ValueError(f"Unknown location '{location}'. Choose one of: {known}.")

    def resolve_service(self, service: Optional[str] = None) -> Optional[ServiceConfig]:
        """Resolve a service key or free-text name. None if tenant has no services map."""
        if not self.services:
            return None
        key = (service or "").strip().lower()
        if not key:
            return None
        if key in self.services:
            return self.services[key]
        for svc in self.services.values():
            if svc.name.lower() == key or svc.id.lower() == key:
                return svc
            if key in svc.name.lower() or key in svc.id.lower():
                return svc
        return None

    def all_calendar_ids(self) -> list[tuple[str, str]]:
        """Return [(location_id, calendar_id), ...] for every bookable calendar."""
        if self.locations:
            return [(loc.id, loc.calendar_id) for loc in self.locations.values()]
        return [("default", self.calendar_id)]


_cache: dict[str, TenantConfig] = {}
_lock = threading.Lock()


def _norm(tenant_id: Optional[str]) -> str:
    """Normalize + validate a client-supplied tenant id.

    Only [a-z0-9-]{1,64} is accepted (also rejects '.', '/', ':' — path
    traversal and thread-namespace collision vectors). Anything else silently
    falls back to 'default' rather than erroring, matching the existing
    unknown-tenant behavior.
    """
    tid = (tenant_id or "default").strip().lower() or "default"
    if not _TENANT_ID_RE.fullmatch(tid):
        log.warning("Rejected invalid tenant_id %r — falling back to default.", tenant_id)
        return "default"
    return tid


def _default_config() -> TenantConfig:
    """Build the 'default' (Orchelix) config from tools.py canonical constants.

    Late import avoids an import cycle (tools.py imports this module at top).
    Called only at request time, never during module import.
    """
    from tools import _BUSINESS_DAYS, _BUSINESS_TZ, _HOURS, _PRICING, _SLOT_MIN

    return TenantConfig(
        tenant_id="default",
        company_name=_DEFAULT_COMPANY,
        business_tz=_BUSINESS_TZ,
        business_hours=(_HOURS.start, _HOURS.stop),
        slot_minutes=_SLOT_MIN,
        email_from=_DEFAULT_EMAIL_FROM,
        email_booking_to=_DEFAULT_EMAIL_BOOKING_TO,
        email_escalation_to=_DEFAULT_EMAIL_ESCALATION_TO,
        sms_signature=_DEFAULT_SMS_SIGNATURE,
        voice_default_summary=_DEFAULT_VOICE_SUMMARY,
        pricing=list(_PRICING),
        business_days=tuple(_BUSINESS_DAYS),
    )


def _parse_hours_pair(raw, fallback: tuple[int, int]) -> tuple[int, int]:
    if not raw or len(raw) < 2:
        return fallback
    return (int(raw[0]), int(raw[1]))


def _parse_day_hours(raw) -> dict[int, tuple[int, int]]:
    """Parse day_hours from JSON: keys may be str or int weekday numbers."""
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[int, tuple[int, int]] = {}
    for k, v in raw.items():
        try:
            day = int(k)
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                out[day] = (int(v[0]), int(v[1]))
        except (TypeError, ValueError):
            continue
    return out


def _parse_locations(data: dict, base: TenantConfig) -> dict[str, LocationConfig]:
    raw = data.get("locations")
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, LocationConfig] = {}
    for loc_id, loc in raw.items():
        if not isinstance(loc, dict):
            continue
        lid = str(loc_id).strip().lower()
        hours = _parse_hours_pair(
            loc.get("business_hours"), base.business_hours
        )
        bdays = loc.get("business_days")
        book_days = loc.get("booking_days")
        out[lid] = LocationConfig(
            id=lid,
            name=str(loc.get("name") or lid).strip(),
            address=str(loc.get("address") or "").strip(),
            calendar_id=str(loc.get("calendar_id") or base.calendar_id),
            business_hours=hours,
            business_days=tuple(int(d) for d in bdays) if bdays is not None else base.business_days,
            booking_days=tuple(int(d) for d in book_days) if book_days is not None else None,
            day_hours=_parse_day_hours(loc.get("day_hours")),
            phone=str(loc.get("phone") or "").strip(),
        )
    return out


def _parse_services(data: dict, default_duration: int) -> dict[str, ServiceConfig]:
    raw = data.get("services")
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, ServiceConfig] = {}
    for svc_id, svc in raw.items():
        if not isinstance(svc, dict):
            continue
        sid = str(svc_id).strip().lower()
        price_by = svc.get("price_by_location") or {}
        out[sid] = ServiceConfig(
            id=sid,
            name=str(svc.get("name") or sid).strip(),
            duration_min=int(svc.get("duration_min") or default_duration),
            price=str(svc.get("price") or "").strip(),
            price_by_location={str(k).lower(): str(v) for k, v in price_by.items()},
        )
    return out


def _config_from_file(tenant_id: str, data: dict) -> TenantConfig:
    """Build a TenantConfig from a tenants/<id>/config.json dict.

    Missing keys fall back to the default tenant's values so a partial
    config.json is valid.
    """
    base = _default_config()
    emails = data.get("emails") or {}
    hours = data.get("business_hours") or list(base.business_hours)
    vapi = data.get("vapi") or {}
    slot = int(data.get("slot_minutes", base.slot_minutes))
    locations = _parse_locations(data, base)
    services = _parse_services(data, slot)
    sms_templates = data.get("sms_templates") or {}
    # If multi-location, prefer first location calendar as legacy calendar_id
    # for any code that still reads the top-level field.
    legacy_cal = data.get("calendar_id", base.calendar_id)
    if locations and not data.get("calendar_id"):
        legacy_cal = next(iter(locations.values())).calendar_id

    return TenantConfig(
        tenant_id=tenant_id,
        company_name=data.get("company_name", base.company_name),
        business_tz=data.get("business_tz", base.business_tz),
        business_hours=(int(hours[0]), int(hours[1])),
        slot_minutes=slot,
        email_from=emails.get("from", base.email_from),
        email_booking_to=emails.get("booking_to", base.email_booking_to),
        email_escalation_to=emails.get("escalation_to", base.email_escalation_to),
        sms_signature=data.get("sms_signature", data.get("company_name", base.sms_signature)),
        voice_default_summary=data.get("voice_default_summary", base.voice_default_summary),
        pricing=data.get("pricing") or list(base.pricing),
        pricing_note=data.get("pricing_note", ""),
        vapi_assistant_ids=tuple(vapi.get("assistant_ids") or ()),
        vapi_phone_number_ids=tuple(vapi.get("phone_number_ids") or ()),
        calendar_id=legacy_cal,
        business_days=tuple(int(d) for d in data.get("business_days") or base.business_days),
        locations=locations,
        services=services,
        sms_templates={str(k): str(v) for k, v in sms_templates.items()},
        transfer_phone=str(data.get("transfer_phone") or "").strip(),
    )


def _build(tenant_id: str) -> TenantConfig:
    if tenant_id == "default":
        return _default_config()
    cfg_path = _REGISTRY_DIR / tenant_id / "config.json"
    if not cfg_path.exists():
        log.warning("Tenant '%s' has no config.json — falling back to default config.", tenant_id)
        # Keep the tenant_id so KB/secret lookups still namespace correctly.
        base = _default_config()
        return TenantConfig(**{**base.__dict__, "tenant_id": tenant_id})
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("Tenant '%s' config.json is invalid (%s) — using default config.", tenant_id, e)
        base = _default_config()
        return TenantConfig(**{**base.__dict__, "tenant_id": tenant_id})
    return _config_from_file(tenant_id, data)


def load_tenant(tenant_id: str = "default") -> TenantConfig:
    """Return the cached TenantConfig for tenant_id (default == Orchelix)."""
    tid = _norm(tenant_id)
    cached = _cache.get(tid)
    if cached is not None:
        return cached
    with _lock:
        cached = _cache.get(tid)
        if cached is not None:
            return cached
        cfg = _build(tid)
        if tid != "default":
            if cfg.email_from == _DEFAULT_EMAIL_FROM or cfg.email_booking_to == _DEFAULT_EMAIL_BOOKING_TO:
                warnings.warn(
                    f"Tenant '{tid}' is inheriting Orchelix email config. "
                    f"Set emails.from / emails.booking_to in tenants/{tid}/config.json.",
                    stacklevel=2,
                )
        _cache[tid] = cfg
        return cfg


def clear_tenant_cache(tenant_id: Optional[str] = None) -> None:
    """Drop cached config(s). Used by tests after monkeypatching config files."""
    with _lock:
        if tenant_id is None:
            _cache.clear()
        else:
            _cache.pop(_norm(tenant_id), None)


def normalize_tenant_id(tenant_id: Optional[str]) -> str:
    """Public wrapper for the tenant_id validator — any code that takes a
    tenant_id from an external source (HTTP header/body, LangGraph config,
    prompt-loader context) must pass it through this before using it in a
    filesystem path or secret-env lookup."""
    return _norm(tenant_id)


def tenant_exists(tenant_id: str) -> bool:
    """True if tenant_id is 'default' or has a tenants/<id>/ directory."""
    tid = _norm(tenant_id)
    if tid == "default":
        return True
    return (_REGISTRY_DIR / tid).is_dir()


def namespaced_thread(tenant_id: str, thread_id: str) -> str:
    """Namespace a checkpoint thread id per tenant so two tenants can never
    share a conversation. 'default' is left unprefixed so existing single-tenant
    threads stay addressable byte-for-byte after this change ships.
    """
    tid = _norm(tenant_id)
    return thread_id if tid == "default" else f"{tid}:{thread_id}"


def tenant_secret(tenant_id: str, name: str) -> Optional[str]:
    """Resolve a secret env var for a tenant.

    default tenant   → the global var (e.g. SENDGRID_API_KEY).
    other tenants    → TENANT_<ID>_<NAME> ONLY (no global fallback, so one
                       tenant can never read another's / the default's creds).
    """
    tid = _norm(tenant_id)
    if tid == "default":
        return os.environ.get(name)
    return os.environ.get(f"TENANT_{tid.upper()}_{name}")


# ── VAPI inbound → tenant mapping ─────────────────────────────────────────────

def _all_tenant_ids() -> list[str]:
    if not _REGISTRY_DIR.is_dir():
        return []
    return [p.name for p in _REGISTRY_DIR.iterdir() if p.is_dir() and p.name != "default"]


def resolve_vapi_tenant(payload: dict) -> str:
    """Map a VAPI webhook payload to a tenant_id via assistant/phone-number id.

    Looks for the assistant id and phone-number id in the common payload
    locations, then matches against each tenant's vapi config. Defaults to
    'default' when nothing matches (single-tenant behavior preserved).
    """
    msg = (payload or {}).get("message") or {}
    call = msg.get("call") or {}
    assistant_id = (
        msg.get("assistantId")
        or call.get("assistantId")
        or (call.get("assistant") or {}).get("id")
    )
    phone_id = (
        msg.get("phoneNumberId")
        or call.get("phoneNumberId")
        or (call.get("phoneNumber") or {}).get("id")
    )
    if not assistant_id and not phone_id:
        return "default"
    for tid in _all_tenant_ids():
        cfg = load_tenant(tid)
        if assistant_id and assistant_id in cfg.vapi_assistant_ids:
            return tid
        if phone_id and phone_id in cfg.vapi_phone_number_ids:
            return tid
    return "default"
