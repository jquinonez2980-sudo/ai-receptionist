"""Tenant resolution + approve-to-activate gate (Phase 4 ticket 4.1, stage 1).

No model, no DB required. The DB layer is monkeypatched at tenants._db_tenant_status
/ _db_tenant_ids so every branch of the fallback matrix is exercised deterministically,
including the two that only occur in production: the deploy window before alembic
0006 has run, and a DB outage.

The matrix these lock in:

    has dir?  DB row?      exists()  is_active()   why
    -------   ----------   --------  -----------   ----------------------------
    yes       (any)        True      per row       live tenants today
    yes       none         True      True          pre-0006 tenant, missing row
    yes       unavailable  True      True          DB outage must not deplatform
    no        'active'     True      True          approved self-serve tenant
    no        'submitted'  True      False         THE GATE
    no        none         False     False         genuinely unknown
    no        unavailable  False     False         outage, unknown to filesystem

Run: PYTHONUTF8=1 pytest evals/test_onboarding_gate.py -v
"""

import pytest

import tenants as T
from tenants import (
    ACTIVE_ONBOARDING_STATUS,
    clear_tenant_cache,
    resolve_vapi_tenant,
    tenant_exists,
    tenant_is_active,
    tenant_onboarding_status,
)

# A tenant id that is guaranteed NOT to have a tenants/<id>/ directory —
# stands in for a signup-created, DB-only tenant.
DB_ONLY = "selfserve-test-co"
# A tenant that really does have a directory in the repo.
ON_DISK = "acme"


@pytest.fixture(autouse=True)
def _clean_cache():
    """Status is cached for 60s — clear around every test so monkeypatched DB
    answers are never served from a previous test's cache."""
    clear_tenant_cache()
    yield
    clear_tenant_cache()


def _stub_status(monkeypatch, mapping: dict):
    """Point _db_tenant_status at a dict. A missing key means "queried, no row";
    the _UNAVAILABLE sentinel means the DB could not answer."""
    monkeypatch.setattr(
        T, "_db_tenant_status", lambda tid: mapping.get(tid, None)
    )


# ── existence ────────────────────────────────────────────────────────────────

def test_filesystem_tenant_exists_without_any_db(monkeypatch):
    _stub_status(monkeypatch, {})
    assert tenant_exists(ON_DISK)
    assert tenant_exists("default")


def test_db_only_tenant_exists(monkeypatch):
    _stub_status(monkeypatch, {DB_ONLY: "submitted"})
    assert tenant_exists(DB_ONLY)


def test_unknown_tenant_still_does_not_exist(monkeypatch):
    _stub_status(monkeypatch, {})
    assert not tenant_exists("nope-not-a-tenant")


def test_db_outage_does_not_invent_a_tenant(monkeypatch):
    monkeypatch.setattr(T, "_db_tenant_status", lambda tid: T._UNAVAILABLE)
    assert not tenant_exists(DB_ONLY)
    # ...but must not deplatform one that has a directory
    assert tenant_exists(ON_DISK)


# ── the gate ─────────────────────────────────────────────────────────────────

def test_pending_tenant_exists_but_is_not_active(monkeypatch):
    """The whole point of the ticket: signed up, visible, NOT serving traffic."""
    for pending in ("draft", "submitted", "provisioning", "review", "rejected"):
        clear_tenant_cache()
        _stub_status(monkeypatch, {DB_ONLY: pending})
        assert tenant_exists(DB_ONLY), pending
        assert not tenant_is_active(DB_ONLY), pending


def test_approved_tenant_is_active(monkeypatch):
    _stub_status(monkeypatch, {DB_ONLY: ACTIVE_ONBOARDING_STATUS})
    assert tenant_is_active(DB_ONLY)


def test_default_is_always_active(monkeypatch):
    """Orchelix's own tenant is code-canonical and was never onboarded — it
    must not depend on a DB row existing."""
    monkeypatch.setattr(T, "_db_tenant_status", lambda tid: T._UNAVAILABLE)
    assert tenant_is_active("default")
    assert tenant_onboarding_status("default") == ACTIVE_ONBOARDING_STATUS


def test_legacy_filesystem_tenant_active_without_db_row(monkeypatch):
    """A tenant that went live before this column existed has no row yet. A
    missing row must never take a paying customer off the air."""
    _stub_status(monkeypatch, {})
    assert tenant_is_active(ON_DISK)


def test_db_outage_keeps_live_tenants_serving(monkeypatch):
    """Covers the manual-migration deploy window too: before alembic 0006 runs,
    the onboarding_status query errors and lands on this same path."""
    monkeypatch.setattr(T, "_db_tenant_status", lambda tid: T._UNAVAILABLE)
    assert tenant_is_active(ON_DISK)
    assert tenant_is_active("default")


