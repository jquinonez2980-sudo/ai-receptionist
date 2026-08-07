# vapi_voice_sync.py — shared VAPI voice + greeting PATCH mechanics.
#
# Single source of truth for two independent callers that must never drift
# apart, same pattern voice_library.py already established for the id ->
# ElevenLabs voiceId mapping:
#   - scripts/sync_vapi_voice.py     (CLI, operator-run via `railway run` —
#                                      voice only, does not push greeting)
#   - platform_api/voice_sync.py     (POST /platform/voice/apply, the
#                                      dashboard's "Apply to live Esmi"
#                                      button — pushes voice AND, when the
#                                      tenant has one saved, greeting)
#
# Both need the exact same allow-list, the exact same assistant-id
# resolution, and the exact same "GET, compute the PATCH payload, PATCH,
# verify" logic — a hand-copied second version of any of these is how the
# two entry points end up disagreeing about what is safe to push to a live
# VAPI assistant. Two independent plan/apply pairs below (voice, greeting)
# since they patch different, unrelated fields on the same assistant object
# and a tenant can have one without the other (e.g. voice_id set, greeting
# still empty).
#
# api_key is a parameter everywhere here, not read from an env var in this
# module: the script resolves VAPI_API_KEY from the Railway/local env
# (scripts/sync_vapi_voice.py's own _require_env), the platform endpoint
# resolves it via tools._get_vapi_key() (env or base64 variant) — two
# different resolution strategies, one shared wire protocol.

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from tenants import load_tenant

VAPI_BASE = "https://api.vapi.ai"

# Default tenant (Orchelix) intentionally has no vapi ids in its own
# config.json — its assistant id is a fact about the live VAPI account, not
# something any tenant config carries.
ORCHELIX_ASSISTANT_ID = "d5e020bf-0235-4214-a57f-de30e8072b0b"

# Hard allow-list for anything that can PATCH a live assistant's voice —
# both the CLI's --tenant and the dashboard's Apply button. Expand only
# after a tenant's sync has been dry-run reviewed and apply-tested here
# first; default/Orchelix (the live production number) stays out until it
# has more track record on this path (see scripts/sync_vapi_voice.py's
# module docstring for the full rationale).
SYNC_ALLOWED_TENANTS = frozenset({"otro-nivel", "coastline-condos"})


class VapiSyncError(RuntimeError):
    """A VAPI API call failed (network/HTTP error). Never raised for a
    business-rule refusal (not allow-listed, no voice saved, ...) — callers
    handle those as their own explicit checks before calling into VAPI."""


def assistant_ids_for(tenant_id: str) -> list[str]:
    if tenant_id == "default":
        return [ORCHELIX_ASSISTANT_ID]
    return list(load_tenant(tenant_id).vapi_assistant_ids)


