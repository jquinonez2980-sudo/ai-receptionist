#!/usr/bin/env python
"""Push a tenant's dashboard-saved voice/speed onto their real VAPI assistant.

This is the missing half of Voice Studio (docs/ESMI_DASHBOARD_UX.md Section
12.1): TenantConfig.voice_id / TenantConfig.speed (tenants.py) are saveable
via PUT /platform/config, but nothing pushes that value onto the tenant's
live VAPI assistant yet — until this script runs (or its logic is called from
somewhere else), "Save voice settings" changes only what the dashboard shows,
not what callers hear.

WHY THIS IS HIGHER RISK than scripts/update_vapi_webhooks.py (which this
script's structure otherwise mirrors — dry-run default, --apply to execute,
GET-then-PATCH-then-verify, never touch an assistant not found in the
account): a wrong voiceId can make a live assistant fail to speak or use the
wrong voice entirely, and there is no tested EsmiVoice -> ElevenLabs voiceId
mapping in this codebase yet. Every voice choice in every tenant's VAPI
assistant today was set BY HAND in the VAPI dashboard — there is no existing
API-driven precedent for this field the way there was for server.url.

Two modes:

  --show-current [--tenant ID]
      READ-ONLY. Prints each target assistant's current `voice` block exactly
      as VAPI has it (provider, voiceId, and whatever else 11labs is
      configured with — stability, similarityBoost, style, speed, etc.).
      Safe to run anytime; requires no VOICE_LIBRARY entries. Use this FIRST,
      for every live tenant, to learn their real current voiceId before
      building out voice_library.VOICE_LIBRARY — do not guess IDs.

  --tenant ID [--apply]
      Reads voice_id/speed from the LIVE GET /platform/config endpoint (not
      tenants.load_tenant(ID) — see "DB reachability" below for why),
      resolves voice_id through voice_library.VOICE_LIBRARY to a real
      ElevenLabs voiceId, and PATCHes only `voice.voiceId` + `voice.speed`
      onto that tenant's VAPI assistant(s) — every other key already on the
      assistant's `voice` object (provider, stability, similarityBoost,
      style, ...) is read back from VAPI and re-sent unchanged, never
      guessed. Dry run by default. Requires --tenant explicitly — no "sync
      every tenant" mode, unlike the webhook script, because a bad voice
      change is more disruptive than a bad server URL and each one deserves
      a human looking at the diff.

DB reachability: tenants.load_tenant() is DB-first (a dashboard "Save voice
settings" writes to the tenant_configs Postgres table, not to
tenants/<id>/config.json), but `railway run` only exposes Postgres's PRIVATE
hostname, unreachable from a local machine — TENANT_CONFIG_FROM_DB=0 below
forces file-only reads so the rest of this script (assistant ids, which
aren't DB-editable and never drift from the file) still works locally. That
means local load_tenant() alone would silently under-report a DB-config
tenant's real voice_id as empty. --tenant sidesteps this by resolving
voice_id/speed through the live GET /platform/config HTTPS endpoint instead
(PLATFORM_API_SECRET, same secret the dashboard's server uses) — that's the
same DB-first path the live app itself uses, no local Postgres access
needed. --show-current's "dashboard voice_id/speed" lines still read the
local file-only load_tenant() and can be stale for a DB-config tenant; they
are a convenience cross-check, not what --tenant actually acts on.

language_pref is NOT synced by this script. VAPI's 11labs voice object has no
"caller language preference" field — language handling for voice happens via
the Deepgram transcriber's `languages` list (set at assistant creation, see
sales/INTEGRATIONS_SETUP_MANUAL.md Part D) and the voice system prompt's
language rules, neither of which this script touches.

Usage (VAPI_API_KEY / PLATFORM_API_SECRET come from the Railway service env —
never put them in a file; --show-current only needs VAPI_API_KEY):

    railway run python scripts/sync_vapi_voice.py --show-current
    railway run python scripts/sync_vapi_voice.py --tenant otro-nivel            # dry run
    railway run python scripts/sync_vapi_voice.py --tenant otro-nivel --apply

Never prints secret values. Test against otro-nivel or coastline-condos
before ever running --apply against the default (Orchelix) tenant.

ALLOW-LIST: --tenant (dry run or --apply) only works for tenants in
SYNC_ALLOWED_TENANTS below. This is a hard gate, not a suggestion — the
default (Orchelix) assistant is the live production number and has no
tested EsmiVoice -> ElevenLabs mapping precedent, so it stays out of this
script's reach until otro-nivel/coastline-condos prove the sync path is
safe. --show-current is exempt (read-only, no PATCH, and it's how you'd
inspect Orchelix's live voice before ever deciding to expand this list).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

# Registry file configs are all we need here; skip DB lookups (railway run
# injects the PRIVATE postgres host, unreachable from a local machine — same
# reasoning as scripts/update_vapi_webhooks.py).
os.environ.setdefault("TENANT_CONFIG_FROM_DB", "0")

from tenants import _all_tenant_ids, load_tenant  # noqa: E402
from vapi_voice_sync import (  # noqa: E402
    SYNC_ALLOWED_TENANTS,
    VapiSyncError,
    apply_assistant_voice,
    assistant_ids_for,
    plan_assistant_voice,
    vapi_api,
)
from voice_library import VOICE_LIBRARY  # noqa: E402


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: {name} is not set. Run via: railway run python {sys.argv[0]}")
        sys.exit(1)
    return v


API_KEY = _require_env("VAPI_API_KEY")

ESMI_BASE_URL = os.environ.get(
    "ESMI_BASE_URL", "https://ai-receptionist-production-5375.up.railway.app"
).rstrip("/")


def api(method: str, path: str, body: dict | None = None) -> dict:
    """Thin wrapper binding vapi_voice_sync.vapi_api to this script's
    env-resolved API_KEY, and translating its VapiSyncError to the plain
    RuntimeError this file's callers already catch."""
    try:
        return vapi_api(method, path, API_KEY, body)
    except VapiSyncError as e:
        raise RuntimeError(str(e)) from None


