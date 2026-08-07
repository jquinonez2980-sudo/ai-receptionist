"""platform_api/voice_sync.py — POST /platform/voice/apply, Voice Studio's
"Apply to live Esmi" button (docs/ESMI_DASHBOARD_UX.md Section 12.1).

Never hits the real VAPI network: the PATCH-preserving-other-keys mechanics
live in vapi_voice_sync.py (already covered by evals/test_vapi_voice_sync.py)
and are exercised here only through vapi_voice_sync.vapi_api, monkeypatched
with a fake recorder — same seam the CLI's own tests use.

What matters here:
  1. Auth: missing X-Platform-Secret -> 401; missing X-Tenant-Id -> 400 —
     same require_tenant()/verify_platform_secret() every other /platform/*
     route uses, not reimplemented.
  2. The allow-list is enforced here too, independently of the CLI's own
     check — "default"/Orchelix and any other tenant get a 403 naming the
     allow-listed set, before load_tenant or any VAPI call.
  3. No assistant configured / no voice saved / unmapped voice_id each fail
     loudly (409/409/503) before any VAPI call.
  4. dry_run=true computes and returns the exact payload without PATCHing.
  5. The success path PATCHes, verifies, and returns applied=true with the
     before/after voice blocks.
  6. Already-in-sync makes no PATCH call and still reports applied=true
     (nothing failed) — `assistants[].changed`/`message` carry the "nothing
     needed doing" detail rather than that collapsing into applied=false.
  7. A VAPI failure on one assistant is surfaced in that assistant's `error`
     and drops `applied` to false overall, rather than a 500.

Run: PYTHONUTF8=1 pytest evals/test_voice_sync.py -v
"""

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import platform_api.voice_sync as vs
import vapi_voice_sync as vvs

SECRET = "test-platform-secret"
TID = "otro-nivel"
HEADERS = {"X-Platform-Secret": SECRET, "X-Tenant-Id": TID}

AID = "32994d60-3712-4183-a7db-edc3badeabec"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_SECRET", SECRET)
    app = FastAPI()
    app.include_router(vs.router)
    return TestClient(app)


class FakeApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, api_key, body=None):
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError(f"unexpected extra vapi_api call: {method} {path}")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _voice_catalog(monkeypatch):
    # vs.VOICE_LIBRARY is voice_sync.py's own `from voice_library import
    # VOICE_LIBRARY` copy of the name — patching voice_library.VOICE_LIBRARY
    # itself (a different module's attribute) would leave this one pointing
    # at the real production catalog.
    monkeypatch.setattr(vs, "VOICE_LIBRARY", {"esmi-default": "el_real_voice_id"})


@pytest.fixture(autouse=True)
def _vapi_key(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "test-vapi-key")


def fake_tenant(voice_id="esmi-default", speed=1.0, assistant_ids=(AID,)):
    """load_tenant() stand-in — the endpoint only reads .voice_id and .speed
    (assistant ids come from assistant_ids_for(), monkeypatched separately
    in each test), so a full TenantConfig (many required fields, no
    defaults) would be pure noise here."""
    return types.SimpleNamespace(voice_id=voice_id, speed=speed, vapi_assistant_ids=tuple(assistant_ids))


# ── auth ──────────────────────────────────────────────────────────────────


def test_requires_platform_secret(client):
    r = client.post("/platform/voice/apply", headers={"X-Tenant-Id": TID})
    assert r.status_code == 401


def test_requires_tenant_header(client):
    r = client.post("/platform/voice/apply", headers={"X-Platform-Secret": SECRET})
    assert r.status_code == 400


# ── allow-list ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tenant_id", ["default", "acme"])
def test_non_allowlisted_tenant_is_403_before_any_lookup(client, monkeypatch, tenant_id):
    monkeypatch.setattr(
        vs, "load_tenant", lambda tid: (_ for _ in ()).throw(
            AssertionError("load_tenant must not be called for a non-allow-listed tenant")
        )
    )
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post(
        "/platform/voice/apply", headers={"X-Platform-Secret": SECRET, "X-Tenant-Id": tenant_id}
    )

    assert r.status_code == 403
    assert "otro-nivel" in r.json()["detail"]
    assert "coastline-condos" in r.json()["detail"]
    assert fake_api.calls == []