def vapi_api(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    # Cloudflare 403s Python's default User-Agent — send a normal one (same
    # workaround as scripts/update_vapi_webhooks.py).
    req = urllib.request.Request(
        f"{VAPI_BASE}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "curl/8.9.1",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise VapiSyncError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


@dataclass
class AssistantVoicePlan:
    """The result of reading one assistant's current voice and computing
    (never sending) the PATCH payload that would bring it to the target
    voice/speed. `after` preserves every key on `before` other than voiceId
    and speed — provider, stability, similarityBoost, style, ... — matching
    the "never guess, only set what we intend" rule scripts/
    update_vapi_webhooks.py established for server.url."""

    assistant_id: str
    name: str
    before: dict
    after: dict
    changed: bool  # False => already in sync, PATCH would be a no-op


def plan_assistant_voice(
    assistant_id: str, target_voice_id: str, target_speed: float, api_key: str
) -> AssistantVoicePlan:
    a = vapi_api("GET", f"/assistant/{assistant_id}", api_key)
    name = a.get("name") or assistant_id
    current_voice = dict(a.get("voice") or {})
    changed = not (
        current_voice.get("voiceId") == target_voice_id
        and current_voice.get("speed") == target_speed
    )
    after = (
        {**current_voice, "voiceId": target_voice_id, "speed": target_speed}
        if changed
        else current_voice
    )
    return AssistantVoicePlan(
        assistant_id=assistant_id, name=name, before=current_voice, after=after, changed=changed
    )


@dataclass
class AssistantVoiceResult:
    assistant_id: str
    name: str
    before: dict
    after: dict
    applied: bool  # True only if a PATCH was actually sent
    verified: bool  # True if the post-PATCH GET confirms voiceId+speed match
    error: str | None = None


def apply_assistant_voice(plan: AssistantVoicePlan, api_key: str) -> AssistantVoiceResult:
    """No-ops (applied=False, verified=True) when the plan already matched —
    same "already in sync — nothing to do" short-circuit the CLI has always
    had, now shared so the dashboard button gets it too."""
    if not plan.changed:
        return AssistantVoiceResult(
            plan.assistant_id, plan.name, plan.before, plan.before, applied=False, verified=True
        )
    try:
        vapi_api("PATCH", f"/assistant/{plan.assistant_id}", api_key, {"voice": plan.after})
        check = vapi_api("GET", f"/assistant/{plan.assistant_id}", api_key)
        got = check.get("voice") or {}
        verified = got.get("voiceId") == plan.after.get("voiceId") and got.get(
            "speed"
        ) == plan.after.get("speed")
        return AssistantVoiceResult(
            plan.assistant_id, plan.name, plan.before, got, applied=True, verified=verified
        )
    except VapiSyncError as e:
        return AssistantVoiceResult(
            plan.assistant_id,
            plan.name,
            plan.before,
            plan.before,
            applied=False,
            verified=False,
            error=str(e),
        )


# ── greeting (assistant.firstMessage) ────────────────────────────────────
#
# Unlike voice, `firstMessage` is a plain top-level string on the assistant
# object (confirmed via a live GET — sibling to `voice`, not nested), so
# there is no "preserve other keys" merge to do: the PATCH payload is just
# {"firstMessage": target_greeting}. Callers (platform_api/voice_sync.py)
# only build a plan when TenantConfig.greeting is non-empty — an empty
# greeting means "leave firstMessage exactly as it is," which these
# functions can't distinguish from "shouldn't touch it" on their own, so
# that decision stays the caller's, same as the voice_id-empty check in
# sync_vapi_voice.py's sync_tenant().


@dataclass
class AssistantGreetingPlan:
    assistant_id: str
    name: str
    before: str  # current firstMessage, "" if unset
    after: str  # target_greeting, verbatim — spoken by TTS, no wrapper
    changed: bool


def plan_assistant_greeting(
    assistant_id: str, target_greeting: str, api_key: str
) -> AssistantGreetingPlan:
    a = vapi_api("GET", f"/assistant/{assistant_id}", api_key)
    name = a.get("name") or assistant_id
    current = a.get("firstMessage") or ""
    changed = current != target_greeting
    return AssistantGreetingPlan(
        assistant_id=assistant_id, name=name, before=current, after=target_greeting, changed=changed
    )


@dataclass
class AssistantGreetingResult:
    assistant_id: str
    name: str
    before: str
    after: str
    applied: bool
    verified: bool
    error: str | None = None


def apply_assistant_greeting(plan: AssistantGreetingPlan, api_key: str) -> AssistantGreetingResult:
    """Same no-op / PATCH-then-verify shape as apply_assistant_voice."""
    if not plan.changed:
        return AssistantGreetingResult(
            plan.assistant_id, plan.name, plan.before, plan.before, applied=False, verified=True
        )
    try:
        vapi_api(
            "PATCH", f"/assistant/{plan.assistant_id}", api_key, {"firstMessage": plan.after}
        )
        check = vapi_api("GET", f"/assistant/{plan.assistant_id}", api_key)
        got = check.get("firstMessage") or ""
        return AssistantGreetingResult(
            plan.assistant_id, plan.name, plan.before, got, applied=True, verified=got == plan.after
        )
    except VapiSyncError as e:
        return AssistantGreetingResult(
            plan.assistant_id,
            plan.name,
            plan.before,
            plan.before,
            applied=False,
            verified=False,
            error=str(e),
        )
