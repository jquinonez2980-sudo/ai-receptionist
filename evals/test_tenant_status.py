"""GET /platform/tenant-status (Phase 4 ticket 4.1 + the onboarding voice gate,
docs/ESMI_DASHBOARD_UX.md Section 7 Step 3).

tenant_state()/tenant_is_active() are the real gating functions used
elsewhere (voice/chat/booking) — this route just reads and reshapes them, so
tests monkeypatch those two directly rather than faking a DB connection.

Run: PYTHONUTF8=1 pytest evals/test_tenant_status.py -v
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.tenant_status as ts
from tenants import TenantState

SECRET = "test-platform-secret"
TID = "otro-nivel"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": TID}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(ts.router)
    return TestClient(app)


def test_never_previewed_is_false(client, monkeypatch):
    monkeypatch.setattr(
        ts,
        "tenant_state",
        lambda tid: TenantState(
            onboarding_status="review", account_status="live", plan="managed",
            onboarding_voice_previewed_at=None,
        ),
    )
    monkeypatch.setattr(ts, "tenant_is_active", lambda tid: False)

    r = client.get("/platform/tenant-status", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["onboarding_voice_previewed"] is False


def test_previewed_is_true(client, monkeypatch):
    monkeypatch.setattr(
        ts,
        "tenant_state",
        lambda tid: TenantState(
            onboarding_status="review", account_status="live", plan="managed",
            onboarding_voice_previewed_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        ),
    )
    monkeypatch.setattr(ts, "tenant_is_active", lambda tid: False)

    r = client.get("/platform/tenant-status", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["onboarding_voice_previewed"] is True


def test_unknown_tenant_defaults_to_false(client, monkeypatch):
    monkeypatch.setattr(ts, "tenant_state", lambda tid: None)
    monkeypatch.setattr(ts, "tenant_is_active", lambda tid: False)

    r = client.get("/platform/tenant-status", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["onboarding_voice_previewed"] is False


def test_active_tenant_that_already_previewed_stays_true(client, monkeypatch):
    """An already-active tenant who previewed during onboarding keeps
    reporting True — nothing here re-locks a tenant that finished onboarding
    (the gate route itself never re-checks after go-live)."""
    monkeypatch.setattr(
        ts,
        "tenant_state",
        lambda tid: TenantState(
            onboarding_status="active", account_status="live", plan="managed",
            onboarding_voice_previewed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
    )
    monkeypatch.setattr(ts, "tenant_is_active", lambda tid: True)

    r = client.get("/platform/tenant-status", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_serve_traffic"] is True
    assert body["onboarding_voice_previewed"] is True
