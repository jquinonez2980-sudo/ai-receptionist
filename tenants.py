# tenants.py — multi-tenant registry (Phase 1 of the SaaS plan)
#
# A tenant's NON-SECRET config (company name, pricing, hours, email recipients,
# persona overrides, KB) lives on disk under tenants/<id>/. SECRETS never live
# here — they are runtime env vars resolved by tenant_secret() using the
# convention TENANT_<ID>_<NAME> (per-tenant) with the global var as the
# "default" tenant's source. This satisfies CLAUDE.md hard rule #1.
#
# DB-first config (PLATFORM_BLUEPRINT.md Ticket 1): load_tenant() now prefers
# the highest published row in the tenant_configs Postgres table (same JSON
# shape as config.json), cached in-process for 60s, and falls back to the
# tenants/<id>/config.json file when the DB row is missing or the DB is
# unavailable. Set TENANT_CONFIG_FROM_DB=0 to force file-only (kill switch).
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
#
# Self-serve onboarding (Phase 4 ticket 4.1): a tenant created through signup
# lives ONLY in Postgres — it has no tenants/<id>/ directory. So tenant_exists()
# and _all_tenant_ids() are DB-aware (DB checked after the filesystem, which
# stays the zero-query fast path for every tenant that predates this), and
# tenant_is_active() gates production traffic on tenants.onboarding_status
# reaching 'active'. Same fallback contract as the config lookup: any DB
# problem leaves resolution on the pre-Phase-4 filesystem behavior.

from __future__ import annotations

import json
import logging
import os
import re
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Optional

log = logging.getLogger(__name__)

_REGISTRY_DIR = Path(__file__).parent / "tenants"
_TENANT_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")

# Onboarding lifecycle (alembic 0006 — keep in sync with that migration's
# CHECK constraint). Reaching ACTIVE_ONBOARDING_STATUS is NECESSARY but no
# longer SUFFICIENT to serve traffic — see BLOCKING_ACCOUNT_STATUSES below.
ONBOARDING_STATUSES = (
    "draft",
    "submitted",
    "provisioning",
    "review",
    "active",
    "rejected",
)
ACTIVE_ONBOARDING_STATUS = "active"

# Billing lifecycle (tenants.status, alembic 0001) values that take a tenant
# OFF THE AIR even when onboarding_status is 'active'.
#
# This is the second half of the traffic gate. Before it existed, an admin
# could set a tenant to suspended/archived on the Tenants admin page and it
# would keep answering calls — the dashboard said one thing and the phone did
# another.
#
# 'past_due' is deliberately NOT here: it usually means a card retry failed,
# and cutting a paying business's phone line mid-dunning is a business
# decision with real customer damage, not a sensible technical default.
# 'trial' obviously keeps serving — that is what a trial is.
BLOCKING_ACCOUNT_STATUSES = frozenset({"suspended", "archived"})
DEFAULT_ACCOUNT_STATUS = "live"

# ── Default-tenant (Orchelix) non-secret constants that are NOT already in
#    tools.py. Pricing / tz / hours / slot come from tools.py at load time.
_DEFAULT_COMPANY = "Orchelix AI Consulting"
_DEFAULT_EMAIL_FROM = "info@orchelix.com"
_DEFAULT_EMAIL_BOOKING_TO = "info@orchelix.com"
_DEFAULT_EMAIL_ESCALATION_TO = "jquinonez2980@gmail.com"
_DEFAULT_SMS_SIGNATURE = "Orchelix AI Consulting"
_DEFAULT_VOICE_SUMMARY = "Orchelix Intro Call"