def test_db_row_can_gate_a_filesystem_tenant(monkeypatch):
    """Direction that matters for suspension: an explicit non-active row wins
    over the mere presence of a directory."""
    _stub_status(monkeypatch, {ON_DISK: "review"})
    assert tenant_exists(ON_DISK)
    assert not tenant_is_active(ON_DISK)


# ── caching ──────────────────────────────────────────────────────────────────

def test_status_is_cached_between_calls(monkeypatch):
    calls = []

    def counting(tid):
        calls.append(tid)
        return ACTIVE_ONBOARDING_STATUS

    monkeypatch.setattr(T, "_db_tenant_status", counting)
    tenant_is_active(DB_ONLY)
    tenant_is_active(DB_ONLY)
    tenant_is_active(DB_ONLY)
    assert len(calls) == 1


def test_unavailable_is_not_cached(monkeypatch):
    """A transient blip must recover on the next request, not stick for a
    full 60s TTL."""
    calls = []

    def failing(tid):
        calls.append(tid)
        return T._UNAVAILABLE

    monkeypatch.setattr(T, "_db_tenant_status", failing)
    tenant_exists(DB_ONLY)
    tenant_exists(DB_ONLY)
    assert len(calls) == 2


def test_clear_cache_drops_status(monkeypatch):
    _stub_status(monkeypatch, {DB_ONLY: "submitted"})
    assert not tenant_is_active(DB_ONLY)
    _stub_status(monkeypatch, {DB_ONLY: ACTIVE_ONBOARDING_STATUS})
    assert not tenant_is_active(DB_ONLY), "should still be serving the cached value"
    clear_tenant_cache(DB_ONLY)
    assert tenant_is_active(DB_ONLY), "approval must take effect after cache clear"


# ── tenant id listing ────────────────────────────────────────────────────────

def test_all_tenant_ids_unions_filesystem_and_db(monkeypatch):
    monkeypatch.setattr(T, "_db_tenant_ids", lambda: [DB_ONLY, ON_DISK, "default"])
    ids = T._all_tenant_ids()
    assert DB_ONLY in ids, "a DB-only tenant must be reachable for VAPI routing"
    assert ON_DISK in ids
    assert "default" not in ids, "'default' is excluded by contract"
    assert ids == sorted(set(ids)), "sorted + deduplicated"


def test_all_tenant_ids_survives_db_outage(monkeypatch):
    monkeypatch.setattr(T, "_db_tenant_ids", lambda: None)
    ids = T._all_tenant_ids()
    assert ON_DISK in ids
    assert DB_ONLY not in ids


# ── VAPI inbound routing ─────────────────────────────────────────────────────

def _vapi_payload(assistant_id: str) -> dict:
    return {"message": {"call": {"assistantId": assistant_id}}}


def _stub_configs(monkeypatch, assistant_owner: str):
    """Give ONLY assistant_owner the test assistant id. _all_tenant_ids() also
    returns the real on-disk tenants, so every other tenant must come back with
    empty VAPI ids or whichever sorts first would match instead."""
    def fake_load(tid):
        return T.TenantConfig(
            tenant_id=tid,
            company_name="X",
            business_tz="UTC",
            business_hours=(9, 17),
            slot_minutes=30,
            email_from="a@b.c",
            email_booking_to="a@b.c",
            email_escalation_to="a@b.c",
            sms_signature="X",
            voice_default_summary="X",
            vapi_assistant_ids=("asst_123",) if tid == assistant_owner else (),
        )

    monkeypatch.setattr(T, "load_tenant", fake_load)


def test_vapi_routes_to_an_active_tenant(monkeypatch):
    monkeypatch.setattr(T, "_db_tenant_ids", lambda: [DB_ONLY])
    _stub_status(monkeypatch, {DB_ONLY: ACTIVE_ONBOARDING_STATUS})
    _stub_configs(monkeypatch, DB_ONLY)
    assert resolve_vapi_tenant(_vapi_payload("asst_123")) == DB_ONLY


def test_vapi_refuses_a_pending_tenant(monkeypatch):
    """An assistant id wired up before approval must not serve calls as that
    tenant — it falls back to default rather than going live early."""
    monkeypatch.setattr(T, "_db_tenant_ids", lambda: [DB_ONLY])
    _stub_status(monkeypatch, {DB_ONLY: "review"})
    _stub_configs(monkeypatch, DB_ONLY)
    assert resolve_vapi_tenant(_vapi_payload("asst_123")) == "default"
