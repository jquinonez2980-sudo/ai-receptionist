# platform_api/voice_sync.py — POST /platform/voice/apply (Voice Studio's
# "Apply to live Esmi" button, docs/ESMI_DASHBOARD_UX.md Section 12.1).
#
# The dashboard's "Save voice settings" (PUT /platform/config) only ever
# writes TenantConfig.voice_id/speed/greeting — it does not touch VAPI, by
# design (see those fields' own comments in tenants.py). Until now the only
# way to actually push a saved voice onto a tenant's live VAPI assistant was
# scripts/sync_vapi_voice.py --apply, run by hand via `railway run`. This
# endpoint is the same action from the dashboard, reusing the exact same
# plan/apply logic (vapi_voice_sync.py) so the two entry points can never
# disagree about what's safe to PATCH — and additionally pushes
# TenantConfig.greeting onto assistant.firstMessage in the same call, since
# a caller hearing the voice change but not the greeting change (or having
# to click two separate buttons for one "make live Esmi match what I saved"
# action) is a worse dashboard experience than the two happening together.
# scripts/sync_vapi_voice.py itself stays voice-only — the CLI's job is
# narrowly "fix the voice," not a general-purpose assistant-config pusher.
#
# Reads voice_id/speed/greeting from tenants.load_tenant(tenant_id) directly
# (no HTTP round-trip the way the script needs — this endpoint IS the live
# app, so the DB-first read is already local) and resolves the VAPI key via
# tools._get_vapi_key() (env or base64 variant), not an env var this module
# reads itself.
#
# Same hard allow-list as the script (vapi_voice_sync.SYNC_ALLOWED_TENANTS)
# — default/Orchelix gets a 403 here exactly like --tenant default does on
# the CLI. This is not weakened or duplicated: it's the same frozenset
# object both entry points import. Applies to greeting too: an empty
# greeting is skipped (see below), but a non-empty one is still gated by
# this same tenant check before anything is read or PATCHed.

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import load_tenant
from tools import _get_vapi_key
from vapi_voice_sync import (
    SYNC_ALLOWED_TENANTS,
    VapiSyncError,
    apply_assistant_greeting,
    apply_assistant_voice,
    assistant_ids_for,
    plan_assistant_greeting,
    plan_assistant_voice,
)
from voice_library import VOICE_LIBRARY

log = logging.getLogger(__name__)

router = APIRouter()


def _voice_entry(aid: str, target_elevenlabs_id: str, target_speed: float, api_key: str, dry_run: bool) -> tuple[dict, str, bool]:
    """Returns (entry, name, errored). name is "" on a GET failure (caller
    has nothing better to show)."""
    try:
        plan = plan_assistant_voice(aid, target_elevenlabs_id, target_speed, api_key)
    except VapiSyncError as e:
        log.warning("Voice sync GET failed: assistant=%s (%s)", aid, e)
        return (
            {"before": None, "after": None, "changed": None, "applied": False, "verified": False, "error": str(e)},
            "",
            True,
        )

    if dry_run:
        entry = {
            "before": plan.before, "after": plan.after, "changed": plan.changed,
            "applied": False, "verified": None, "error": None,  # not attempted — dry run
        }
        return entry, plan.name, False

    result = apply_assistant_voice(plan, api_key)
    errored = bool(result.error) or (plan.changed and not result.verified)
    if result.error:
        log.warning("Voice sync PATCH failed: assistant=%s (%s)", aid, result.error)
    entry = {
        "before": result.before, "after": result.after, "changed": plan.changed,
        "applied": result.applied, "verified": result.verified, "error": result.error,
    }
    return entry, plan.name, errored


def _greeting_entry(aid: str, target_greeting: str, api_key: str, dry_run: bool) -> tuple[dict, bool]:
    try:
        plan = plan_assistant_greeting(aid, target_greeting, api_key)
    except VapiSyncError as e:
        log.warning("Greeting sync GET failed: assistant=%s (%s)", aid, e)
        return {
            "before": None, "after": None, "changed": None, "applied": False, "verified": False, "error": str(e),
        }, True

    if dry_run:
        return {
            "before": plan.before, "after": plan.after, "changed": plan.changed,
            "applied": False, "verified": None, "error": None,
        }, False

    result = apply_assistant_greeting(plan, api_key)
    errored = bool(result.error) or (plan.changed and not result.verified)
    if result.error:
        log.warning("Greeting sync PATCH failed: assistant=%s (%s)", aid, result.error)
    return {
        "before": result.before, "after": result.after, "changed": plan.changed,
        "applied": result.applied, "verified": result.verified, "error": result.error,
    }, errored


