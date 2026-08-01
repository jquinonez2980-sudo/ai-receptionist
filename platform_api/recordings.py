# platform_api/recordings.py — copy VAPI call recordings to Cloudflare R2.
#
# WHY: VAPI's recording URLs (storage.vapi.ai) are temporary — they expire,
# and with them the Call Log's audio players. R2 is the permanent home
# (S3-compatible, zero egress fees). The `calls.recording_key` column holds
# either a raw http(s) URL (pre-R2 rows, or R2 not configured) or an R2
# object key like `recordings/<tenant>/<call_id>.wav`; GET /platform/calls
# presigns keys into short-lived playable URLs at read time so the bucket
# stays private.
#
# Fail-soft contract (same as every platform_api module): R2 unconfigured or
# any copy error → the raw VAPI URL stays in the row, everything behaves
# exactly as before this module existed. A copy failure must never lose a
# call row (copy runs AFTER the DB upsert).
#
# Config (Railway env vars, platform-owned — not per-tenant):
#   R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
#   R2_PRESIGN_TTL_SECONDS (optional, default 3600)

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

log = logging.getLogger(__name__)

_MAX_RECORDING_BYTES = 100 * 1024 * 1024  # sanity cap: refuse >100MB downloads
_DOWNLOAD_TIMEOUT = 60

_client = None
_client_lock = threading.Lock()

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def r2_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    )


def _bucket() -> str:
    return os.environ["R2_BUCKET"]


def _get_client():
    """Lazy process-wide S3 client for R2, or None when unconfigured."""
    global _client
    if _client is not None:
        return _client
    if not r2_configured():
        return None
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client(
                "s3",
                endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2}),
            )
    return _client


def recording_object_key(tenant_id: str, vapi_call_id: str, content_type: str = "") -> str:
    ext = ".mp3" if "mpeg" in content_type or "mp3" in content_type else ".wav"
    safe_call = _SAFE_ID.sub("_", vapi_call_id)[:128]
    return f"recordings/{tenant_id}/{safe_call}{ext}"


def copy_recording_to_r2(tenant_id: str, vapi_call_id: str, url: str) -> Optional[str]:
    """Download the recording from VAPI and store it in R2.

    Returns the object key on success, None on any failure (logged). Never
    raises — callers treat None as "keep the URL we already have".
    """
    client = _get_client()
    if client is None or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        import requests

        resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "audio/wav")
        declared = int(resp.headers.get("Content-Length") or 0)
        if declared > _MAX_RECORDING_BYTES:
            log.warning("Recording %s: %s bytes exceeds cap — not copied.", vapi_call_id, declared)
            return None
        body = resp.content  # a few MB; fine in memory
        if len(body) > _MAX_RECORDING_BYTES or not body:
            log.warning("Recording %s: empty or oversized body — not copied.", vapi_call_id)
            return None

        key = recording_object_key(tenant_id, vapi_call_id, content_type)
        client.put_object(
            Bucket=_bucket(), Key=key, Body=body, ContentType=content_type
        )
        log.info(
            "Recording copied to R2: tenant=%s call=%s key=%s (%d KB)",
            tenant_id, vapi_call_id, key, len(body) // 1024,
        )
        return key
    except Exception as e:
        log.warning(
            "Recording %s: copy to R2 failed (%s: %s) — keeping VAPI URL.",
            vapi_call_id, type(e).__name__, e,
        )
        return None


def fetch_fresh_recording_url(vapi_call_id: str) -> Optional[str]:
    """Mint a fresh presigned recording URL via VAPI's API.

    VAPI's presigned URLs (artifact.presignedStereoUrl / presignedMonoUrl)
    expire ~1h after being issued; the recordingUrl/stereoRecordingUrl fields
    are UNSIGNED R2 paths that 400 for everyone. GET /call/{id} returns a
    freshly presigned pair for as long as VAPI retains the audio.
    Requires VAPI_API_KEY (platform env). None on any failure.
    """
    api_key = os.environ.get("VAPI_API_KEY")
    if not api_key or not vapi_call_id:
        return None
    try:
        import requests

        resp = requests.get(
            f"https://api.vapi.ai/call/{vapi_call_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                # Cloudflare 403s python's default UA
                "User-Agent": "curl/8.9.1",
            },
            timeout=30,
        )
        resp.raise_for_status()
        artifact = (resp.json() or {}).get("artifact") or {}
        return artifact.get("presignedStereoUrl") or artifact.get("presignedMonoUrl")
    except Exception as e:
        log.warning("Fresh recording URL for %s failed (%s).", vapi_call_id, e)
        return None


