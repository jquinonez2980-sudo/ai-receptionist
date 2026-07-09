"""Multi-tenancy tests (Phase 1 of the SaaS plan).

Fast unit tests (no model) lock in tenant isolation: config, secrets, thread
namespacing, VAPI mapping, and prompt resolution. One end-to-end test runs an
Acme conversation through the REAL multi-agent graph and asserts it serves
Acme's pricing — never Orchelix's — proving tenant context flows through
routing → prompt → tools together.

Run: PYTHONUTF8=1 pytest evals/test_multi_tenant.py -v
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()  # populate OPENAI_API_KEY before the skipif below is evaluated

from tenants import (  # noqa: E402
    load_tenant,
    namespaced_thread,
    resolve_vapi_tenant,
    tenant_exists,
    tenant_secret,
)

# ── Tenant config isolation (no model) ───────────────────────────────────────

def test_default_tenant_is_orchelix():
    d = load_tenant("default")
    assert d.company_name == "Orchelix AI Consulting"
    assert d.business_tz == "America/Toronto"
    assert d.business_hours == (9, 17)
    # default pricing comes from tools._PRICING (canonical)
    assert any("Esmi" in p["name"] for p in d.pricing)
    assert any(p["setup_from"] == 8500 for p in d.pricing)


def test_acme_tenant_config_is_isolated():
    a = load_tenant("acme")
    assert a.company_name == "Acme Dental Care"
    assert a.business_tz == "America/New_York"
    assert a.business_hours == (8, 18)
    # acme pricing is its own — no Orchelix products
    assert any("Invisalign" in p["name"] for p in a.pricing)
    assert not any("Esmi" in p["name"] for p in a.pricing)
    assert a.email_escalation_to == "drsmith@acmedental.example"


def test_unknown_tenant_does_not_exist():
    assert tenant_exists("default")
    assert tenant_exists("acme")
    assert not tenant_exists("nope-not-a-tenant")


# ── Secret isolation (no model) ──────────────────────────────────────────────

def test_secret_isolation(monkeypatch):
    monkeypatch.setenv("SENDGRID_API_KEY", "GLOBAL")
    monkeypatch.setenv("TENANT_ACME_SENDGRID_API_KEY", "ACME")
    # default reads the global var
    assert tenant_secret("default", "SENDGRID_API_KEY") == "GLOBAL"
    # acme reads only its namespaced var
    assert tenant_secret("acme", "SENDGRID_API_KEY") == "ACME"


def test_tenant_cannot_read_global_secret(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "GLOBAL_TWILIO")
    monkeypatch.delenv("TENANT_ACME_TWILIO_AUTH_TOKEN", raising=False)
    # acme must NOT fall back to the global — would leak default creds across tenants
    assert tenant_secret("acme", "TWILIO_AUTH_TOKEN") is None
    assert tenant_secret("default", "TWILIO_AUTH_TOKEN") == "GLOBAL_TWILIO"


# ── Thread namespacing (no model) ────────────────────────────────────────────

def test_thread_namespacing_isolates_tenants():
    # Same client thread_id, different tenants → different checkpoint threads.
    assert namespaced_thread("acme", "abc") == "acme:abc"
    assert namespaced_thread("globex", "abc") == "globex:abc"
    # default is NOT prefixed (preserves existing Orchelix threads)
    assert namespaced_thread("default", "abc") == "abc"


# ── VAPI inbound mapping (no model) ──────────────────────────────────────────

def test_vapi_assistant_maps_to_tenant():
    assert resolve_vapi_tenant({"message": {"assistantId": "acme-vapi-assistant-001"}}) == "acme"


def test_vapi_phone_maps_to_tenant():
    assert resolve_vapi_tenant({"message": {"phoneNumberId": "acme-phone-001"}}) == "acme"


def test_vapi_unknown_falls_back_to_default():
    assert resolve_vapi_tenant({"message": {"assistantId": "unknown"}}) == "default"
    assert resolve_vapi_tenant({}) == "default"


# ── Multi-location tenant (Otro Nivel) ───────────────────────────────────────

def test_otro_nivel_is_multi_location():
    from tenants import clear_tenant_cache

    clear_tenant_cache("otro-nivel")
    o = load_tenant("otro-nivel")
    assert o.company_name == "Otro Nivel Barbershop"
    assert o.is_multi_location
    assert set(o.locations.keys()) == {"weston", "keele"}
    weston = o.resolve_location("weston")
    # Python weekday: Mon=0 … Sun=6. Saturday (5) open but not bookable.
    assert 5 in weston.business_days
    assert 5 not in weston.effective_booking_days
    assert weston.hours_for_day(0) == (10, 19)  # Monday
    assert weston.hours_for_day(6) == (10, 17)  # Sunday
    fade = o.resolve_service("fade")
    assert fade is not None
    assert fade.duration_min == 45
    assert fade.price_for("weston") == "$50"
    assert fade.price_for("keele") == "$35–$40"


def test_single_location_tenants_still_synthesize_default():
    f = load_tenant("fresh-cuts")
    assert not f.is_multi_location
    loc = f.default_location()
    assert loc.calendar_id == f.calendar_id or loc.calendar_id == "primary"
    assert loc.effective_booking_days == f.business_days


# ── Prompt resolution (no model) ─────────────────────────────────────────────

def test_prompt_company_substitution():
    from agents import _load_tenant_prompt
    d = _load_tenant_prompt("informer.md", "default")
    assert "Orchelix AI Consulting" in d
    assert "{company}" not in d
    a = _load_tenant_prompt("informer.md", "acme")
    assert "Acme Dental Care" in a
    assert "Orchelix" not in a
    assert "{company}" not in a


# ── Per-tenant calendar isolation (no model) ─────────────────────────────────

def test_calendar_service_raises_for_unconfigured_tenant(monkeypatch):
    """_get_calendar_service raises RuntimeError for a tenant with no credentials."""
    from tools import _CAL_SERVICE_CACHE, _get_calendar_service

    # Clear cache so our tenant isn't accidentally served from a prior test run
    _CAL_SERVICE_CACHE.pop("acme", None)

    # Ensure no TENANT_ACME_GOOGLE_TOKEN_B64 or individual vars exist
    monkeypatch.delenv("TENANT_ACME_GOOGLE_TOKEN_B64", raising=False)
    monkeypatch.delenv("TENANT_ACME_GOOGLE_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("TENANT_ACME_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("TENANT_ACME_GOOGLE_CLIENT_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="Calendar not configured for tenant acme"):
        _get_calendar_service("acme")


def test_calendar_id_defaults_to_primary():
    """TenantConfig.calendar_id defaults to 'primary' for unconfigured tenants."""
    cfg = load_tenant("default")
    assert cfg.calendar_id == "primary"


# ── End-to-end: Acme through the REAL multi-agent graph (model) ──────────────

@pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — end-to-end test calls the real model.",
)
def test_acme_pricing_end_to_end_real_graph():
    """A pricing question as tenant=acme must return Acme's pricing, not Orchelix's.

    Exercises the full stack together: _route → informer node → dynamic prompt
    (runtime.context → Acme persona) → get_pricing tool (RunnableConfig → Acme
    pricing). Proves tenant context propagates through every layer at once.
    """
    from langgraph.checkpoint.memory import MemorySaver

    from graph import _build_multi_agent_graph

    g = _build_multi_agent_graph(MemorySaver())
    out = g.invoke(
        {"messages": [("user", "What are your prices? Give me exact numbers.")]},
        config={"configurable": {"thread_id": "acme-e2e", "tenant_id": "acme"}},
        context={"tenant_id": "acme"},
    )
    text = out["messages"][-1].content

    # Acme's pricing must appear …
    assert ("Invisalign" in text or "New Patient" in text or "149" in text), (
        f"expected Acme pricing in reply, got: {text[:300]!r}"
    )
    # … and Orchelix's must NOT leak through.
    assert "8,500" not in text and "Firm OS" not in text, (
        f"Orchelix pricing leaked into Acme reply: {text[:300]!r}"
    )
