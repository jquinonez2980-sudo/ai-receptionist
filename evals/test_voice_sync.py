"""platform_api/voice_sync.py — POST /platform/voice/apply, Voice Studio's
"Apply to live Esmi" button (docs/ESMI_DASHBOARD_UX.md Section 12.1).

Never hits the real VAPI network: the PATCH mechanics live in
vapi_voice_sync.py (already covered by evals/test_vapi_voice_sync.py) and
are exercised here only through vapi_voice_sync.vapi_api, monkeypatched
with a fake recorder — same seam the CLI's own tests use.

Response shape: each assistant entry is
  {assistant_id, name, voice: {before, after, changed, applied, verified,
  error}, greeting: {...same shape...} | None}
`greeting` is None whenever TenantConfig.greeting is empty — voice and
greeting are two independent PATCH targets on the same assistant, and a
tenant can have one saved without the other.

What matters here:
  1. Auth: missing X-Platform-Secret -> 401; missing X-Tenant-Id -> 400 —
     same require_tenant()/verify_platform_secret() every other /platform/*
     route uses, not reimplemented.
  2. The allow-list is enforced here too, independently of the CLI's own
     check — "default"/Orchelix and any other tenant get a 403 naming the
     allow-listed set, before load_tenant or any VAPI call. Applies to
     greeting too, not just voice.
  3. No assistant configured / no voice saved / unmapped voice_id each fail
     loudly (409/409/503) before any VAPI call — a missing voice_id blocks
     the whole request even when greeting is set, since voice is the
     always-required half of this endpoint.
  4. Empty greeting: no greeting plan is ever built, no extra GET/PATCH for
     it, response `greeting` is None — an empty TenantConfig.greeting must
     never blank out an assistant's existing firstMessage.
  5. Non-empty greeting: planned and (unless dry_run) PATCHed independently
     of voice, with its own {"firstMessage": ...} payload — never merged
     into the voice PATCH body.
  6. dry_run=true computes and returns both payloads without PATCHing
     either.
  7. Already-in-sync (voice and/or greeting) makes no PATCH call for that
     part and still reports applied=true overall (nothing failed).
  8. A VAPI failure on either voice or greeting is surfaced on that part's
     `error` and drops top-level `applied` to false, rather than a 500.

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


def fake_tenant(voice_id="esmi-default", speed=1.0, greeting="", assistant_ids=(AID,)):
    """load_tenant() stand-in — the endpoint only reads .voice_id, .speed,
    and .greeting (assistant ids come from assistant_ids_for(), monkeypatched
    separately in each test), so a full TenantConfig (many required fields,
    no defaults) would be pure noise here. greeting defaults empty so tests
    that don't care about it don't pick up an unexpected extra GET/PATCH."""
    return types.SimpleNamespace(
        voice_id=voice_id, speed=speed, greeting=greeting, vapi_assistant_ids=tuple(assistant_ids)
    )


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


def test_no_voice_saved_is_409_even_with_a_greeting_saved(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(voice_id="", greeting="Hi!"))
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
    assert body["voice"]["after"] == {
        "provider": "11labs",
        "voiceId": "el_real_voice_id",
        "speed": 1.1,
        "stability": 0.5,
    }
    assert body["assistants"][0]["voice"]["changed"] is True
    assert body["assistants"][0]["voice"]["verified"] is None
    assert body["greeting"] is None  # no greeting saved -> untouched
    assert body["assistants"][0]["greeting"] is None


def test_dry_run_computes_greeting_payload_too(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(greeting="New greeting!"))
    current_assistant = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.0}, "firstMessage": "Old"}
    fake_api = FakeApi([current_assistant, current_assistant])  # voice GET, greeting GET
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply?dry_run=true", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert [c[:2] for c in fake_api.calls] == [
        ("GET", f"/assistant/{AID}"),
        ("GET", f"/assistant/{AID}"),
    ]  # two independent plans, no PATCH
    assert body["greeting"] == {
        "before": "Old", "after": "New greeting!", "changed": True, "applied": False,
        "verified": None, "error": None,
    }


# ── success path: voice ──────────────────────────────────────────────────


def test_apply_patches_voice_and_returns_before_after(client, monkeypatch):
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
    assert fake_api.calls[1][2] == {
        "voice": {"provider": "11labs", "voiceId": "el_real_voice_id", "speed": 1.1, "stability": 0.5}
    }
    assert body["applied"] is True
    assert body["dry_run"] is False
    assert body["tenant_id"] == TID
    assert body["assistant_id"] == AID
    assert body["voice"]["before"] == {
        "provider": "11labs", "voiceId": "old_voice_id", "speed": 1.0, "stability": 0.5
    }
    assert body["voice"]["after"] == {
        "provider": "11labs", "voiceId": "el_real_voice_id", "speed": 1.1, "stability": 0.5
    }
    assert body["greeting"] is None
    assert "New callers will hear this voice" in body["message"]
    assert "greeting" not in body["message"].lower()
    assert body["assistants"][0]["voice"]["verified"] is True


def test_already_in_sync_makes_no_patch_but_still_reports_applied(client, monkeypatch):
    """`applied` means "completed without errors", not "a byte changed" —
    already-in-sync is a successful outcome (nothing failed), just with no
    PATCH call. `assistants[].voice.changed` / `message` carry the "nothing
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
    assert body["assistants"][0]["voice"]["changed"] is False
    assert body["assistants"][0]["voice"]["applied"] is False  # no PATCH was needed