@router.post("/platform/voice/apply")
def platform_voice_apply(request: Request, dry_run: bool = False) -> dict:
    """Push the tenant's saved voice_id/speed, and (when set) greeting, onto
    their live VAPI assistant(s). dry_run=true computes and returns the
    exact payloads without calling PATCH (still calls GET for each — there
    is no way to build a correct partial-PATCH voice body, i.e. preserving
    stability/similarityBoost/..., without reading current state first;
    greeting's GET is just for the before/after diff, since firstMessage
    itself needs no merge).

    Sync `def` on purpose (FastAPI threadpool) — every branch here is a
    blocking outbound HTTP call to VAPI, matching platform_api/
    voice_preview.py's own reasoning for its ElevenLabs call.
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    if tenant_id not in SYNC_ALLOWED_TENANTS:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Voice sync isn't enabled for '{tenant_id}' yet — only "
                f"{', '.join(sorted(SYNC_ALLOWED_TENANTS))} are allow-listed. "
                "Ask Orchelix to expand this once more tenants are verified."
            ),
        )

    aids = assistant_ids_for(tenant_id)
    if not aids:
        raise HTTPException(
            status_code=409, detail="No VAPI assistant is configured for this tenant."
        )

    cfg = load_tenant(tenant_id)
    voice_id = (cfg.voice_id or "").strip().lower()
    if not voice_id:
        raise HTTPException(
            status_code=409,
            detail="No voice has been saved yet — choose a voice and Save first.",
        )

    target_elevenlabs_id = VOICE_LIBRARY.get(voice_id)
    if target_elevenlabs_id is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"voice_id '{voice_id}' has no ElevenLabs mapping yet — this voice "
                "can't be pushed to VAPI until voice_library.VOICE_LIBRARY has an "
                "entry for it."
            ),
        )

    api_key = _get_vapi_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="VAPI is not configured (missing VAPI_API_KEY).")

    # Empty greeting -> None for every assistant, always. Skipped entirely,
    # not planned with an empty target — an empty TenantConfig.greeting
    # must never be able to blank out an assistant's existing firstMessage.
    target_greeting = cfg.greeting.strip() if cfg.greeting else ""

    assistants: list[dict] = []
    any_error = False
    any_greeting_planned = False

    for aid in aids:
        voice_entry, name, voice_errored = _voice_entry(aid, target_elevenlabs_id, cfg.speed, api_key, dry_run)
        any_error = any_error or voice_errored

        greeting_entry = None
        if target_greeting:
            any_greeting_planned = True
            greeting_entry, greeting_errored = _greeting_entry(aid, target_greeting, api_key, dry_run)
            any_error = any_error or greeting_errored

        assistants.append(
            {
                "assistant_id": aid,
                "name": name or aid,
                "voice": voice_entry,
                "greeting": greeting_entry,
            }
        )

    applied = (not dry_run) and not any_error
    if applied:
        log.info(
            "Tenant '%s': voice%s pushed to live VAPI assistant(s).",
            tenant_id, " + greeting" if any_greeting_planned else "",
        )

    voice_changed = any(a["voice"].get("changed") for a in assistants)
    greeting_changed = any((a["greeting"] or {}).get("changed") for a in assistants)

    if dry_run:
        message = f"Dry run — showing the exact payload for {len(assistants)} assistant(s). No changes were made."
    elif any_error:
        message = "Couldn't update every assistant — see the error on each one below."
    elif not voice_changed and not greeting_changed:
        message = "Already up to date — nothing to change."
    elif voice_changed and greeting_changed:
        message = "Live Esmi updated. New callers will hear this voice and greeting."
    elif greeting_changed:
        message = "Live Esmi updated. New callers will hear this greeting."
    else:
        message = "Live Esmi updated. New callers will hear this voice."

    first = assistants[0] if assistants else {}
    return {
        "tenant_id": tenant_id,
        # Flattened from the first assistant for the common (today: every
        # allow-listed tenant) single-assistant case — `assistants` below is
        # the source of truth for a tenant with more than one.
        "assistant_id": first.get("assistant_id"),
        "voice": first.get("voice"),
        "greeting": first.get("greeting"),
        "assistants": assistants,
        "applied": applied,
        "dry_run": dry_run,
        "message": message,
    }
