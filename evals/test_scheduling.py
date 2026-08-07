"""GET /platform/scheduling/status (dashboard Scheduling page,
docs/ESMI_DASHBOARD_UX.md Section 5.4).

No live Google Calendar in this environment — resolve_google_credentials and
googleapiclient.discovery.build are both faked. What matters here:

  * Auth (platform secret + tenant header), same as every /platform/* route.
  * Tenant scoping: resolve_google_credentials is always called with the
    REQUEST tenant, never "default" — the isolation guarantee this endpoint
    exists to respect, not just document.
  * A calendar_id that resolves to Orchelix's shared "primary" alias for a
    non-default tenant is reported as misconfigured WITHOUT ever calling the
    Google API (never probed) — checked at the code-path level (did
    .freebusy() ever get called), not just the response shape.
  * "default" tenant's OWN "primary" calendar is legitimately Orchelix's own
    and must still be probed normally — the skip is non-default-only.
  * Missing credentials / a failed freebusy probe both degrade to a clear
    connected: false response — never a 500.
  * hours summary is a read-only echo of cfg, not a live Calendar read.

Run: PYTHONUTF8=1 pytest evals/test_scheduling.py -v
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.scheduling as sched
from tenants import LocationConfig, TenantConfig

SECRET = "test-platform-secret"
TID = "otro-nivel"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": TID}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(sched.router)
    return TestClient(app)


def fake_cfg(**over) -> TenantConfig:
    base = dict(
        tenant_id=TID,
        company_name="Otro Nivel Barbershop",
        business_tz="America/Toronto",
        business_hours=(9, 17),
        slot_minutes=30,
        email_from="x@example.com",
        email_booking_to="x@example.com",
        email_escalation_to="x@example.com",
        sms_signature="",
        voice_default_summary="",
        calendar_id="cal-otro-nivel@group.calendar.google.com",
    )
    base.update(over)
    return TenantConfig(**base)


class FakeCreds:
    pass


class FakeQuery:
    def __init__(self, outcome):
        self._outcome = outcome

    def execute(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeFreebusy:
    def __init__(self, outcome):
        self._outcome = outcome

    def query(self, body):
        return FakeQuery(self._outcome)


class FakeService:
    """Records whether .freebusy() was ever called, so isolation tests can
    assert "never probed" at the code-path level."""

    def __init__(self, outcome=None):
        self._outcome = outcome if outcome is not None else {}
        self.freebusy_called = False

    def freebusy(self):
        self.freebusy_called = True
        return FakeFreebusy(self._outcome)


# ── auth ──────────────────────────────────────────────────────────────────


def test_requires_platform_secret(client):
    r = client.get("/platform/scheduling/status", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


def test_requires_tenant(client):
    r = client.get("/platform/scheduling/status", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


# ── tenant scoping / isolation ──────────────────────────────────────────────


def test_resolves_credentials_for_the_request_tenant_not_default(client, monkeypatch):
    calls = []
    monkeypatch.setattr(sched, "load_tenant", lambda tid: fake_cfg(tenant_id=tid))
    monkeypatch.setattr(
        "tools.resolve_google_credentials",
        lambda tenant_id: (calls.append(tenant_id), FakeCreds())[1],
    )
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: FakeService())

    client.get("/platform/scheduling/status", headers=HEADERS)

    assert calls == [TID]
    assert "default" not in calls


@pytest.mark.parametrize("bad_calendar_id", ["", "primary", "PRIMARY", "  "])
def test_orchelix_shared_calendar_is_never_probed(client, monkeypatch, bad_calendar_id):
    monkeypatch.setattr(
        sched, "load_tenant", lambda tid: fake_cfg(calendar_id=bad_calendar_id)
    )
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())
    service = FakeService()
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: service)

    r = client.get("/platform/scheduling/status", headers=HEADERS)
    body = r.json()

    assert body["connected"] is False
    assert body["calendars"][0]["reachable"] is False
    assert body["calendars"][0]["detail"]
    assert service.freebusy_called is False, "must never probe Orchelix's shared calendar"


def test_default_tenants_own_primary_calendar_is_probed_normally(client, monkeypatch):
    """"primary" for tenant_id == "default" IS legitimately Orchelix's own
    calendar — the never-probe skip only applies to non-default tenants."""
    monkeypatch.setattr(
        sched, "load_tenant",
        lambda tid: fake_cfg(tenant_id="default", calendar_id="primary"),
    )
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())
    service = FakeService()
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: service)

    r = client.get(
        "/platform/scheduling/status",
        headers={"X-Platform-Secret": SECRET, "X-Tenant-Id": "default"},
    )
    body = r.json()

    assert r.status_code == 200, r.text
    assert body["calendars"][0]["reachable"] is True
    assert service.freebusy_called is True


# ── degrades cleanly, never 500s ─────────────────────────────────────────────


def test_missing_credentials_is_a_clear_not_connected_shape(client, monkeypatch):
    monkeypatch.setattr(sched, "load_tenant", lambda tid: fake_cfg())

    def boom(tenant_id):
        raise RuntimeError(f"Calendar not configured for tenant {tenant_id}")

    monkeypatch.setattr("tools.resolve_google_credentials", boom)

    r = client.get("/platform/scheduling/status", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is False
    assert body["calendars"][0]["reachable"] is False
    assert body["calendars"][0]["detail"]
    assert body["detail"]


def test_freebusy_probe_failure_is_not_connected_not_500(client, monkeypatch):
    monkeypatch.setattr(sched, "load_tenant", lambda tid: fake_cfg())
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())
    monkeypatch.setattr(
        "googleapiclient.discovery.build",
        lambda *a, **kw: FakeService(RuntimeError("403 calendarNotFound")),
    )

    r = client.get("/platform/scheduling/status", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connected"] is False
    assert body["calendars"][0]["reachable"] is False


def test_calendar_client_init_failure_is_not_connected_not_500(client, monkeypatch):
    monkeypatch.setattr(sched, "load_tenant", lambda tid: fake_cfg())
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())

    def boom(*a, **kw):
        raise RuntimeError("discovery doc fetch failed")

    monkeypatch.setattr("googleapiclient.discovery.build", boom)

    r = client.get("/platform/scheduling/status", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["connected"] is False


# ── happy path ────────────────────────────────────────────────────────────────


def test_connected_happy_path(client, monkeypatch):
    monkeypatch.setattr(sched, "load_tenant", lambda tid: fake_cfg())
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: FakeService({}))

    r = client.get("/platform/scheduling/status", headers=HEADERS)
    body = r.json()

    assert r.status_code == 200, r.text
    assert body["connected"] is True
    assert body["detail"] is None
    assert body["calendars"][0]["reachable"] is True
    assert body["calendars"][0]["calendar_id"] == "cal-otro-nivel@group.calendar.google.com"


# ── hours summary (read-only echo of cfg) ──────────────────────────────────────


def test_hours_summary_reflects_cfg_single_location(client, monkeypatch):
    monkeypatch.setattr(
        sched, "load_tenant",
        lambda tid: fake_cfg(business_hours=(8, 18), business_days=(0, 1, 2, 3, 4, 5)),
    )
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: FakeService({}))

    body = client.get("/platform/scheduling/status", headers=HEADERS).json()
    assert body["hours"]["business_hours"] == [8, 18]
    assert body["hours"]["business_days"] == [0, 1, 2, 3, 4, 5]
    assert body["hours"]["locations"] is None


def test_multi_location_mixed_calendar_status_and_hours(client, monkeypatch):
    locs = {
        "shopA": LocationConfig(
            id="shopA", name="Shop A",
            calendar_id="a@group.calendar.google.com",
            business_hours=(10, 19),
        ),
        "shopB": LocationConfig(id="shopB", name="Shop B", calendar_id="primary"),
    }
    monkeypatch.setattr(sched, "load_tenant", lambda tid: fake_cfg(locations=locs))
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: FakeService({}))

    body = client.get("/platform/scheduling/status", headers=HEADERS).json()

    assert body["connected"] is False  # one location still misconfigured
    by_loc = {c["location_id"]: c for c in body["calendars"]}
    assert by_loc["shopA"]["reachable"] is True
    assert by_loc["shopB"]["reachable"] is False
    assert body["hours"]["locations"]["shopA"]["name"] == "Shop A"
    assert body["hours"]["locations"]["shopA"]["business_hours"] == [10, 19]
    assert body["hours"]["locations"]["shopB"]["name"] == "Shop B"


# ── default tenant ──────────────────────────────────────────────────────────


def test_default_tenant_is_not_rejected(client, monkeypatch):
    """Unlike knowledge.py, the default/Orchelix tenant has a real calendar
    and must not be blocked from checking its own status."""
    monkeypatch.setattr(
        sched, "load_tenant",
        lambda tid: fake_cfg(tenant_id="default", calendar_id="c@group.calendar.google.com"),
    )
    monkeypatch.setattr("tools.resolve_google_credentials", lambda tid: FakeCreds())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: FakeService({}))

    r = client.get(
        "/platform/scheduling/status",
        headers={"X-Platform-Secret": SECRET, "X-Tenant-Id": "default"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == "default"