def live_voice_config(tenant_id: str) -> dict:
    """voice_id/speed via the live GET /platform/config endpoint — the same
    DB-first source the dashboard itself reads, and the only way to see a
    DB-config tenant's real saved voice from a machine that can't reach
    Postgres directly (see module docstring "DB reachability"). Not routed
    through api()/BASE — this hits ESMI_BASE_URL (our own app), not VAPI.
    """
    secret = _require_env("PLATFORM_API_SECRET")
    req = urllib.request.Request(
        f"{ESMI_BASE_URL}/platform/config",
        headers={
            "X-Platform-Secret": secret,
            "X-Tenant-Id": tenant_id,
            "User-Agent": "curl/8.9.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(
            f"GET /platform/config (tenant={tenant_id}) -> HTTP {e.code}: {detail}"
        ) from None
    cfg = data.get("config") or {}
    return {"voice_id": (cfg.get("voice_id") or "").strip().lower(), "speed": cfg.get("speed", 1.0)}


# SYNC_ALLOWED_TENANTS and assistant_ids_for() now live in vapi_voice_sync.py
# (shared with platform_api/voice_sync.py's "Apply to live Esmi" endpoint) —
# see module docstring's ALLOW-LIST section for the rationale, unchanged.


def all_tenant_ids() -> list[str]:
    return ["default", *_all_tenant_ids()]


def show_current(tenant_filter: str | None) -> int:
    targets = [tenant_filter] if tenant_filter else all_tenant_ids()
    for tid in targets:
        aids = assistant_ids_for(tid)
        if not aids:
            print(f"=== tenant: {tid} — no VAPI assistant ids configured ===")
            continue
        for aid in aids:
            try:
                a = api("GET", f"/assistant/{aid}")
            except RuntimeError as e:
                print(f"=== tenant: {tid} / assistant {aid} — FAILED: {e} ===")
                continue
            name = a.get("name") or aid
            voice = a.get("voice") or {}
            cfg = load_tenant(tid)
            print(f"\n=== {name} (tenant: {tid}, assistant {aid}) ===")
            print(f"  live voice block   : {json.dumps(voice, indent=2)}")
            print(f"  dashboard voice_id : {cfg.voice_id!r} (not yet mapped to the above)")
            print(f"  dashboard speed    : {cfg.speed}")
    return 0


def sync_tenant(tenant_id: str, apply: bool) -> int:
    if tenant_id not in SYNC_ALLOWED_TENANTS:
        print(
            f"ERROR: tenant '{tenant_id}' is not on the sync allow-list "
            f"({', '.join(sorted(SYNC_ALLOWED_TENANTS))}). Refusing — see "
            "module docstring's ALLOW-LIST section. This blocks dry runs too: "
            "the point is that this tenant's payload never gets built or "
            "printed by this path yet, not just that --apply is withheld. "
            f"Use --show-current --tenant {tenant_id} to inspect its live "
            "voice config read-only."
        )
        return 1

    aids = assistant_ids_for(tenant_id)
    if not aids:
        print(f"Tenant '{tenant_id}' has no VAPI assistant ids configured — nothing to sync.")
        return 1

    live = live_voice_config(tenant_id)
    voice_id, speed = live["voice_id"], live["speed"]

    if not voice_id:
        print(f"Tenant '{tenant_id}' has no voice_id saved (live config voice_id is empty) "
              "— nothing to sync.")
        return 0

    target_elevenlabs_id = VOICE_LIBRARY.get(voice_id)
    if target_elevenlabs_id is None:
        print(
            f"ERROR: voice_id '{voice_id}' has no voice_library.VOICE_LIBRARY entry. "
            f"Run --show-current --tenant {tenant_id} to see the assistant's real current "
            "voiceId, then add the mapping before syncing. Refusing to guess."
        )
        return 1

    failed = []
    for aid in aids:
        try:
            plan = plan_assistant_voice(aid, target_elevenlabs_id, speed, API_KEY)
        except VapiSyncError as e:
            print(f"\n=== assistant {aid} (tenant: {tenant_id}) — FAILED to read: {e} ===")
            failed.append(aid)
            continue

        print(f"\n=== {plan.name} (tenant: {tenant_id}, assistant {aid}) ===")
        print(f"  voiceId : {plan.before.get('voiceId')!r} -> {target_elevenlabs_id!r}")
        print(f"  speed   : {plan.before.get('speed')!r} -> {speed!r}")

        if not plan.changed:
            print("  already in sync — nothing to do")
            continue

        # Preserve every other key already on the voice object (provider,
        # stability, similarityBoost, style, useSpeakerBoost, ...) — only
        # voiceId and speed are ours to change. Same "never guess, only set
        # what we intend" rule as scripts/update_vapi_webhooks.py. Built (and
        # printed) on every run, dry or applied, so dry-run output IS the
        # exact payload — never a paraphrase of it.
        print(f"  PATCH payload (voice): {json.dumps(plan.after, indent=2)}")

        if not apply:
            continue

        result = apply_assistant_voice(plan, API_KEY)
        if result.error:
            print(f"  ✘ FAILED: {result.error}")
            failed.append(plan.name)
        elif result.verified:
            print("  ✔ patched + verified")
        else:
            print(f"  ✘ verification mismatch: {result.after}")
            failed.append(plan.name)

    if not apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to execute.")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", help="tenant id to sync (required unless --show-current)")
    p.add_argument("--show-current", action="store_true", help="read-only: print live voice config")
    p.add_argument("--apply", action="store_true", help="execute (default is dry run)")
    args = p.parse_args()

    if args.show_current:
        return show_current(args.tenant)

    if not args.tenant:
        p.error("--tenant is required (no sync-all mode — see module docstring for why)")

    return sync_tenant(args.tenant, args.apply)


if __name__ == "__main__":
    sys.exit(main())
