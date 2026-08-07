"""vapi_voice_sync.py — shared VAPI voice + greeting PATCH mechanics used by
both scripts/sync_vapi_voice.py (voice only) and platform_api/voice_sync.py
(voice + greeting, the dashboard's "Apply to live Esmi" button).

Never hits the real VAPI network: vapi_voice_sync.vapi_api is the one seam
both plan_*/apply_* pairs (GET / PATCH + verify GET) go through,
monkeypatched here with a fake recorder.

What matters here (the logic BOTH callers depend on being correct exactly
once, not reimplemented twice):
  1. plan_assistant_voice reports changed=False when voiceId+speed already
     match — no PATCH should ever be built for that case.
  2. The computed `after` voice payload preserves every key it didn't
     intend to touch (provider, stability, similarityBoost, ...).
  3. apply_assistant_voice/apply_assistant_greeting no-op (applied=False,
     verified=True) when the plan wasn't changed — PATCH is never called
     for an already-in-sync assistant.
  4. Both apply_* functions PATCH then re-GET to verify; a mismatch after
     PATCH is reported as verified=False, not silently treated as success.
  5. A VAPI HTTP failure during either apply is caught and reported on the
     result (error set, applied=False) rather than raised past the caller.
  6. assistant_ids_for("default") returns the hardcoded Orchelix id without
     touching load_tenant (default has no tenants/default vapi config).
  7. plan_assistant_greeting's PATCH payload is a plain
     {"firstMessage": target} — no merge, unlike voice — and its `changed`
     flag is a simple string comparison against the current firstMessage.

Run: PYTHONUTF8=1 pytest evals/test_vapi_voice_sync.py -v
"""

import types

import vapi_voice_sync as vvs

AID = "32994d60-3712-4183-a7db-edc3badeabec"


