# platform_api/security.py — auth helpers for control-plane endpoints.
#
# These read env vars at call time (not import time) so tests can monkeypatch
# os.environ, and so platform_api never has to import api.py (which would be a
# circular import — api.py mounts these routers).

from __future__ import annotations

import hmac
import logging
import os

from fastapi import HTTPException, Request

from tenants import normalize_tenant_id, tenant_exists

log = logging.getLogger(__name__)


def verify_vapi_secret(request: Request) -> None:
    """Same contract as api._verify_vapi_secret: fail-closed on X-Vapi-Secret.

    VAPI sends the shared server secret (the "Esmi Production Secret"
    credential attached to each assistant's `server`) in the x-vapi-secret
    header. 503 when VAPI_SERVER_SECRET is unset (misconfiguration must be
    loud, not silently open), 401 on mismatch.
    LOCAL DEV ONLY bypass: ALLOW_UNAUTHENTICATED_VOICE=1.
    """
    secret = os.environ.get("VAPI_SERVER_SECRET")
    if not secret:
        if os.environ.get("ALLOW_UNAUTHENTICATED_VOICE") == "1":
            log.warning(
                "ALLOW_UNAUTHENTICATED_VOICE=1 — /webhooks/vapi is "
                "UNAUTHENTICATED. Never use this in production."
            )
            return
        log.error(
            "VAPI_SERVER_SECRET is not set — refusing /webhooks/vapi request."
        )
        raise HTTPException(status_code=503, detail="Webhook not configured.")
    provided = request.headers.get("x-vapi-secret", "")
    if not hmac.compare_digest(provided, secret):
        log.warning("Rejected /webhooks/vapi: bad or missing X-Vapi-Secret header.")
        raise HTTPException(status_code=401, detail="Unauthorized")


def verify_platform_secret(request: Request) -> None:
    """Gate /platform/* reads on X-Platform-Secret vs PLATFORM_API_SECRET.

    Fail-closed (503 when unset): /platform/* serves call transcripts and
    caller phone numbers, so unlike the fail-open booking secret there is no
    acceptable unauthenticated mode. Replaced by real per-user auth (Clerk)
    in a later phase.
    """
    secret = os.environ.get("PLATFORM_API_SECRET")
    if not secret:
        log.error("PLATFORM_API_SECRET is not set — refusing /platform request.")
        raise HTTPException(
            status_code=503,
            detail="Platform API not configured — set PLATFORM_API_SECRET.",
        )
    provided = request.headers.get("X-Platform-Secret", "")
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


def verify_platform_admin_secret(request: Request) -> None:
    """Gate /platform/admin/* on X-Platform-Admin-Secret vs PLATFORM_ADMIN_SECRET.

    Deliberately a DIFFERENT secret from PLATFORM_API_SECRET (verify_platform_secret
    above), not a shared one — a leaked client-facing platform secret must never
    unlock admin actions (assigning any tenant's plan). Fail-closed when unset,
    same as verify_platform_secret. Only the Next.js server holds this value
    (never the browser); the frontend page itself is additionally gated on the
    caller's active Clerk org being "default" (Orchelix's own org).
    """
    secret = os.environ.get("PLATFORM_ADMIN_SECRET")
    if not secret:
        log.error("PLATFORM_ADMIN_SECRET is not set — refusing /platform/admin request.")
        raise HTTPException(
            status_code=503,
            detail="Admin API not configured — set PLATFORM_ADMIN_SECRET.",
        )
    provided = request.headers.get("X-Platform-Admin-Secret", "")
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_tenant(request: Request) -> str:
    """Strict tenant resolution for /platform/*: X-Tenant-Id must name a real
    tenant. No default fallback — a dashboard bug must surface as 400, never
    silently show another tenant's (or Orchelix's) data."""
    raw = request.headers.get("X-Tenant-Id")
    if not raw or not str(raw).strip():
        log.warning(
            "Rejected %s %s: X-Tenant-Id header missing.",
            request.method, request.url.path,
        )
        raise HTTPException(status_code=400, detail="X-Tenant-Id header is required")
    tid = normalize_tenant_id(raw)
    if (tid == "default" and raw.strip().lower() != "default") or not tenant_exists(tid):
        # normalize_tenant_id silently maps invalid ids to 'default' — for the
        # platform API that silent fallback would be a data-isolation bug.
        # Logged so a rejected slug (e.g. an org whose Clerk slug isn't a
        # registered tenant) is visible in Railway logs instead of only ever
        # showing up as an unexplained 400 in the dashboard.
        log.warning(
            "Rejected %s %s: unknown tenant %r.",
            request.method, request.url.path, raw,
        )
        raise HTTPException(status_code=400, detail=f"Unknown tenant '{raw}'")
    return tid
