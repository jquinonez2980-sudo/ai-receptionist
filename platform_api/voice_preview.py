# platform_api/voice_preview.py — POST /platform/voice/preview (Voice Studio,
# docs/ESMI_DASHBOARD_UX.md Section 3.5 / 4 / 12.2).
#
# "Hear exactly how Esmi will greet your callers" only holds if the preview
# calls the SAME ElevenLabs voiceId the tenant's live VAPI assistant is
# configured with — VAPI itself has no simple preview endpoint, so this calls
# ElevenLabs' TTS API directly. voice_library.VOICE_LIBRARY (short dashboard
# id -> real ElevenLabs voiceId) is the single source of truth for that
# mapping, shared with scripts/sync_vapi_voice.py so preview and production
# can never point at two different voices for the same voice_id.
#
# Deviates from the illustrative contract in Section 4 in one way: tenant_id
# is resolved from the X-Tenant-Id header via require_tenant() (like every
# other /platform/* route — platform_api/security.py), not a body field.
# Section 12.3's actual requirement — every write must carry tenant_id
# explicitly — is satisfied either way; this keeps one convention instead of
# two on the same API surface.
#
# Caching: previews are stored in R2 (platform_api/recordings.py's existing
# audio store) under a key that hashes every input that can change the audio
# (tenant, voice, speed, language, text). Because the key is content-
# addressed, a cache hit is always correct for that key — the same key can
# never legitimately mean two different audios — so entries are kept
# indefinitely rather than expired on a timer. If storage cost ever matters,
# add an R2 lifecycle rule on the `voice_previews/` prefix; that is bucket
# config, not something this code needs to implement.
#
# ElevenLabs key: platform-level (ELEVENLABS_API_KEY), not tenant_secret()-
# resolved. VOICE_LIBRARY's short ids are one shared catalog across every
# tenant — the same ElevenLabs account VAPI's 11labs integration already
# uses — so this mirrors tools._get_vapi_key() (one global platform key), not
# tools._get_sendgrid_key() (which IS per-tenant, because each tenant sends
# its own notification email from its own SendGrid identity).

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from platform_api.config import _LANGUAGE_PREFS, _VOICE_SPEED_MAX, _VOICE_SPEED_MIN
from platform_api.security import require_tenant, verify_platform_secret
from voice_library import resolve_voice_id

log = logging.getLogger(__name__)

router = APIRouter()

# Preview text comes from the dashboard's greeting editor or a sample-script
# chip (docs/ESMI_DASHBOARD_UX.md Section 3.4/3.6) — longer than a single
# greeting (_MAX_GREETING_LEN in config.py) since "booking flow sample" /
# "escalation sample" scripts run several sentences, but still a short spoken
# clip, not an arbitrary document.
_MAX_PREVIEW_TEXT_LEN = 1000

_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
# Fixed output format so duration can be estimated from byte size alone
# (num_bytes * 8 / bitrate) without decoding audio or shelling out to ffprobe.
_OUTPUT_FORMAT = "mp3_44100_128"
_BITRATE_BPS = 128_000


class VoicePreviewRequest(BaseModel):
    voice_id: str
    speed: float = 1.0
    language: str = "auto"
    text: str = Field(min_length=1, max_length=_MAX_PREVIEW_TEXT_LEN)


