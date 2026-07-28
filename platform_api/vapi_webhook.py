# platform_api/vapi_webhook.py — POST /webhooks/vapi (end-of-call reports).
#
# Ops note: each VAPI assistant must have this URL configured as its server
# URL with serverMessages including "end-of-call-report" (the assistants
# already carry the "Esmi Production Secret" credential, which arrives here
# as x-vapi-secret). Until that VAPI-side change is made, nothing calls this.

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request

from platform_api.call_log import record_end_of_call
from platform_api.security import verify_vapi_secret

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/vapi")
async def vapi_webhook(request: Request) -> dict:
    """Ingest VAPI server messages; persist end-of-call reports to `calls`.

    Resilience contract: once authenticated, this endpoint ALWAYS returns 200.
    A processing bug must not make VAPI retry-storm us or mark the org's
    webhooks unhealthy — errors are logged (with traceback) for Railway, and
    the call remains recoverable from VAPI's call-history API. Auth failures
    still 401/503: an unauthenticated caller learns nothing, and a
    misconfigured secret must be loud, not silently swallowed.
    """
    verify_vapi_secret(request)

    try:
        payload = json.loads((await request.body()) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except Exception as e:
        log.warning("VAPI webhook: unparseable body (%s) — acknowledged anyway.", e)
        return {"received": True}

    try:
        # record_end_of_call is blocking (sync SQLAlchemy) — keep it off the
        # event loop that /chat SSE streams and /voice/tools run on.
        result = await asyncio.to_thread(record_end_of_call, payload)
    except Exception:
        log.exception("VAPI webhook: processing failed — acknowledged anyway.")
        return {"received": True}

    return {"received": True, **({"call": result} if result else {})}