def archive_call_recording(tenant_id: str, vapi_call_id: str, url: Optional[str]) -> Optional[str]:
    """Best available path to a permanent copy: try the given URL, and if
    that fails (expired presign / legacy unsigned path), mint a fresh
    presigned URL from the VAPI API and retry once. Returns the R2 key or
    None; never raises."""
    if not r2_configured():
        return None
    key = copy_recording_to_r2(tenant_id, vapi_call_id, url or "")
    if key:
        return key
    fresh = fetch_fresh_recording_url(vapi_call_id)
    if fresh and fresh != url:
        return copy_recording_to_r2(tenant_id, vapi_call_id, fresh)
    return None


def playable_recording_url(recording_key: Optional[str]) -> Optional[str]:
    """Turn whatever recording_key holds into something an <audio> tag can play.

    http(s) URL → passthrough (pre-R2 rows / R2 unconfigured).
    R2 object key → short-lived presigned GET URL (bucket stays private).
    Presign failure → None (player hidden; transcript remains).
    """
    if not recording_key:
        return None
    if recording_key.lower().startswith(("http://", "https://")):
        return recording_key
    client = _get_client()
    if client is None:
        return None
    try:
        ttl = int(os.environ.get("R2_PRESIGN_TTL_SECONDS", "3600"))
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": recording_key},
            ExpiresIn=ttl,
        )
    except Exception as e:
        log.warning("Presign failed for %s (%s) — omitting recording.", recording_key, e)
        return None


# ── WhatsApp-friendly MP3 export (lazy sidecar) ──────────────────────────────
# In-dashboard playback stays on the original WAV via playable_recording_url.
# Export builds (or reuses) a permanent sibling MP3 in R2 and returns a
# short-lived presigned download URL so the browser never streams large audio
# through the Vercel proxy.

_MP3_BITRATE = "64k"
_MP3_SAMPLE_RATE = "22050"
_CONVERT_TIMEOUT_SEC = 120


class RecordingUnavailable(Exception):
    """Recording missing, expired, or otherwise unrecoverable (→ 404/410)."""


class RecordingExportError(Exception):
    """Convert/R2 failure that is not a missing source (→ 500/503)."""


def ffmpeg_available() -> bool:
    """True when the ffmpeg binary is on PATH (installed in the Docker image)."""
    import shutil

    return shutil.which("ffmpeg") is not None


def mp3_sidecar_key(
    recording_key: str,
    tenant_id: str,
    vapi_call_id: Optional[str] = None,
) -> str:
    """R2 key for the permanent MP3 sidecar next to the archived recording.

    Already-mp3 keys pass through. R2 .wav keys swap extension. Legacy http(s)
    VAPI URLs fall back to the standard recordings/{tenant}/{call}.mp3 layout.
    """
    if recording_key.lower().startswith(("http://", "https://")):
        return recording_object_key(tenant_id, vapi_call_id or "unknown", "audio/mpeg")
    lower = recording_key.lower()
    if lower.endswith(".mp3"):
        return recording_key
    if lower.endswith(".wav"):
        return recording_key[:-4] + ".mp3"
    base, sep, _ext = recording_key.rpartition(".")
    if sep and base:
        return f"{base}.mp3"
    return f"{recording_key}.mp3"


def _r2_object_exists(key: str) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        client.head_object(Bucket=_bucket(), Key=key)
        return True
    except Exception:
        return False


def _download_r2_object(key: str) -> bytes:
    """Fetch object bytes from R2. Raises RecordingUnavailable if missing."""
    client = _get_client()
    if client is None:
        raise RecordingExportError("R2 is not configured")
    try:
        resp = client.get_object(Bucket=_bucket(), Key=key)
        body = resp["Body"].read()
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound") or "NoSuchKey" in type(e).__name__:
            raise RecordingUnavailable(f"Recording object not found in R2: {key}") from e
        # botocore ClientError for missing keys usually has 404 status
        status = getattr(e, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == 404:
            raise RecordingUnavailable(f"Recording object not found in R2: {key}") from e
        raise RecordingExportError(f"Failed to download recording from R2: {e}") from e
    if not body:
        raise RecordingUnavailable(f"Recording object empty in R2: {key}")
    if len(body) > _MAX_RECORDING_BYTES:
        raise RecordingExportError("Recording exceeds size cap")
    return body


def _upload_r2_object(key: str, body: bytes, content_type: str) -> None:
    client = _get_client()
    if client is None:
        raise RecordingExportError("R2 is not configured")
    try:
        client.put_object(Bucket=_bucket(), Key=key, Body=body, ContentType=content_type)
    except Exception as e:
        raise RecordingExportError(f"Failed to upload MP3 to R2: {e}") from e


def convert_audio_to_mp3(source_bytes: bytes) -> bytes:
    """Transcode audio bytes to mono 64kbps MP3 via ffmpeg (phone-friendly size)."""
    import subprocess
    import tempfile
    from pathlib import Path

    if not ffmpeg_available():
        raise RecordingExportError("ffmpeg is not installed on this server")

    in_path = None
    out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".src", delete=False) as fin:
            fin.write(source_bytes)
            in_path = fin.name
        out_path = in_path + ".mp3"
        proc = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                in_path,
                "-ac",
                "1",
                "-ar",
                _MP3_SAMPLE_RATE,
                "-b:a",
                _MP3_BITRATE,
                "-codec:a",
                "libmp3lame",
                out_path,
            ],
            capture_output=True,
            timeout=_CONVERT_TIMEOUT_SEC,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[:500]
            raise RecordingExportError(f"ffmpeg conversion failed: {err or 'unknown error'}")
        out = Path(out_path).read_bytes()
        if not out:
            raise RecordingExportError("ffmpeg produced an empty MP3")
        return out
    except subprocess.TimeoutExpired as e:
        raise RecordingExportError("ffmpeg conversion timed out") from e
    finally:
        if in_path:
            Path(in_path).unlink(missing_ok=True)
        if out_path:
            Path(out_path).unlink(missing_ok=True)