class FakeApi:
    """Records every call; api_key is captured but not asserted on (both
    real callers resolve it differently — this module only cares that
    whatever key it's given gets threaded through)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, api_key, body=None):
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError(f"unexpected extra vapi_api call: {method} {path}")
        return self.responses.pop(0)


# ── plan_assistant_voice ──────────────────────────────────────────────────


def test_plan_reports_unchanged_when_already_matching(monkeypatch):
    fake = FakeApi([{"name": "Otro Nivel Esmi", "voice": {"voiceId": "v1", "speed": 1.0}}])
    monkeypatch.setattr(vvs, "vapi_api", fake)

    plan = vvs.plan_assistant_voice(AID, "v1", 1.0, "key")

    assert plan.changed is False
    assert plan.before == {"voiceId": "v1", "speed": 1.0}
    assert plan.after == plan.before
    assert fake.calls == [("GET", f"/assistant/{AID}", None)]


def test_plan_preserves_unrelated_voice_keys(monkeypatch):
    fake = FakeApi(
        [
            {
                "name": "Otro Nivel Esmi",
                "voice": {
                    "provider": "11labs",
                    "voiceId": "old",
                    "speed": 1.0,
                    "stability": 0.5,
                    "similarityBoost": 0.8,
                },
            }
        ]
    )
    monkeypatch.setattr(vvs, "vapi_api", fake)

    plan = vvs.plan_assistant_voice(AID, "new_voice_id", 1.1, "key")

    assert plan.changed is True
    assert plan.after == {
        "provider": "11labs",
        "voiceId": "new_voice_id",
        "speed": 1.1,
        "stability": 0.5,
        "similarityBoost": 0.8,
    }


def test_plan_falls_back_to_assistant_id_when_name_missing(monkeypatch):
    monkeypatch.setattr(vvs, "vapi_api", FakeApi([{"voice": {}}]))
    plan = vvs.plan_assistant_voice(AID, "v1", 1.0, "key")
    assert plan.name == AID


# ── apply_assistant_voice ─────────────────────────────────────────────────


def test_apply_is_a_noop_when_plan_unchanged(monkeypatch):
    fake = FakeApi([])  # no calls expected at all
    monkeypatch.setattr(vvs, "vapi_api", fake)
    plan = vvs.AssistantVoicePlan(
        assistant_id=AID, name="X", before={"voiceId": "v1", "speed": 1.0}, after={"voiceId": "v1", "speed": 1.0}, changed=False
    )

    result = vvs.apply_assistant_voice(plan, "key")

    assert result.applied is False
    assert result.verified is True
    assert result.error is None
    assert fake.calls == []


def test_apply_patches_then_verifies(monkeypatch):
    fake = FakeApi(
        [
            {},  # PATCH response (unused)
            {"voice": {"voiceId": "new", "speed": 1.1}},  # verify GET
        ]
    )
    monkeypatch.setattr(vvs, "vapi_api", fake)
    plan = vvs.AssistantVoicePlan(
        assistant_id=AID,
        name="Otro Nivel Esmi",
        before={"voiceId": "old", "speed": 1.0},
        after={"voiceId": "new", "speed": 1.1},
        changed=True,
    )

    result = vvs.apply_assistant_voice(plan, "key")

    assert result.applied is True
    assert result.verified is True
    assert result.error is None
    assert [c[:2] for c in fake.calls] == [
        ("PATCH", f"/assistant/{AID}"),
        ("GET", f"/assistant/{AID}"),
    ]
    assert fake.calls[0][2] == {"voice": {"voiceId": "new", "speed": 1.1}}


def test_apply_reports_verification_mismatch(monkeypatch):
    fake = FakeApi(
        [
            {},
            {"voice": {"voiceId": "old", "speed": 1.0}},  # PATCH silently no-op'd
        ]
    )
    monkeypatch.setattr(vvs, "vapi_api", fake)
    plan = vvs.AssistantVoicePlan(
        assistant_id=AID, name="X", before={"voiceId": "old", "speed": 1.0}, after={"voiceId": "new", "speed": 1.1}, changed=True
    )

    result = vvs.apply_assistant_voice(plan, "key")

    assert result.applied is True
    assert result.verified is False
    assert result.error is None


def test_apply_catches_vapi_errors_and_reports_them(monkeypatch):
    def boom(method, path, api_key, body=None):
        raise vvs.VapiSyncError(f"{method} {path} -> HTTP 500: boom")

    monkeypatch.setattr(vvs, "vapi_api", boom)
    plan = vvs.AssistantVoicePlan(
        assistant_id=AID, name="X", before={"voiceId": "old", "speed": 1.0}, after={"voiceId": "new", "speed": 1.1}, changed=True
    )

    result = vvs.apply_assistant_voice(plan, "key")

    assert result.applied is False
    assert result.verified is False
    assert "HTTP 500" in (result.error or "")


# ── plan_assistant_greeting / apply_assistant_greeting ──────────────────────


def test_greeting_plan_reports_unchanged_when_already_matching(monkeypatch):
    fake = FakeApi([{"name": "Otro Nivel Esmi", "firstMessage": "Hi there!"}])
    monkeypatch.setattr(vvs, "vapi_api", fake)

    plan = vvs.plan_assistant_greeting(AID, "Hi there!", "key")

    assert plan.changed is False
    assert plan.before == "Hi there!"
    assert plan.after == "Hi there!"
    assert fake.calls == [("GET", f"/assistant/{AID}", None)]


def test_greeting_plan_reports_changed_and_payload_is_plain_string(monkeypatch):
    fake = FakeApi([{"name": "Otro Nivel Esmi", "firstMessage": "Old greeting"}])
    monkeypatch.setattr(vvs, "vapi_api", fake)

    plan = vvs.plan_assistant_greeting(AID, "New greeting text", "key")

    assert plan.changed is True
    assert plan.before == "Old greeting"
    assert plan.after == "New greeting text"


def test_greeting_plan_treats_missing_first_message_as_empty_string(monkeypatch):
    monkeypatch.setattr(vvs, "vapi_api", FakeApi([{"name": "X"}]))  # no firstMessage key at all
    plan = vvs.plan_assistant_greeting(AID, "New greeting", "key")
    assert plan.before == ""
    assert plan.changed is True


def test_greeting_apply_is_a_noop_when_plan_unchanged(monkeypatch):
    fake = FakeApi([])
    monkeypatch.setattr(vvs, "vapi_api", fake)
    plan = vvs.AssistantGreetingPlan(assistant_id=AID, name="X", before="Hi!", after="Hi!", changed=False)

    result = vvs.apply_assistant_greeting(plan, "key")

    assert result.applied is False
    assert result.verified is True
    assert result.error is None
    assert fake.calls == []


def test_greeting_apply_patches_with_plain_string_payload_then_verifies(monkeypatch):
    fake = FakeApi(
        [
            {},  # PATCH response (unused)
            {"firstMessage": "New greeting"},  # verify GET
        ]
    )
    monkeypatch.setattr(vvs, "vapi_api", fake)
    plan = vvs.AssistantGreetingPlan(
        assistant_id=AID, name="Otro Nivel Esmi", before="Old greeting", after="New greeting", changed=True
    )

    result = vvs.apply_assistant_greeting(plan, "key")

    assert result.applied is True
    assert result.verified is True
    assert result.error is None
    assert [c[:2] for c in fake.calls] == [
        ("PATCH", f"/assistant/{AID}"),
        ("GET", f"/assistant/{AID}"),
    ]
    assert fake.calls[0][2] == {"firstMessage": "New greeting"}  # no merge, unlike voice


def test_greeting_apply_reports_verification_mismatch(monkeypatch):
    fake = FakeApi(
        [
            {},
            {"firstMessage": "Old greeting"},  # PATCH silently no-op'd
        ]
    )
    monkeypatch.setattr(vvs, "vapi_api", fake)
    plan = vvs.AssistantGreetingPlan(
        assistant_id=AID, name="X", before="Old greeting", after="New greeting", changed=True
    )

    result = vvs.apply_assistant_greeting(plan, "key")

    assert result.applied is True
    assert result.verified is False
    assert result.error is None


def test_greeting_apply_catches_vapi_errors_and_reports_them(monkeypatch):
    def boom(method, path, api_key, body=None):
        raise vvs.VapiSyncError(f"{method} {path} -> HTTP 500: boom")

    monkeypatch.setattr(vvs, "vapi_api", boom)
    plan = vvs.AssistantGreetingPlan(
        assistant_id=AID, name="X", before="Old", after="New", changed=True
    )

    result = vvs.apply_assistant_greeting(plan, "key")

    assert result.applied is False
    assert result.verified is False
    assert "HTTP 500" in (result.error or "")


# ── assistant_ids_for ──────────────────────────────────────────────────────


def test_assistant_ids_for_default_is_hardcoded_no_load_tenant_call(monkeypatch):
    monkeypatch.setattr(vvs, "load_tenant", lambda tid: (_ for _ in ()).throw(
        AssertionError("load_tenant must not be called for the default tenant")
    ))
    assert vvs.assistant_ids_for("default") == [vvs.ORCHELIX_ASSISTANT_ID]


def test_assistant_ids_for_non_default_reads_load_tenant(monkeypatch):
    monkeypatch.setattr(
        vvs, "load_tenant", lambda tid: types.SimpleNamespace(vapi_assistant_ids=(AID,))
    )
    assert vvs.assistant_ids_for("otro-nivel") == [AID]


def test_allowlist_is_exactly_otro_nivel_and_coastline():
    assert vvs.SYNC_ALLOWED_TENANTS == frozenset({"otro-nivel", "coastline-condos"})
