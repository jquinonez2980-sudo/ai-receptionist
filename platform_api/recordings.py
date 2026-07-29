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