def _get_elevenlabs_key() -> Optional[str]:
    """Platform-level ElevenLabs key — see module docstring for why this is
    global rather than tenant_secret()-resolved."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return key
    key_b64 = os.environ.get("ELEVENLABS_API_KEY_B64")
    if key_b64:
        try:
            import base64

            return base64.b64decode(key_b64).decode("utf-8")
        except Exception as e:
            log.warning(f"ELEVENLABS_API_KEY_B64 decode failed: {e}")
    return None


def _cache_key(tenant_id: str, voice_id: str, speed: float, language: str, text: str) -> str:
    """Content-addressed id — every input that changes the audio goes in."""
    raw = f"{tenant_id}:{voice_id}:{speed}:{language}:{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _object_key(tenant_id: str, cache_key: str) -> str:
    return f"voice_previews/{tenant_id}/{cache_key}.mp3"


def _estimate_duration_sec(num_bytes: int) -> float:
    return round((num_bytes * 8) / _BITRATE_BPS, 1)


def _synthesize(elevenlabs_voice_id: str, api_key: str, speed: float, text: str) -> bytes:
    """Call ElevenLabs' TTS API. Raises on any non-2xx or transport error."""
    import requests

    resp = requests.post(
        _ELEVENLABS_TTS_URL.format(voice_id=elevenlabs_voice_id),
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        params={"output_format": _OUTPUT_FORMAT},
        json={"text": text, "voice_settings": {"speed": speed}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


@router.post("/platform/voice/preview")
def platform_voice_preview(body: VoicePreviewRequest, request: Request) -> dict:
    """Synthesize (or reuse a cached) preview of `text` in a tenant's voice.

    Sync `def` on purpose (FastAPI threadpool) — every branch here is either
    a blocking R2 call or an outbound HTTP call to ElevenLabs, matching every
    other platform_api route that touches R2 (platform_api/knowledge.py,
    platform_api/recordings.py).
    """
    verify_platform_secret(request)
    tenant_id = require_tenant(request)

    voice_id = body.voice_id.strip().lower()
    speed = body.speed
    language = body.language.strip().lower()
    text = body.text.strip()

    if not (_VOICE_SPEED_MIN <= speed <= _VOICE_SPEED_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"speed must be between {_VOICE_SPEED_MIN} and {_VOICE_SPEED_MAX}",
        )
    if language not in _LANGUAGE_PREFS:
        raise HTTPException(
            status_code=400,
            detail=f"language must be one of: {', '.join(sorted(_LANGUAGE_PREFS))}",
        )
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")

    elevenlabs_voice_id = resolve_voice_id(voice_id)
    if elevenlabs_voice_id is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Voice preview is not available for voice_id '{voice_id}' yet — "
                "no ElevenLabs voice is mapped in voice_library.VOICE_LIBRARY. "
                "Run scripts/sync_vapi_voice.py --show-current to find the real "
                "voice id before this voice can be previewed."
            ),
        )

    from platform_api.recordings import _bucket, _get_client, r2_configured

    if not r2_configured():
        raise HTTPException(status_code=503, detail="Voice preview storage (R2) is not configured.")

    api_key = _get_elevenlabs_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="ELEVENLABS_API_KEY is not configured.")

    client = _get_client()
    cache_key = _cache_key(tenant_id, voice_id, speed, language, text)
    object_key = _object_key(tenant_id, cache_key)

    try:
        head = client.head_object(Bucket=_bucket(), Key=object_key)
        size = head["ContentLength"]
    except Exception:
        try:
            audio = _synthesize(elevenlabs_voice_id, api_key, speed, text)
        except Exception as e:
            log.warning(
                "ElevenLabs TTS failed for tenant=%s voice=%s (%s: %s).",
                tenant_id, voice_id, type(e).__name__, e,
            )
            raise HTTPException(
                status_code=502, detail="Voice preview synthesis failed — try again."
            )
        if not audio:
            raise HTTPException(
                status_code=502, detail="Voice preview synthesis returned no audio."
            )
        client.put_object(
            Bucket=_bucket(), Key=object_key, Body=audio, ContentType="audio/mpeg"
        )
        size = len(audio)
        log.info(
            "Voice preview cached: tenant=%s key=%s (%d KB)",
            tenant_id, object_key, size // 1024,
        )

    ttl = int(os.environ.get("R2_PRESIGN_TTL_SECONDS", "3600"))
    url = client.generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": object_key}, ExpiresIn=ttl
    )

    return {
        "url": url,
        "duration_sec": _estimate_duration_sec(size),
        "cache_key": cache_key,
    }