# Short, tenant-scoped override for "what does Esmi itself cost" (agents.py's
# _make_middleware injects this — see TenantConfig.esmi_pricing_pitch below).
# Kept in sync with orchelix.com/pricing by hand; update both together.
_DEFAULT_ESMI_PRICING_PITCH = (
    "Esmi has three plans:\n"
    "- Starter — $299/mo + $499 one-time setup: 300 minutes, 1 number, voice only, 1 calendar\n"
    "- Growth (most popular) — $599/mo + $799 one-time setup: 800 minutes, up to 2 numbers, "
    "voice + web chat, multi-location calendars\n"
    "- Scale — $999/mo + custom setup: 1,500 minutes, 3+ numbers, multi-org\n"
    "7-day pilot: $149, includes setup, credited to your first month if you continue.\n"
    "Paying annually gets 2 months free and waives the one-time setup fee.\n"
    "Full details: https://www.orchelix.com/pricing"
)


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
    # Optional Spanish display name (get_pricing()'s Spanish path). Empty ->
    # callers fall back to `name`, same "_es override, else default" pattern
    # already used by sms_templates' confirmation_en/confirmation_es keys.
    name_es: str = ""

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
    # Optional Spanish override for pricing_note (get_pricing()'s Spanish path).
    # Deliberately NOT falling back to pricing_note when unset — that string is
    # English, and leaking it into a Spanish reply is the exact bug being fixed.
    pricing_note_es: str = ""
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
    # Optional custom opening line (dashboard "Greeting" field, Phase 2 self-
    # serve config). Not yet wired into the live prompt — prompts/esmi_system.md
    # and tenants/<id>/prompts/ remain authoritative (CLAUDE.md rule #4) until a
    # later change compiles this into the prompt template. Stored now so the
    # settings UI has somewhere durable to write.
    greeting: str = ""
    # Tenant-scoped override for "what does Esmi itself cost" (the
    # PRICING — ESMI ITSELF case in prompts/esmi_system.md / informer.md).
    # Empty for every tenant except 'default' (see _DEFAULT_ESMI_PRICING_PITCH)
    # — agents.py only appends a prompt section when this is set, so an unset
    # value leaves the base prompt (and its canned "I'll have Jorge reach
    # out" deflection) byte-identical to before for every client tenant.
    esmi_pricing_pitch: str = ""

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


# tid → (config, monotonic-clock expiry). 'default' never expires (it is
# code-canonical in tools.py); other tenants expire after _TTL_SECONDS so a
# tenant_configs row published by the dashboard goes live within a minute
# without a redeploy.
_TTL_SECONDS = 60.0
_cache: dict[str, tuple[TenantConfig, float]] = {}
_lock = threading.Lock()

@dataclass(frozen=True)
class TenantState:
    """The control-plane row fields that decide whether a tenant serves traffic.

    Both lifecycle columns are fetched in ONE query and cached together. They
    are always read as a pair (the gate needs both), so splitting them would
    mean two round trips to answer one question — which is what the code did
    before: platform_api/tenant_status.py fired a second SELECT for exactly
    these columns on every dashboard page load.
    """

    onboarding_status: str
    account_status: str
    plan: Optional[str] = None


# tid → (TenantState | None, expiry). None means "queried the DB, this tenant
# has no row" — a real answer worth caching, distinct from _UNAVAILABLE (below)
# which is never cached. Same 60s TTL as the config cache so an admin approval
# or suspension goes live within a minute without a redeploy.
_status_cache: dict[str, tuple[Optional[TenantState], float]] = {}

# Sentinel: the DB could not answer (kill switch off, DATABASE_URL unset, table
# or column not migrated yet, connection failure). Callers must fall back to
# the filesystem registry — the exact pre-Phase-4 behavior — rather than
# treating it as "no such tenant". Deliberately NOT cached, so a transient
# blip recovers on the next request instead of sticking for a full TTL.
_UNAVAILABLE = object()


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
        esmi_pricing_pitch=_DEFAULT_ESMI_PRICING_PITCH,
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
            name_es=str(svc.get("name_es") or "").strip(),
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
    # MISSING pricing inherits the default tenant's (long-standing behavior for
    # partial configs). An EXPLICIT empty list means "this tenant has no price
    # list yet" and must stay empty — `or` would treat [] as falsy and hand a
    # brand-new tenant Orchelix's own SaaS pricing cards, which the agent would
    # then quote to that tenant's customers. Self-serve onboarding seeds
    # exactly this shape (see platform_api/signup.py), so the distinction is
    # load-bearing. No existing tenants/<id>/config.json has an empty pricing
    # array, so nothing already live changes behavior.
    raw_pricing = data.get("pricing")
    pricing = list(base.pricing) if raw_pricing is None else list(raw_pricing)
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
        pricing=pricing,
        pricing_note=data.get("pricing_note", ""),
        pricing_note_es=data.get("pricing_note_es", ""),
        vapi_assistant_ids=tuple(vapi.get("assistant_ids") or ()),
        vapi_phone_number_ids=tuple(vapi.get("phone_number_ids") or ()),
        calendar_id=legacy_cal,
        business_days=tuple(int(d) for d in data.get("business_days") or base.business_days),
        locations=locations,
        services=services,
        sms_templates={str(k): str(v) for k, v in sms_templates.items()},
        transfer_phone=str(data.get("transfer_phone") or "").strip(),
        greeting=str(data.get("greeting") or "").strip(),
        # No base.esmi_pricing_pitch fallback (unlike company_name etc. above) —
        # this must stay "" for every tenant that doesn't explicitly set it, or
        # a config.json missing the key would silently inherit the default
        # tenant's Esmi-itself pricing pitch.
        esmi_pricing_pitch=str(data.get("esmi_pricing_pitch") or "").strip(),
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


