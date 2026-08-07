"""scripts/sync_vapi_voice.py — VAPI assistant voice sync (Voice Studio,
docs/ESMI_DASHBOARD_UX.md Section 12.1).

Never hits the real VAPI network: `sync_vapi_voice.api()` is the one seam
both GET and PATCH go through, so it's monkeypatched with a fake recorder
that returns canned assistant JSON. `load_tenant` is monkeypatched too, so
tests control voice_id/speed directly instead of depending on the real
tenants/<id>/config.json content drifting out from under this suite.

What matters here:
  1. The hard allow-list (SYNC_ALLOWED_TENANTS) blocks any other tenant —
     including "default" (live Orchelix) — for BOTH dry run and --apply,
     before api() is ever called.
  2. Dry run (no --apply) calls GET only, never PATCH, and prints the exact
     PATCH payload it would send.
  3. --apply does GET -> PATCH -> verify-GET, preserving every voice key it
     didn't intend to change (stability, similarityBoost, ...).
  4. A verification mismatch after PATCH is reported as a failure.
  5. An unmapped voice_id refuses before any api() call.
  6. A tenant already in sync makes no PATCH call even under --apply.

Run: PYTHONUTF8=1 pytest evals/test_sync_vapi_voice.py -v
"""

import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("VAPI_API_KEY", "test-vapi-key")
os.environ.setdefault("TENANT_CONFIG_FROM_DB", "0")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sync_vapi_voice as svv  # noqa: E402

OTRO_NIVEL_AID = "32994d60-3712-4183-a7db-edc3badeabec"
COASTLINE_AID = "a351deb6-bf22-4cda-a3f3-67bca8ac6346"


def fake_tenant(voice_id="sofia", speed=1.0, assistant_ids=(OTRO_NIVEL_AID,)):
    return types.SimpleNamespace(voice_id=voice_id, speed=speed, vapi_assistant_ids=tuple(assistant_ids))


