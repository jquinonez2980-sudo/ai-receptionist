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
      Reads voice_id/speed from tenants.load_tenant(ID), resolves voice_id
      through voice_library.VOICE_LIBRARY to a real ElevenLabs voiceId, and PATCHes
      only `voice.voiceId` + `voice.speed` onto that tenant's VAPI
      assistant(s) — every other key already on the assistant's `voice`
      object (provider, stability, similarityBoost, style, ...) is read back
      from VAPI and re-sent unchanged, never guessed. Dry run by default.
      Requires --tenant explicitly — no "sync every tenant" mode, unlike the
      webhook script, because a bad voice change is more disruptive than a
      bad server URL and each one deserves a human looking at the diff.

language_pref is NOT synced by this script. VAPI's 11labs voice object has no
"caller language preference" field — language handling for voice happens via
the Deepgram transcriber's `languages` list (set at assistant creation, see
sales/INTEGRATIONS_SETUP_MANUAL.md Part D) and the voice system prompt's
language rules, neither of which this script touches.

Usage (VAPI_API_KEY comes from the Railway service env — never put it in a
file):

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
from voice_library import VOICE_LIBRARY  # noqa: E402

BASE = "https://api.vapi.ai"


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"ERROR: {name} is not set. Run via: railway run python {sys.argv[0]}")
        sys.exit(1)
    return v


API_KEY = _require_env("VAPI_API_KEY")


def api(method: str, path: str, body: dict | None = None) -> dict | list:
    # Cloudflare 403s Python's default User-Agent — send a normal one (same
    # workaround as scripts/update_vapi_webhooks.py).
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
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
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


# Default tenant (Orchelix) intentionally has no vapi ids in its own
# config.json — same fact scripts/update_vapi_webhooks.py encodes.
ORCHELIX_ASSISTANT_ID = "d5e020bf-0235-4214-a57f-de30e8072b0b"

# Hard allow-list for --tenant (both dry run and --apply) — see module
# docstring "ALLOW-LIST" section. Expand only after a tenant's sync has been
# dry-run reviewed and apply-tested here first.
SYNC_ALLOWED_TENANTS = frozenset({"otro-nivel", "coastline-condos"})


def assistant_ids_for(tenant_id: str) -> list[str]:
    if tenant_id == "default":
        return [ORCHELIX_ASSISTANT_ID]
    return list(load_tenant(tenant_id).vapi_assistant_ids)


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

    cfg = load_tenant(tenant_id)
    aids = assistant_ids_for(tenant_id)
    if not aids:
        print(f"Tenant '{tenant_id}' has no VAPI assistant ids configured — nothing to sync.")
        return 1

    if not cfg.voice_id:
        print(f"Tenant '{tenant_id}' has no voice_id saved (TenantConfig.voice_id is empty) "
              "— nothing to sync.")
        return 0

    target_elevenlabs_id = VOICE_LIBRARY.get(cfg.voice_id)
    if target_elevenlabs_id is None:
        print(
            f"ERROR: voice_id '{cfg.voice_id}' has no voice_library.VOICE_LIBRARY entry. "
            f"Run --show-current --tenant {tenant_id} to see the assistant's real current "
            "voiceId, then add the mapping before syncing. Refusing to guess."
        )
        return 1

    failed = []
    for aid in aids:
        a = api("GET", f"/assistant/{aid}")
        name = a.get("name") or aid
        current_voice = dict(a.get("voice") or {})
        cur_voice_id = current_voice.get("voiceId")
        cur_speed = current_voice.get("speed")

        print(f"\n=== {name} (tenant: {tenant_id}, assistant {aid}) ===")
        print(f"  voiceId : {cur_voice_id!r} -> {target_elevenlabs_id!r}")
        print(f"  speed   : {cur_speed!r} -> {cfg.speed!r}")

        if cur_voice_id == target_elevenlabs_id and cur_speed == cfg.speed:
            print("  already in sync — nothing to do")
            continue

        # Preserve every other key already on the voice object (provider,
        # stability, similarityBoost, style, useSpeakerBoost, ...) — only
        # voiceId and speed are ours to change. Same "never guess, only set
        # what we intend" rule as scripts/update_vapi_webhooks.py. Built (and
        # printed) on every run, dry or applied, so dry-run output IS the
        # exact payload — never a paraphrase of it.
        new_voice = {**current_voice, "voiceId": target_elevenlabs_id, "speed": cfg.speed}
        print(f"  PATCH payload (voice): {json.dumps(new_voice, indent=2)}")

        if not apply:
            continue

        try:
            api("PATCH", f"/assistant/{aid}", {"voice": new_voice})
            check = api("GET", f"/assistant/{aid}")
            got = check.get("voice") or {}
            if got.get("voiceId") == target_elevenlabs_id and got.get("speed") == cfg.speed:
                print("  ✔ patched + verified")
            else:
                print(f"  ✘ verification mismatch: {got}")
                failed.append(name)
        except RuntimeError as e:
            print(f"  ✘ FAILED: {e}")
            failed.append(name)

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
