# platform_api/voice_sync.py — POST /platform/voice/apply (Voice Studio's
# "Apply to live Esmi" button, docs/ESMI_DASHBOARD_UX.md Section 12.1).
#
# The dashboard's "Save voice settings" (PUT /platform/config) only ever
# writes TenantConfig.voice_id/speed — it does not touch VAPI, by design
# (see that field's own comment in tenants.py). Until now the only way to
# actually push a saved voice onto a tenant's live VAPI assistant was
# scripts/sync_vapi_voice.py --apply, run by hand via `railway run`. This
# endpoint is the same action from the dashboard, reusing the exact same
# plan/apply logic (vapi_voice_sync.py) so the two entry points can never
# disagree about what's safe to PATCH.
#
# Reads voice_id/speed from tenants.load_tenant(tenant_id) directly (no HTTP
# round-trip the way the script needs — this endpoint IS the live app, so
# the DB-first read is already local) and resolves the VAPI key via
# tools._get_vapi_key() (env or base64 variant), not an env var this module
# reads itself.
#
# Same hard allow-list as the script (vapi_voice_sync.SYNC_ALLOWED_TENANTS)
# — default/Orchelix gets a 403 here exactly like --tenant default does on
# the CLI. This is not weakened or duplicated: it's the same frozenset
# object both entry points import.

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from platform_api.security import require_tenant, verify_platform_secret
from tenants import load_tenant
from tools import _get_vapi_key
from vapi_voice_sync import (
    SYNC_ALLOWED_TENANTS,
    VapiSyncError,
    apply_assistant_voice,
    assistant_ids_for,
    plan_assistant_voice,
)
from voice_library import VOICE_LIBRARY

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/platform/voice/apply")
def platform_voice_apply(request: Request, dry_run: bool = False) -> dict:
    """Push the tenant's saved voice_id/speed onto their live VAPI
    assistant(s). dry_run=true computes and returns the exact payload
    without calling PATCH (still calls GET, same tradeoff the CLI's dry run
    makes — there is no way to build a correct partial-PATCH body, i.e.
    preserving stability/similarityBoost/..., without reading current state
    first).

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

    assistants: list[dict] = []
    any_error = False

    for aid in aids:
        try:
            plan = plan_assistant_voice(aid, target_elevenlabs_id, cfg.speed, api_key)
        except VapiSyncError as e:
            log.warning("Voice sync GET failed: tenant=%s assistant=%s (%s)", tenant_id, aid, e)
            any_error = True
            assistants.append(
                {
                    "assistant_id": aid,
                    "name": aid,
                    "before": None,
                    "after": None,
                    "changed": None,
                    "applied": False,
                    "verified": False,
                    "error": str(e),
                }
            )
            continue

        if dry_run:
            assistants.append(
                {
                    "assistant_id": plan.assistant_id,
                    "name": plan.name,
                    "before": plan.before,
                    "after": plan.after,
                    "changed": plan.changed,
                    "applied": False,
                    "verified": None,  # not attempted — dry run
                    "error": None,
                }
            )
            continue

        result = apply_assistant_voice(plan, api_key)
        if result.error or (plan.changed and not result.verified):
            any_error = True
            if result.error:
                log.warning(
                    "Voice sync PATCH failed: tenant=%s assistant=%s (%s)",
                    tenant_id, aid, result.error,
                )
        assistants.append(
            {
                "assistant_id": result.assistant_id,
                "name": result.name,
                "before": result.before,
                "after": result.after,
                "changed": plan.changed,
                "applied": result.applied,
                "verified": result.verified,
                "error": result.error,
            }
        )

    applied = (not dry_run) and not any_error
    if applied:
        log.info("Tenant '%s': voice pushed to live VAPI assistant(s).", tenant_id)

    if dry_run:
        message = f"Dry run — showing the exact payload for {len(assistants)} assistant(s). No changes were made."
    elif any_error:
        message = "Couldn't update every assistant — see the error on each one below."
    elif all(a["changed"] is False for a in assistants):
        message = "Already up to date — nothing to change."
    else:
        message = "Live Esmi updated. New callers will hear this voice."

    first = assistants[0] if assistants else {}
    return {
        "tenant_id": tenant_id,
        # Flattened from the first assistant for the common (today: every
        # allow-listed tenant) single-assistant case — `assistants` below is
        # the source of truth for a tenant with more than one.
        "assistant_id": first.get("assistant_id"),
        "before": first.get("before"),
        "after": first.get("after"),
        "assistants": assistants,
        "applied": applied,
        "dry_run": dry_run,
        "message": message,
    }
