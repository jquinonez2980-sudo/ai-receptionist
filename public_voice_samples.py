# public_voice_samples.py — fixed sample scripts for the public, unauthenticated
# try-esmi voice preview (docs/ESMI_DASHBOARD_UX.md Section 6).
#
# Single source of truth for platform_api/public_voice_preview.py, same
# "shared module" pattern as voice_library.py. The public preview endpoint
# NEVER accepts free-form text from a caller — that is the entire cost/abuse
# control (fixed ElevenLabs synthesis inputs, bounded set, cacheable
# indefinitely). A client sends only a `sample_id` + `language`; the server
# resolves the actual spoken words from here. Do not add a "custom text"
# escape hatch to this endpoint — that is what the authenticated, tenant-
# scoped /platform/voice/preview (platform_api/voice_preview.py) is for.
#
# Short by design (~15-25 words each) — every sample is a real ElevenLabs
# TTS call on a cache miss, so keeping these short keeps the worst-case cost
# of a scraped/abused endpoint bounded even before the rate limiter kicks in.

from __future__ import annotations

# Which VOICE_LIBRARY (voice_library.py) entries are exposed on the PUBLIC
# preview. Deliberately a separate, smaller list: a voice being mapped for
# tenant use does not automatically make it publicly previewable — adding a
# voice here is its own decision, made once that voice's public sample
# audio has actually been reviewed.
PUBLIC_VOICE_IDS = frozenset({"esmi-default"})

PUBLIC_LANGUAGES = ("en", "es")

# {sample_id: {"label": ..., "en": ..., "es": ...}}
# label is the human-facing industry chip name shown in the UI; en/es are
# the literal words ElevenLabs will speak — fictional business names, no
# real company, matching the placeholder style already used elsewhere in
# the marketing site (e.g. orhelix-website's try-esmi LOGOS list).
PUBLIC_SAMPLES: dict[str, dict[str, str]] = {
    "general": {
        "label": "General business",
        "en": "Thanks for calling — this is Esmi. How can I help you today?",
        "es": "Gracias por llamar — habla Esmi. ¿En qué le puedo ayudar hoy?",
    },
    "hvac": {
        "label": "HVAC & home services",
        "en": (
            "Thanks for calling ABC Heating and Air, this is Esmi. Is this an "
            "emergency, or would you like to book a service visit?"
        ),
        "es": (
            "Gracias por llamar a ABC Calefacción y Aire, habla Esmi. ¿Es una "
            "emergencia, o le gustaría agendar una visita de servicio?"
        ),
    },
    "dental": {
        "label": "Dental & medical",
        "en": (
            "Thanks for calling Riverside Dental, this is Esmi. Are you an "
            "existing patient, or would you like to schedule a new visit?"
        ),
        "es": (
            "Gracias por llamar a Riverside Dental, habla Esmi. ¿Es usted "
            "paciente actual, o le gustaría agendar una primera cita?"
        ),
    },
    "law-firm": {
        "label": "Law firms",
        "en": (
            "Thanks for calling Harper and Associates, this is Esmi. Can you "
            "tell me briefly what your legal matter involves?"
        ),
        "es": (
            "Gracias por llamar a Harper y Asociados, habla Esmi. ¿Puede "
            "contarme brevemente de qué se trata su caso legal?"
        ),
    },
    "real-estate": {
        "label": "Real estate",
        "en": (
            "Thanks for calling Skyline Realty, this is Esmi. Are you looking "
            "to buy, sell, or schedule a showing?"
        ),
        "es": (
            "Gracias por llamar a Skyline Realty, habla Esmi. ¿Está buscando "
            "comprar, vender, o agendar una visita?"
        ),
    },
}


def resolve_sample(sample_id: str, language: str) -> str | None:
    """The literal text to speak for (sample_id, language), or None when
    either isn't in the fixed catalog. Callers must treat None as "refuse to
    guess" — never fall back to a default sample or synthesize arbitrary text."""
    entry = PUBLIC_SAMPLES.get((sample_id or "").strip().lower())
    if entry is None:
        return None
    return entry.get((language or "").strip().lower())
