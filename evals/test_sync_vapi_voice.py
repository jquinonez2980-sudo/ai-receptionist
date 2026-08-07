"""scripts/sync_vapi_voice.py — VAPI assistant voice sync CLI (Voice Studio,
docs/ESMI_DASHBOARD_UX.md Section 12.1).

The PATCH-preserving-other-keys / plan-then-apply logic itself now lives in
vapi_voice_sync.py (shared with platform_api/voice_sync.py's "Apply to live
Esmi" endpoint) and has its own dedicated tests in
evals/test_vapi_voice_sync.py. This file tests the CLI's OWN wiring on top
of that shared module:
  1. The hard allow-list (vapi_voice_sync.SYNC_ALLOWED_TENANTS) blocks any
     other tenant — including "default" (live Orchelix) — for BOTH dry run
     and --apply, before vapi_api or live_voice_config is ever called.
  2. Dry run (no --apply) never calls apply_assistant_voice (no PATCH),
     and prints the exact PATCH payload it would send.
  3. --apply calls apply_assistant_voice and reports its result (patched +
     verified / verification mismatch / FAILED) correctly.
  4. An unmapped voice_id refuses before any VAPI call.
  5. A tenant already in sync makes no PATCH call even under --apply.
  6. No configured assistant ids refuses cleanly.
  7. live_voice_config() itself parses GET /platform/config correctly and
     surfaces HTTP failures loudly — this is the CLI's own network seam
     (resolving a DB-config tenant's real voice from a machine that can't
     reach Postgres directly), not shared with the endpoint.

Never hits the real VAPI or platform network: vapi_voice_sync.vapi_api
(the seam plan_assistant_voice/apply_assistant_voice call internally) and
vapi_voice_sync.load_tenant (the seam assistant_ids_for calls internally)
are monkeypatched directly on the vapi_voice_sync module — NOT on
sync_vapi_voice's own copies of those names, since `from x import y` binds
a separate reference that plan_assistant_voice/apply_assistant_voice/
assistant_ids_for (defined in vapi_voice_sync.py) never consult.

Run: PYTHONUTF8=1 pytest evals/test_sync_vapi_voice.py -v
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("VAPI_API_KEY", "test-vapi-key")
os.environ.setdefault("PLATFORM_API_SECRET", "test-platform-secret")
os.environ.setdefault("TENANT_CONFIG_FROM_DB", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sync_vapi_voice as svv  # noqa: E402

import vapi_voice_sync as vvs  # noqa: E402

OTRO_NIVEL_AID = "32994d60-3712-4183-a7db-edc3badeabec"
COASTLINE_AID = "a351deb6-bf22-4cda-a3f3-67bca8ac6346"


def fake_tenant(assistant_ids=(OTRO_NIVEL_AID,)):
    """vapi_voice_sync.load_tenant() stand-in — only vapi_assistant_ids
    matters to assistant_ids_for(); voice_id/speed come from
    live_voice_config(), not this."""
    return types.SimpleNamespace(vapi_assistant_ids=tuple(assistant_ids))


def fake_live(voice_id="sofia", speed=1.0):
    return lambda tenant_id: {"voice_id": voice_id, "speed": speed}


class FakeApi:
    """Records every vapi_voice_sync.vapi_api call and answers from a
    scripted queue of responses."""

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
    monkeypatch.setattr(svv, "VOICE_LIBRARY", {"sofia": "el_real_voice_id"})


# ── hard allow-list ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("tenant_id", ["default", "acme", "some-future-tenant"])
@pytest.mark.parametrize("apply", [False, True])
def test_non_allowlisted_tenant_is_refused_before_any_api_call(monkeypatch, tenant_id, apply):
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: (_ for _ in ()).throw(
        AssertionError("load_tenant must not be called for a non-allow-listed tenant")
    ))
    monkeypatch.setattr(svv, "live_voice_config", lambda tid: (_ for _ in ()).throw(
        AssertionError("live_voice_config must not be called for a non-allow-listed tenant")
    ))

    rc = svv.sync_tenant(tenant_id, apply)

    assert rc == 1
    assert fake_api.calls == []


def test_allowlisted_tenants_are_exactly_otro_nivel_and_coastline():
    assert svv.SYNC_ALLOWED_TENANTS == frozenset({"otro-nivel", "coastline-condos"})


# ── dry run: GET only, prints exact payload ─────────────────────────────────


def test_dry_run_never_calls_patch(monkeypatch, capsys):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant())
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id="sofia", speed=1.1))
    current_assistant = {
        "name": "Otro Nivel Esmi",
        "voice": {"provider": "11labs", "voiceId": "old_voice_id", "speed": 1.0, "stability": 0.5},
    }
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=False)

    assert rc == 0
    assert fake_api.calls == [("GET", f"/assistant/{OTRO_NIVEL_AID}", None)]

    out = capsys.readouterr().out
    assert "PATCH payload (voice):" in out
    assert '"voiceId": "el_real_voice_id"' in out
    assert '"speed": 1.1' in out
    assert '"stability": 0.5' in out  # preserved, unrelated key
    assert "DRY RUN" in out


# ── --apply: reports the shared module's plan/apply result ─────────────────


def test_apply_patches_and_reports_verified(monkeypatch, capsys):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant())
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id="sofia", speed=1.1))
    current_assistant = {
        "name": "Otro Nivel Esmi",
        "voice": {"provider": "11labs", "voiceId": "old_voice_id", "speed": 1.0, "stability": 0.5},
    }
    verify_assistant = {
        "voice": {"provider": "11labs", "voiceId": "el_real_voice_id", "speed": 1.1, "stability": 0.5}
    }
    fake_api = FakeApi([current_assistant, {}, verify_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 0
    assert [c[:2] for c in fake_api.calls] == [
        ("GET", f"/assistant/{OTRO_NIVEL_AID}"),
        ("PATCH", f"/assistant/{OTRO_NIVEL_AID}"),
        ("GET", f"/assistant/{OTRO_NIVEL_AID}"),
    ]
    assert fake_api.calls[1][2] == {
        "voice": {"provider": "11labs", "voiceId": "el_real_voice_id", "speed": 1.1, "stability": 0.5}
    }

    out = capsys.readouterr().out
    assert "patched + verified" in out
    assert "DRY RUN" not in out


def test_verification_mismatch_is_reported_as_failure(monkeypatch, capsys):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant())
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id="sofia", speed=1.1))
    current_assistant = {"name": "Otro Nivel Esmi", "voice": {"voiceId": "old_voice_id", "speed": 1.0}}
    verify_assistant = {"voice": {"voiceId": "old_voice_id", "speed": 1.0}}  # PATCH silently no-op'd
    fake_api = FakeApi([current_assistant, {}, verify_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 1
    assert "verification mismatch" in capsys.readouterr().out


def test_apply_failure_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant())
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id="sofia", speed=1.1))
    current_assistant = {"name": "Otro Nivel Esmi", "voice": {"voiceId": "old_voice_id", "speed": 1.0}}

    def flaky(method, path, api_key, body=None):
        if method == "PATCH":
            raise vvs.VapiSyncError(f"{method} {path} -> HTTP 500: boom")
        return current_assistant

    monkeypatch.setattr(vvs, "vapi_api", flaky)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 1
    assert "FAILED" in capsys.readouterr().out


def test_coastline_condos_is_allowlisted_and_uses_its_own_assistant_id(monkeypatch):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant(assistant_ids=(COASTLINE_AID,)))
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id="sofia", speed=1.0))
    current_assistant = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.0}}
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("coastline-condos", apply=False)

    assert rc == 0
    assert fake_api.calls == [("GET", f"/assistant/{COASTLINE_AID}", None)]


# ── unmapped voice refuses to guess, no VAPI api() calls ────────────────────


def test_unmapped_voice_id_refuses_before_any_api_call(monkeypatch):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant())
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id="not-a-real-voice"))
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 1
    assert fake_api.calls == []


def test_empty_voice_id_is_a_noop_before_any_api_call(monkeypatch):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant())
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id=""))
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 0
    assert fake_api.calls == []


# ── already in sync: no PATCH even under --apply ────────────────────────────


def test_already_in_sync_makes_no_patch_call_even_with_apply(monkeypatch, capsys):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant())
    monkeypatch.setattr(svv, "live_voice_config", fake_live(voice_id="sofia", speed=1.0))
    current_assistant = {"name": "Otro Nivel Esmi", "voice": {"voiceId": "el_real_voice_id", "speed": 1.0}}
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 0
    assert fake_api.calls == [("GET", f"/assistant/{OTRO_NIVEL_AID}", None)]
    assert "already in sync" in capsys.readouterr().out


# ── no assistant ids configured ──────────────────────────────────────────────


def test_no_assistant_ids_is_reported_and_refuses(monkeypatch):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: fake_tenant(assistant_ids=()))
    monkeypatch.setattr(svv, "live_voice_config", lambda tid: (_ for _ in ()).throw(
        AssertionError("live_voice_config must not be called when there are no assistant ids")
    ))
    fake_api = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=False)

    assert rc == 1
    assert fake_api.calls == []


# ── live_voice_config(): the CLI's own network seam ─────────────────────────


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_live_voice_config_parses_config_response(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeHTTPResponse(
            {"tenant_id": "otro-nivel", "version": 6, "config": {"voice_id": "Esmi-Default", "speed": 1.05}}
        )

    monkeypatch.setattr(svv.urllib.request, "urlopen", fake_urlopen)

    out = svv.live_voice_config("otro-nivel")

    assert out == {"voice_id": "esmi-default", "speed": 1.05}  # lower-cased, like PUT does
    assert captured["url"] == f"{svv.ESMI_BASE_URL}/platform/config"
    assert captured["headers"]["x-tenant-id"] == "otro-nivel"
    assert captured["headers"]["x-platform-secret"] == "test-platform-secret"


def test_live_voice_config_defaults_missing_voice_id_to_empty(monkeypatch):
    monkeypatch.setattr(
        svv.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeHTTPResponse({"config": {}}),
    )
    assert svv.live_voice_config("otro-nivel") == {"voice_id": "", "speed": 1.0}


def test_live_voice_config_raises_loudly_on_http_error(monkeypatch):
    import io
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"bad secret")
        )

    monkeypatch.setattr(svv.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="platform/config"):
        svv.live_voice_config("otro-nivel")