def _db_enabled() -> bool:
    """DB-first lookup kill switch: TENANT_CONFIG_FROM_DB=0 forces file-only."""
    return os.environ.get("TENANT_CONFIG_FROM_DB", "1").strip().lower() not in (
        "0", "false", "off",
    )


def _config_from_db(tenant_id: str) -> Optional[TenantConfig]:
    """Load the highest PUBLISHED tenant_configs row, or None to fall back.

    None on: kill switch off, DATABASE_URL unset, table not migrated yet, DB
    outage, or simply no row for this tenant. Never raises — any problem here
    must leave the runtime on the file-based path.
    """
    if not _db_enabled():
        return None
    try:
        from platform_db import get_engine

        engine = get_engine()
        if engine is None:
            return None
        from sqlalchemy import text as _sql

        with engine.connect() as conn:
            row = conn.execute(
                _sql(
                    "SELECT config, version FROM tenant_configs "
                    "WHERE tenant_id = :tid AND published "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"tid": tenant_id},
            ).first()
        if row is None:
            return None
        data = row[0]
        if isinstance(data, str):  # driver returned jsonb as text
            data = json.loads(data)
        log.info("Tenant '%s': config loaded from DB (v%s).", tenant_id, row[1])
        return _config_from_file(tenant_id, data)
    except Exception as e:
        log.warning(
            "Tenant '%s': DB config lookup failed (%s: %s) — using file config.",
            tenant_id, type(e).__name__, e,
        )
        return None


def _db_tenant_status(tenant_id: str):
    """Read the tenant's lifecycle columns as a TenantState.

    Returns a TenantState, None (queried successfully, no such row), or
    _UNAVAILABLE (DB off / unreachable / not migrated). Never raises.

    The UndefinedColumn case matters operationally: migrations are applied
    manually on Railway, so this code ships live BEFORE alembic 0006 runs.
    That error lands in the same `except` as a connection failure, which is
    exactly right — both mean "the DB can't tell me, use the filesystem".
    """
    if not _db_enabled():
        return _UNAVAILABLE
    try:
        from platform_db import get_engine

        engine = get_engine()
        if engine is None:
            return _UNAVAILABLE
        from sqlalchemy import text as _sql

        with engine.connect() as conn:
            row = conn.execute(
                _sql(
                    "SELECT onboarding_status, status, plan FROM tenants WHERE id = :tid"
                ),
                {"tid": tenant_id},
            ).first()
        if row is None:
            return None
        # Both columns are NOT NULL with server defaults, but coalesce anyway:
        # a NULL here must not read as "blocked" and silently take a live
        # tenant off the air.
        return TenantState(
            onboarding_status=row[0] or ACTIVE_ONBOARDING_STATUS,
            account_status=row[1] or DEFAULT_ACCOUNT_STATUS,
            plan=row[2],
        )
    except Exception as e:
        log.warning(
            "Tenant '%s': lifecycle lookup failed (%s: %s) — "
            "falling back to the filesystem registry.",
            tenant_id, type(e).__name__, e,
        )
        return _UNAVAILABLE


def _cached_tenant_status(tenant_id: str):
    """_db_tenant_status with the 60s cache in front. Same return contract."""
    hit = _status_cache.get(tenant_id)
    if hit is not None and monotonic() < hit[1]:
        return hit[0]
    state = _db_tenant_status(tenant_id)
    if state is not _UNAVAILABLE:
        with _lock:
            _status_cache[tenant_id] = (state, monotonic() + _TTL_SECONDS)
    return state


def tenant_state(tenant_id: str) -> Optional[TenantState]:
    """Resolved lifecycle state for tenant_id, or None if it's unknown.

    Public accessor so the platform API can render onboarding_status /
    account_status / plan from ONE cached lookup instead of issuing its own
    query. Applies the same legacy/outage fallbacks as the gate, so callers
    never have to reimplement them — but the traffic DECISION must still come
    from tenant_is_active(), which is the single place that rule lives.
    """
    return _resolved_state(tenant_id)


