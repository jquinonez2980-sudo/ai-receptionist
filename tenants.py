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

from __future__ import annotations

import json
import logging
import os
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_REGISTRY_DIR = Path(__file__).parent / "tenants"

# ── Default-tenant (Orchelix) non-secret constants that are NOT already in
#    tools.py. Pricing / tz / hours / slot come from tools.py at load time.
_DEFAULT_COMPANY = "Orchelix AI Consulting"
_DEFAULT_EMAIL_FROM = "info@orchelix.com"
_DEFAULT_EMAIL_BOOKING_TO = "info@orchelix.com"
_DEFAULT_EMAIL_ESCALATION_TO = "jquinonez2980@gmail.com"
_DEFAULT_SMS_SIGNATURE = "Orchelix AI Consulting"
_DEFAULT_VOICE_SUMMARY = "Orchelix Intro Call"


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
    vapi_assistant_ids: tuple[str, ...] = ()
    vapi_phone_number_ids: tuple[str, ...] = ()
    calendar_id: str = "primary"  # Google Calendar identifier

    @property
    def hours_range(self) -> range:
        return range(self.business_hours[0], self.business_hours[1])


_cache: dict[str, TenantConfig] = {}
_lock = threading.Lock()


def _norm(tenant_id: Optional[str]) -> str:
    return (tenant_id or "default").strip().lower() or "default"


def _default_config() -> TenantConfig:
    """Build the 'default' (Orchelix) config from tools.py canonical constants.

    Late import avoids an import cycle (tools.py imports this module at top).
    Called only at request time, never during module import.
    """
    from tools import _BUSINESS_TZ, _HOURS, _PRICING, _SLOT_MIN

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
    )


def _config_from_file(tenant_id: str, data: dict) -> TenantConfig:
    """Build a TenantConfig from a tenants/<id>/config.json dict.

    Missing keys fall back to the default tenant's values so a partial
    config.json is valid.
    """
    base = _default_config()
    emails = data.get("emails") or {}
    hours = data.get("business_hours") or list(base.business_hours)
    vapi = data.get("vapi") or {}
    return TenantConfig(
        tenant_id=tenant_id,
        company_name=data.get("company_name", base.company_name),
        business_tz=data.get("business_tz", base.business_tz),
        business_hours=(int(hours[0]), int(hours[1])),
        slot_minutes=int(data.get("slot_minutes", base.slot_minutes)),
        email_from=emails.get("from", base.email_from),
        email_booking_to=emails.get("booking_to", base.email_booking_to),
        email_escalation_to=emails.get("escalation_to", base.email_escalation_to),
        sms_signature=data.get("sms_signature", data.get("company_name", base.sms_signature)),
        voice_default_summary=data.get("voice_default_summary", base.voice_default_summary),
        pricing=data.get("pricing") or list(base.pricing),
        vapi_assistant_ids=tuple(vapi.get("assistant_ids") or ()),
        vapi_phone_number_ids=tuple(vapi.get("phone_number_ids") or ()),
        calendar_id=data.get("calendar_id", "primary"),
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
