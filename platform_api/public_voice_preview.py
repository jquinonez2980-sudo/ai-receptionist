# platform_api/public_voice_preview.py — POST /platform/public/voice/preview
# (try-esmi public voice preview, docs/ESMI_DASHBOARD_UX.md Section 6).
#
# The public, unauthenticated sibling of platform_api/voice_preview.py (the
# dashboard's tenant-scoped preview). Deliberately a SEPARATE endpoint, not
# a "public mode" flag on the tenant one — Section 6's own spec is explicit:
# "do not reuse the tenant-scoped dashboard endpoint, since it has no
# tenant_id to bill usage against and must never touch a real tenant's
# ElevenLabs quota." The two differ in every dimension that matters for
# abuse:
#   - No X-Platform-Secret / X-Tenant-Id — anyone can call this.
#   - No free-form `text` — only a fixed `sample_id` from
#     public_voice_samples.PUBLIC_SAMPLES; the server resolves the actual
#     words, the client never supplies them. This is the primary cost/abuse
#     control: a bounded, cacheable-forever input space.
#   - No `speed` — always 1.0.
#   - Its own R2 key prefix (public_voice_previews/) — never shares a cache
#     key or a directory with a real tenant's previews (voice_previews/<id>/).
#   - Strict IP rate limit via api.py's existing slowapi `limiter` (same
#     mechanism /chat already uses, imported rather than reimplemented —
#     see _rate_limit_key's docstring in api.py for the X-Client-IP /
#     X-Chat-Secret proxy-trust reasoning, reused here unchanged).
#
# _synthesize / _get_elevenlabs_key / duration estimate are intentionally
# DUPLICATED from voice_preview.py rather than extracted into a shared
# module — small (~20 lines), and this keeps that already-shipped,
# tenant-facing endpoint untouched by this change. Worth revisiting if a
# third ElevenLabs caller shows up.

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from public_voice_samples import PUBLIC_LANGUAGES, PUBLIC_VOICE_IDS, resolve_sample
from voice_library import resolve_voice_id

log = logging.getLogger(__name__)

router = APIRouter()

# Same Limiter instance /chat and /health/deep use (api.py), imported from
# rate_limit.py rather than from api.py itself — see that module's
# docstring for why importing straight from api.py here would be a
# circular import that only works depending on which module gets imported
# first.
from rate_limit import limiter  # noqa: E402

_WATERMARK = "Sample only — your Esmi will use your business name and services."

_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_OUTPUT_FORMAT = "mp3_44100_128"
_BITRATE_BPS = 128_000
_SPEED = 1.0  # public preview has no speed control — one fixed, natural pace


class PublicVoicePreviewRequest(BaseModel):
    sample_id: str
    language: str
    voice_id: str = "esmi-default"


def _get_elevenlabs_key() -> Optional[str]:
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


def _cache_key(voice_id: str, language: str, sample_id: str, text: str) -> str:
    """Content-addressed, namespaced with "public:" so this can never
    collide with platform_api/voice_preview.py's tenant cache keys even if
    the two ever shared a bucket prefix by mistake."""
    raw = f"public:{voice_id}:{language}:{sample_id}:{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _object_key(cache_key: str) -> str:
    return f"public_voice_previews/{cache_key}.mp3"


def _estimate_duration_sec(num_bytes: int) -> float:
    return round((num_bytes * 8) / _BITRATE_BPS, 1)


def _synthesize(elevenlabs_voice_id: str, api_key: str, text: str) -> bytes:
    import requests

    resp = requests.post(
        _ELEVENLABS_TTS_URL.format(voice_id=elevenlabs_voice_id),
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        params={"output_format": _OUTPUT_FORMAT},
        json={"text": text, "voice_settings": {"speed": _SPEED}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


@router.post("/platform/public/voice/preview")
@limiter.limit("5/minute")
def platform_public_voice_preview(body: PublicVoicePreviewRequest, request: Request) -> dict:
    """Synthesize (or reuse a cached) preview of a FIXED sample script.

    No auth — this is the one platform_api route anyone can call. Rate-
    limited strictly (5/min/IP, vs /chat's 10/min) since a cache miss is a
    real ElevenLabs charge and there's no tenant to attribute cost to.
    Sync `def` on purpose, same reasoning as voice_preview.py: every branch
    here is a blocking R2 or ElevenLabs call.
    """
    sample_id = body.sample_id.strip().lower()
    language = body.language.strip().lower()
    voice_id = body.voice_id.strip().lower()

    if language not in PUBLIC_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"language must be one of: {', '.join(PUBLIC_LANGUAGES)}",
        )

    if voice_id not in PUBLIC_VOICE_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"voice_id '{voice_id}' is not available in the public preview yet — "
                f"only {', '.join(sorted(PUBLIC_VOICE_IDS))} are public."
            ),
        )

    text = resolve_sample(sample_id, language)
    if text is None:
        raise HTTPException(
            status_code=400,
            detail=f"sample_id '{sample_id}' is not a recognized public sample.",
        )

    elevenlabs_voice_id = resolve_voice_id(voice_id)
    if elevenlabs_voice_id is None:
        # PUBLIC_VOICE_IDS and VOICE_LIBRARY are two independent lists by
        # design (see public_voice_samples.py) — this only fires if a voice
        # id was added to the public list without also being mapped, a
        # config mistake, not a caller error.
        raise HTTPException(
            status_code=503,
            detail=f"voice_id '{voice_id}' has no ElevenLabs mapping — this is a configuration issue, not yours.",
        )

    from platform_api.recordings import _bucket, _get_client, r2_configured

    if not r2_configured():
        raise HTTPException(status_code=503, detail="Voice preview storage (R2) is not configured.")

    api_key = _get_elevenlabs_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="ELEVENLABS_API_KEY is not configured.")

    client = _get_client()
    cache_key = _cache_key(voice_id, language, sample_id, text)
    object_key = _object_key(cache_key)

    try:
        head = client.head_object(Bucket=_bucket(), Key=object_key)
        size = head["ContentLength"]
    except Exception:
        try:
            audio = _synthesize(elevenlabs_voice_id, api_key, text)
        except Exception as e:
            log.warning(
                "Public voice preview TTS failed for sample=%s lang=%s (%s: %s).",
                sample_id, language, type(e).__name__, e,
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
        log.info("Public voice preview cached: key=%s (%d KB)", object_key, size // 1024)

    ttl = int(os.environ.get("R2_PRESIGN_TTL_SECONDS", "3600"))
    url = client.generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": object_key}, ExpiresIn=ttl
    )

    return {
        "url": url,
        "duration_sec": _estimate_duration_sec(size),
        "cache_key": cache_key,
        "text": text,
        "watermark": _WATERMARK,
    }