def _db_tenant_ids() -> Optional[list[str]]:
    """Every tenant id in the DB, or None when the DB cannot answer."""
    if not _db_enabled():
        return None
    try:
        from platform_db import get_engine

        engine = get_engine()
        if engine is None:
            return None
        from sqlalchemy import text as _sql

        with engine.connect() as conn:
            rows = conn.execute(_sql("SELECT id FROM tenants")).all()
        return [r[0] for r in rows]
    except Exception as e:
        log.warning(
            "Tenant id listing from DB failed (%s: %s) — filesystem registry only.",
            type(e).__name__, e,
        )
        return None


def load_tenant(tenant_id: str = "default") -> TenantConfig:
    """Return the TenantConfig for tenant_id (default == Orchelix).

    Resolution order: in-process cache (60s TTL) → highest published row in
    the tenant_configs DB table → tenants/<id>/config.json (the exact pre-DB
    behavior). The build runs OUTSIDE the lock so one tenant's slow DB lookup
    can never block another tenant's load; duplicate concurrent builds are
    harmless because TenantConfig is immutable.
    """
    tid = _norm(tenant_id)
    hit = _cache.get(tid)
    if hit is not None and monotonic() < hit[1]:
        return hit[0]

    if tid == "default":
        cfg = _default_config()
        expires = float("inf")
    else:
        cfg = _config_from_db(tid)
        if cfg is None:
            cfg = _build(tid)
        expires = monotonic() + _TTL_SECONDS
        if cfg.email_from == _DEFAULT_EMAIL_FROM or cfg.email_booking_to == _DEFAULT_EMAIL_BOOKING_TO:
            warnings.warn(
                f"Tenant '{tid}' is inheriting Orchelix email config. "
                f"Set emails.from / emails.booking_to in tenants/{tid}/config.json.",
                stacklevel=2,
            )

    with _lock:
        _cache[tid] = (cfg, expires)
    return cfg


def clear_tenant_cache(tenant_id: Optional[str] = None) -> None:
    """Drop cached config(s) AND cached onboarding status.

    Used by tests after monkeypatching config files, and by the platform API
    after a write that must take effect immediately rather than at the next
    TTL expiry (config publish, tenant approval).
    """
    with _lock:
        if tenant_id is None:
            _cache.clear()
            _status_cache.clear()
        else:
            tid = _norm(tenant_id)
            _cache.pop(tid, None)
            _status_cache.pop(tid, None)


def normalize_tenant_id(tenant_id: Optional[str]) -> str:
    """Public wrapper for the tenant_id validator — any code that takes a
    tenant_id from an external source (HTTP header/body, LangGraph config,
    prompt-loader context) must pass it through this before using it in a
    filesystem path or secret-env lookup."""
    return _norm(tenant_id)


def tenant_exists(tenant_id: str) -> bool:
    """True if tenant_id is 'default', has a tenants/<id>/ directory, or has a
    row in the tenants table.

    Filesystem is checked FIRST and short-circuits: every tenant that predates
    self-serve onboarding resolves with zero queries, and the answer survives a
    DB outage. The DB check is what lets a signup-created tenant — which has no
    directory — reach the dashboard at all.

    Existence is NOT permission to serve traffic; see tenant_is_active().
    """
    tid = _norm(tenant_id)
    if tid == "default":
        return True
    if (_REGISTRY_DIR / tid).is_dir():
        return True
    return _cached_tenant_status(tid) not in (None, _UNAVAILABLE)


def _fallback_state(tid: str) -> Optional[TenantState]:
    """State for a tenant the DB can't describe.

    A filesystem tenant with no readable row is treated as fully live: it went
    live before these columns existed, and neither a missing row nor a DB
    outage may take a paying customer off the air. Anything else is unknown.
    """
    if (_REGISTRY_DIR / tid).is_dir():
        return TenantState(ACTIVE_ONBOARDING_STATUS, DEFAULT_ACCOUNT_STATUS)
    return None


def _resolved_state(tenant_id: str) -> Optional[TenantState]:
    """The state the gate and the dashboard should both reason about."""
    tid = _norm(tenant_id)
    if tid == "default":
        # Orchelix itself is code-canonical and was never onboarded or billed.
        return TenantState(ACTIVE_ONBOARDING_STATUS, DEFAULT_ACCOUNT_STATUS)
    state = _cached_tenant_status(tid)
    if state is _UNAVAILABLE or state is None:
        return _fallback_state(tid)
    return state


