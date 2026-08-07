# voice_library.py — shared short-id -> real ElevenLabs voiceId catalog.
#
# Single source of truth for two independent consumers that must never drift
# apart: scripts/sync_vapi_voice.py (pushes a tenant's chosen voice onto their
# live VAPI assistant) and platform_api/voice_preview.py (synthesizes a
# preview of that same voice via ElevenLabs directly — docs/ESMI_DASHBOARD_UX.md
# Section 12.2). Both need the exact same short-id -> voiceId mapping, or a
# tenant's preview and their live assistant could end up speaking in two
# different voices while showing the same dashboard selection.
#
# docs/ESMI_DASHBOARD_UX.md Section 3.3 names 8 voices (ava, mateo, sofia,
# elena, lucas, camila, noah, isabel) but those are still design-spec
# placeholders, not real ElevenLabs voice IDs — none of them exist in this
# repo or in any VAPI dashboard yet. Add a placeholder's real mapping by:
#   1. Running `railway run python scripts/sync_vapi_voice.py --show-current`
#      for each live tenant to see their real, already-configured voiceId
#      (chosen by hand when the assistant was created — see
#      sales/INTEGRATIONS_SETUP_MANUAL.md Part D).
#   2. Deciding which dashboard-facing short id that voiceId should map to.
#   3. Test-calling it (Quality Studio "Spanish caller" scenario, or a real
#      call) before trusting the mapping for a client's assistant.
#
# A voice_id saved on a TenantConfig that has no entry here must fail loudly
# (scripts/sync_vapi_voice.py's --apply, platform_api/voice_preview.py's
# preview endpoint) rather than silently doing nothing or guessing.
VOICE_LIBRARY: dict[str, str] = {
    # Confirmed live via `railway run python scripts/sync_vapi_voice.py
    # --show-current`: as of this mapping, ALL THREE live tenants
    # (default/Orchelix, otro-nivel, coastline-condos) are configured on VAPI
    # with this exact same ElevenLabs voice (model eleven_multilingual_v2) —
    # there is no per-tenant distinction yet, hence one neutral catalog id
    # rather than a personality name. More voices (the Section 3.3 roster)
    # get their own entries here once each is actually chosen and validated
    # against a real tenant's assistant.
    "esmi-default": "hpp4J3VqNfWAUOO0d1Us",
}


def resolve_voice_id(voice_id: str) -> str | None:
    """Resolve a dashboard-facing short voice id (e.g. "sofia") to a real
    ElevenLabs voiceId, or None when there is no mapping yet.

    Callers must treat None as "refuse to guess" — never fall back to a
    default voice or send a null voiceId anywhere.
    """
    return VOICE_LIBRARY.get((voice_id or "").strip().lower())