class FakeApi:
    """Records every call and answers from a scripted queue of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError(f"unexpected extra api() call: {method} {path}")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _voice_catalog(monkeypatch):
    monkeypatch.setattr(svv, "VOICE_LIBRARY", {"sofia": "el_real_voice_id"})


# ── hard allow-list ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("tenant_id", ["default", "acme", "some-future-tenant"])
@pytest.mark.parametrize("apply", [False, True])
def test_non_allowlisted_tenant_is_refused_before_any_api_call(monkeypatch, tenant_id, apply):
    fake_api = FakeApi([])
    monkeypatch.setattr(svv, "api", fake_api)
    monkeypatch.setattr(svv, "load_tenant", lambda tid: (_ for _ in ()).throw(
        AssertionError("load_tenant must not be called for a non-allow-listed tenant")
    ))

    rc = svv.sync_tenant(tenant_id, apply)

    assert rc == 1
    assert fake_api.calls == []


def test_allowlisted_tenants_are_exactly_otro_nivel_and_coastline():
    assert svv.SYNC_ALLOWED_TENANTS == frozenset({"otro-nivel", "coastline-condos"})


# ── dry run: GET only, prints exact payload ─────────────────────────────────


def test_dry_run_never_calls_patch(monkeypatch, capsys):
    monkeypatch.setattr(svv, "load_tenant", lambda tid: fake_tenant(voice_id="sofia", speed=1.1))
    current_assistant = {
        "name": "Otro Nivel Esmi",
        "voice": {"provider": "11labs", "voiceId": "old_voice_id", "speed": 1.0, "stability": 0.5},
    }
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=False)

    assert rc == 0
    assert fake_api.calls == [("GET", f"/assistant/{OTRO_NIVEL_AID}", None)]

    out = capsys.readouterr().out
    assert "PATCH payload (voice):" in out
    assert '"voiceId": "el_real_voice_id"' in out
    assert '"speed": 1.1' in out
    assert '"stability": 0.5' in out  # preserved, unrelated key
    assert "DRY RUN" in out


# ── --apply: GET -> PATCH -> verify-GET, preserves unrelated keys ──────────


def test_apply_patches_and_verifies_preserving_other_voice_keys(monkeypatch, capsys):
    monkeypatch.setattr(svv, "load_tenant", lambda tid: fake_tenant(voice_id="sofia", speed=1.1))
    current_assistant = {
        "name": "Otro Nivel Esmi",
        "voice": {
            "provider": "11labs",
            "voiceId": "old_voice_id",
            "speed": 1.0,
            "stability": 0.5,
            "similarityBoost": 0.8,
        },
    }
    verify_assistant = {
        "voice": {
            "provider": "11labs",
            "voiceId": "el_real_voice_id",
            "speed": 1.1,
            "stability": 0.5,
            "similarityBoost": 0.8,
        }
    }
    fake_api = FakeApi([current_assistant, {}, verify_assistant])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 0
    assert [c[:2] for c in fake_api.calls] == [
        ("GET", f"/assistant/{OTRO_NIVEL_AID}"),
        ("PATCH", f"/assistant/{OTRO_NIVEL_AID}"),
        ("GET", f"/assistant/{OTRO_NIVEL_AID}"),
    ]

    patch_body = fake_api.calls[1][2]
    assert patch_body == {
        "voice": {
            "provider": "11labs",
            "voiceId": "el_real_voice_id",
            "speed": 1.1,
            "stability": 0.5,
            "similarityBoost": 0.8,
        }
    }

    out = capsys.readouterr().out
    assert "patched + verified" in out
    assert "DRY RUN" not in out


def test_verification_mismatch_is_reported_as_failure(monkeypatch, capsys):
    monkeypatch.setattr(svv, "load_tenant", lambda tid: fake_tenant(voice_id="sofia", speed=1.1))
    current_assistant = {"name": "Otro Nivel Esmi", "voice": {"voiceId": "old_voice_id", "speed": 1.0}}
    verify_assistant = {"voice": {"voiceId": "old_voice_id", "speed": 1.0}}  # PATCH silently no-op'd
    fake_api = FakeApi([current_assistant, {}, verify_assistant])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 1
    assert "verification mismatch" in capsys.readouterr().out


def test_coastline_condos_is_allowlisted_and_uses_its_own_assistant_id(monkeypatch):
    monkeypatch.setattr(
        svv, "load_tenant", lambda tid: fake_tenant(voice_id="sofia", speed=1.0, assistant_ids=(COASTLINE_AID,))
    )
    current_assistant = {"voice": {"voiceId": "el_real_voice_id", "speed": 1.0}}
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("coastline-condos", apply=False)

    assert rc == 0
    assert fake_api.calls == [("GET", f"/assistant/{COASTLINE_AID}", None)]


# ── unmapped voice refuses to guess, no api() calls ─────────────────────────


def test_unmapped_voice_id_refuses_before_any_api_call(monkeypatch):
    monkeypatch.setattr(svv, "load_tenant", lambda tid: fake_tenant(voice_id="not-a-real-voice"))
    fake_api = FakeApi([])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 1
    assert fake_api.calls == []


def test_empty_voice_id_is_a_noop_before_any_api_call(monkeypatch):
    monkeypatch.setattr(svv, "load_tenant", lambda tid: fake_tenant(voice_id=""))
    fake_api = FakeApi([])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 0
    assert fake_api.calls == []


# ── already in sync: no PATCH even under --apply ────────────────────────────


def test_already_in_sync_makes_no_patch_call_even_with_apply(monkeypatch, capsys):
    monkeypatch.setattr(svv, "load_tenant", lambda tid: fake_tenant(voice_id="sofia", speed=1.0))
    current_assistant = {"name": "Otro Nivel Esmi", "voice": {"voiceId": "el_real_voice_id", "speed": 1.0}}
    fake_api = FakeApi([current_assistant])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=True)

    assert rc == 0
    assert fake_api.calls == [("GET", f"/assistant/{OTRO_NIVEL_AID}", None)]
    assert "already in sync" in capsys.readouterr().out


# ── no assistant ids configured ──────────────────────────────────────────────


def test_no_assistant_ids_is_reported_and_refuses(monkeypatch):
    monkeypatch.setattr(svv, "load_tenant", lambda tid: fake_tenant(voice_id="sofia", assistant_ids=()))
    fake_api = FakeApi([])
    monkeypatch.setattr(svv, "api", fake_api)

    rc = svv.sync_tenant("otro-nivel", apply=False)

    assert rc == 1
    assert fake_api.calls == []