def test_allowlisted_tenants_match_the_cli(client):
    assert vs.SYNC_ALLOWED_TENANTS == frozenset({"otro-nivel", "coastline-condos"})


# ── pre-flight refusals (before any VAPI call) ──────────────────────────────


def test_no_assistant_configured_is_409(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [])
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 409
    assert fake_api.calls == []


def test_no_voice_saved_is_409(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(voice_id=""))
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 409
    assert "Save first" in r.json()["detail"] or "save" in r.json()["detail"].lower()
    assert fake_api.calls == []


def test_unmapped_voice_id_is_503(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(voice_id="not-a-real-voice"))
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 503
    assert "not-a-real-voice" in r.json()["detail"]
    assert fake_api.calls == []


def test_missing_vapi_key_is_503(client, monkeypatch):
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant())

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 503
    assert "VAPI_API_KEY" in r.json()["detail"]


# ── dry_run ───────────────────────────────────────────────────────────────


def test_dry_run_never_calls_patch(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.1))
    current_assistant = {
        "name": "Otro Nivel Esmi",
        "voice": {"provider": "11labs", "voiceId": "old_voice_id", "speed": 1.0, "stability": 0.5},
    }
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply?dry_run=true", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert fake_api.calls == [("GET", f"/assistant/{AID}", None)]
    assert body["dry_run"] is True
    assert body["applied"] is False
    assert body["assistant_id"] == AID
    assert body["after"] == {
        "provider": "11labs",
        "voiceId": "el_real_voice_id",
        "speed": 1.1,
        "stability": 0.5,
    }
    assert body["assistants"][0]["changed"] is True
    assert body["assistants"][0]["verified"] is None


# ── success path ─────────────────────────────────────────────────────────


def test_apply_patches_and_returns_before_after(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.1))
    current_assistant = {
        "name": "Otro Nivel Esmi",
        "voice": {"provider": "11labs", "voiceId": "old_voice_id", "speed": 1.0, "stability": 0.5},
    }
    verify_assistant = {
        "voice": {"provider": "11labs", "voiceId": "el_real_voice_id", "speed": 1.1, "stability": 0.5}
    }
    fake_api = FakeApi([current_assistant, {}, verify_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert [c[:2] for c in fake_api.calls] == [
        ("GET", f"/assistant/{AID}"),
        ("PATCH", f"/assistant/{AID}"),
        ("GET", f"/assistant/{AID}"),
    ]
    assert body["applied"] is True
    assert body["dry_run"] is False
    assert body["tenant_id"] == TID
    assert body["assistant_id"] == AID
    assert body["before"] == {
        "provider": "11labs", "voiceId": "old_voice_id", "speed": 1.0, "stability": 0.5
    }
    assert body["after"] == {
        "provider": "11labs", "voiceId": "el_real_voice_id", "speed": 1.1, "stability": 0.5
    }
    assert "New callers will hear this voice" in body["message"]
    assert body["assistants"][0]["verified"] is True


def test_already_in_sync_makes_no_patch_but_still_reports_applied(client, monkeypatch):
    """`applied` means "completed without errors", not "a byte changed" —
    already-in-sync is a successful outcome (nothing failed), just with no
    PATCH call. `assistants[].changed` / `message` carry the "nothing
    needed doing" detail for the frontend to show distinctly."""
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.0))
    current_assistant = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.0}}
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert fake_api.calls == [("GET", f"/assistant/{AID}", None)]  # no PATCH
    assert body["applied"] is True
    assert "up to date" in body["message"].lower()
    assert body["assistants"][0]["changed"] is False
    assert body["assistants"][0]["applied"] is False  # per-assistant: no PATCH was needed


def test_vapi_failure_is_reported_not_500(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.1))
    current_assistant = {"voice": {"voiceId": "old_voice_id", "speed": 1.0}}

    def flaky(method, path, api_key, body=None):
        if method == "PATCH":
            raise vvs.VapiSyncError(f"{method} {path} -> HTTP 500: boom")
        return current_assistant

    monkeypatch.setattr(vvs, "vapi_api", flaky)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is False
    assert body["assistants"][0]["error"] is not None
    assert "boom" in body["assistants"][0]["error"]