def test_vapi_failure_on_voice_is_reported_not_500(client, monkeypatch):
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
    assert body["assistants"][0]["voice"]["error"] is not None
    assert "boom" in body["assistants"][0]["voice"]["error"]


# ── success path: greeting ───────────────────────────────────────────────


def test_empty_greeting_is_never_planned_or_patched(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.0, greeting=""))
    current_assistant = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.0}, "firstMessage": "Existing"}
    fake_api = FakeApi([current_assistant])  # only ONE call — voice GET, no greeting GET at all
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert fake_api.calls == [("GET", f"/assistant/{AID}", None)]
    assert body["greeting"] is None
    assert body["assistants"][0]["greeting"] is None


def test_apply_patches_greeting_with_own_payload_alongside_voice(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.1, greeting="New greeting!"))
    voice_get = {"voice": {"voiceId": "old_voice_id", "speed": 1.0}}
    voice_verify = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.1}}
    greeting_get = {"firstMessage": "Old greeting"}
    greeting_verify = {"firstMessage": "New greeting!"}
    fake_api = FakeApi([voice_get, {}, voice_verify, greeting_get, {}, greeting_verify])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    methods = [c[:2] for c in fake_api.calls]
    assert methods == [
        ("GET", f"/assistant/{AID}"), ("PATCH", f"/assistant/{AID}"), ("GET", f"/assistant/{AID}"),
        ("GET", f"/assistant/{AID}"), ("PATCH", f"/assistant/{AID}"), ("GET", f"/assistant/{AID}"),
    ]
    # voice PATCH body vs greeting PATCH body — never merged into one call
    assert fake_api.calls[1][2] == {"voice": {"voiceId": "el_real_voice_id", "speed": 1.1}}
    assert fake_api.calls[4][2] == {"firstMessage": "New greeting!"}
    assert body["applied"] is True
    assert body["greeting"] == {
        "before": "Old greeting", "after": "New greeting!", "changed": True,
        "applied": True, "verified": True, "error": None,
    }
    assert "voice and greeting" in body["message"].lower()


def test_greeting_only_changed_message_does_not_mention_voice(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.0, greeting="New!"))
    voice_get = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.0}}  # already in sync
    greeting_get = {"firstMessage": "Old"}
    greeting_verify = {"firstMessage": "New!"}
    fake_api = FakeApi([voice_get, greeting_get, {}, greeting_verify])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert "greeting" in body["message"].lower()
    assert "voice" not in body["message"].lower()


def test_vapi_failure_on_greeting_drops_applied_even_if_voice_succeeds(client, monkeypatch):
    monkeypatch.setattr(vs, "assistant_ids_for", lambda tid: [AID])
    monkeypatch.setattr(vs, "load_tenant", lambda tid: fake_tenant(speed=1.0, greeting="New!"))
    voice_get = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.0}}  # already in sync, no PATCH
    greeting_get = {"firstMessage": "Old"}

    # Call-count based fake since both GETs hit the identical path — order
    # is voice's plan GET first, then greeting's plan GET, then greeting's
    # PATCH (which fails).
    calls = {"n": 0}
    responses = [voice_get, greeting_get]

    def fake(method, path, api_key, body=None):
        if method == "GET":
            r = responses[calls["n"]]
            calls["n"] += 1
            return r
        raise vvs.VapiSyncError(f"{method} {path} -> HTTP 500: boom")

    monkeypatch.setattr(vvs, "vapi_api", fake)

    r = client.post("/platform/voice/apply", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is False
    assert body["assistants"][0]["voice"]["error"] is None  # voice was already in sync — never PATCHed
    assert body["assistants"][0]["greeting"]["error"] is not None
    assert "boom" in body["assistants"][0]["greeting"]["error"]