def tenant_onboarding_status(tenant_id: str) -> Optional[str]:
    """Onboarding lifecycle value for tenant_id, or None if it has no DB row.

    'default' (Orchelix itself) is code-canonical and was never onboarded — it
    reports 'active'. A filesystem tenant with no DB row also reports 'active':
    it went live before this column existed, and a missing row must never take
    a paying customer off the air.
    """
    state = _resolved_state(tenant_id)
    return state.onboarding_status if state is not None else None


def tenant_account_status(tenant_id: str) -> Optional[str]:
    """Billing lifecycle value (trial | live | past_due | suspended | archived),
    or None if the tenant has no DB row. Same fallbacks as
    tenant_onboarding_status — an unreadable row reports 'live', never a
    blocking value."""
    state = _resolved_state(tenant_id)
    return state.account_status if state is not None else None


def tenant_is_active(tenant_id: str) -> bool:
    """True if tenant_id may serve PRODUCTION traffic (voice + web chat).

    THE single traffic gate. api._resolve_tenant (chat), api._resolve_tenant_strict
    (booking), tenants._vapi_tenant_allowed (voice) and the dashboard's
    can_serve_traffic all route through here, so this function is the only
    place the rule is written down.

    Two independent axes, both of which must pass:

      1. onboarding_status == 'active' — the approve-to-activate gate. A tenant
         in draft / submitted / provisioning / review / rejected exists, can
         sign in, and can configure itself, but must not answer a single
         customer call until Orchelix approves it.

      2. account_status not in BLOCKING_ACCOUNT_STATUSES — the billing gate.
         Added because axis 1 alone meant an admin could set a tenant to
         suspended or archived on the Tenants page and it would keep answering
         the phone: the dashboard said one thing, the phone did another.

    Legacy and outage behavior are both fail-OPEN by design, because the only
    tenants that can reach those branches are ones that were already live
    before either gate existed: a filesystem tenant with no DB row, and any
    tenant when the DB is unreachable. A self-serve tenant has neither a
    directory nor a pre-approval path to 'active', so it stays gated.

    Note this is NOT the same question as tenant_exists() — a suspended tenant
    still exists and still reaches its dashboard, so it can be told why.
    """
    state = _resolved_state(tenant_id)
    if state is None:
        return False
    return (
        state.onboarding_status == ACTIVE_ONBOARDING_STATUS
        and state.account_status not in BLOCKING_ACCOUNT_STATUSES
    )


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
                       Hyphens in the slug become underscores (env var names
                       can't safely contain '-'), e.g. tenant "otro-nivel" →
                       TENANT_OTRO_NIVEL_<NAME>.
    """
    tid = _norm(tenant_id)
    if tid == "default":
        return os.environ.get(name)
    return os.environ.get(f"TENANT_{tid.upper().replace('-', '_')}_{name}")


# ── VAPI inbound → tenant mapping ─────────────────────────────────────────────

def _all_tenant_ids() -> list[str]:
    """Every known tenant id except 'default' — filesystem registry ∪ DB rows.

    Sorted so callers (VAPI resolution, scripts/update_vapi_webhooks.py) get a
    stable order regardless of which source a given tenant came from.
    """
    ids: set[str] = set()
    if _REGISTRY_DIR.is_dir():
        ids.update(p.name for p in _REGISTRY_DIR.iterdir() if p.is_dir())
    db_ids = _db_tenant_ids()
    if db_ids:
        ids.update(db_ids)
    ids.discard("default")
    return sorted(ids)


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
            return tid if _vapi_tenant_allowed(tid) else "default"
        if phone_id and phone_id in cfg.vapi_phone_number_ids:
            return tid if _vapi_tenant_allowed(tid) else "default"
    return "default"


def _vapi_tenant_allowed(tenant_id: str) -> bool:
    """Approve-to-activate gate on the inbound voice path.

    A pre-approval tenant should have no VAPI assistant or number at all (both
    provisioning steps are manual and run after approval), so this is a
    belt-and-braces check for the case where an id was wired up early — e.g.
    an admin pasting an assistant id into the provisioning checklist before
    clicking Approve. Logged loudly, because reaching it means a number went
    live ahead of its approval.
    """
    if tenant_is_active(tenant_id):
        return True
    log.warning(
        "VAPI call matched tenant '%s' but it cannot serve traffic "
        "(onboarding_status=%s, account_status=%s) — refusing to serve it and "
        "falling back to default.",
        tenant_id,
        tenant_onboarding_status(tenant_id),
        tenant_account_status(tenant_id),
    )
    return False