def _resolve_source_r2_key(
    tenant_id: str,
    vapi_call_id: Optional[str],
    recording_key: str,
) -> str:
    """Ensure a permanent R2 object exists for the source recording; return its key.

    Prefer the existing R2 key. If recording_key is still a legacy http(s) VAPI
    URL, attempt archive_call_recording once. Raises RecordingUnavailable when
    nothing recoverable remains.
    """
    if not recording_key:
        raise RecordingUnavailable("No recording for this call")

    if not recording_key.lower().startswith(("http://", "https://")):
        if _r2_object_exists(recording_key):
            return recording_key
        raise RecordingUnavailable("Recording is no longer available in storage")

    # Legacy VAPI URL still on the row — try permanent archive, then re-check.
    if not r2_configured():
        raise RecordingExportError("R2 is not configured")
    archived = archive_call_recording(tenant_id, vapi_call_id or "", recording_key)
    if archived and _r2_object_exists(archived):
        return archived
    raise RecordingUnavailable(
        "Recording link has expired and could not be recovered"
    )


def ensure_mp3_export(
    tenant_id: str,
    vapi_call_id: Optional[str],
    recording_key: str,
) -> str:
    """Return the R2 key of a WhatsApp-friendly MP3 for this call.

    Lazy-creates a permanent sibling object next to the archived WAV on first
    request; subsequent calls only check existence. Never mutates the source WAV.
    """
    if not r2_configured():
        raise RecordingExportError("R2 is not configured")

    # Fast path: sidecar (or original MP3 key) already in R2 — skip source resolve
    # so legacy http(s) rows don't re-hit VAPI on every export click.
    candidate_mp3 = mp3_sidecar_key(recording_key, tenant_id, vapi_call_id)
    if _r2_object_exists(candidate_mp3):
        return candidate_mp3
    if (
        not recording_key.lower().startswith(("http://", "https://"))
        and recording_key.lower().endswith(".mp3")
        and _r2_object_exists(recording_key)
    ):
        return recording_key

    source_key = _resolve_source_r2_key(tenant_id, vapi_call_id, recording_key)

    # Source is already MP3 (rare VAPI path) — no convert needed.
    if source_key.lower().endswith(".mp3"):
        return source_key

    mp3_key = mp3_sidecar_key(source_key, tenant_id, vapi_call_id)
    if _r2_object_exists(mp3_key):
        return mp3_key

    if not ffmpeg_available():
        raise RecordingExportError("ffmpeg is not installed on this server")

    source_bytes = _download_r2_object(source_key)
    # Re-encode to mono 64k so exports stay small and consistent for WhatsApp,
    # even if the source object is already some form of compressed audio.
    mp3_bytes = convert_audio_to_mp3(source_bytes)
    _upload_r2_object(mp3_key, mp3_bytes, "audio/mpeg")
    log.info(
        "MP3 export sidecar written: tenant=%s call=%s key=%s (%d KB)",
        tenant_id,
        vapi_call_id,
        mp3_key,
        len(mp3_bytes) // 1024,
    )
    return mp3_key


def downloadable_mp3_url(mp3_key: str, filename: str) -> tuple[str, int]:
    """Short-lived presigned GET that forces a named MP3 download.

    Returns (url, expires_in_seconds). Raises RecordingExportError on failure.
    """
    client = _get_client()
    if client is None:
        raise RecordingExportError("R2 is not configured")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "call.mp3"
    if not safe_name.lower().endswith(".mp3"):
        safe_name = f"{safe_name}.mp3"
    try:
        ttl = int(os.environ.get("R2_PRESIGN_TTL_SECONDS", "3600"))
        url = client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": _bucket(),
                "Key": mp3_key,
                "ResponseContentType": "audio/mpeg",
                "ResponseContentDisposition": f'attachment; filename="{safe_name}"',
            },
            ExpiresIn=ttl,
        )
        return url, ttl
    except Exception as e:
        raise RecordingExportError(f"Failed to presign MP3 download: {e}") from e
