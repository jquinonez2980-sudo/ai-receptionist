# rate_limit.py — the shared slowapi Limiter instance + IP-key resolution.
#
# Extracted out of api.py so platform_api/* routes (e.g.
# platform_api/public_voice_preview.py) can rate-limit against the exact
# same Limiter every api.py route (/chat, /health/deep) already uses,
# without a circular import: api.py imports platform_api's routers, so a
# platform_api module importing the limiter FROM api.py creates a cycle
# whose success depends on import order — it happens to work when something
# imports `api` first (limiter is defined before api.py reaches its own
# `from platform_api import ...` line, so the partial module already has
# it), but breaks the moment anything imports a platform_api module
# directly first (`import platform_api.public_voice_preview`, which every
# evals/test_*.py file style does — api.py hasn't run yet, so THAT import
# tries to pull platform_api routers out of a platform_api package that
# hasn't finished defining them). Importing from this leaf module instead
# has no direction dependency either way.

from __future__ import annotations

import hmac
import os

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

# Same env var api.py's own _verify_chat_secret() reads independently — one
# canonical name, read in two places rather than one module importing the
# other's copy of it (which would reintroduce the exact cycle this file
# exists to avoid).
CHAT_PROXY_SECRET = os.environ.get("CHAT_PROXY_SECRET")


def _rate_limit_key(request: Request) -> str:
    """Key the limiter on the real visitor, not a proxy's own egress IP.

    All traffic through orhelix-website's server-to-server proxies (e.g.
    app/api/chat/route.ts, app/api/public/voice/preview/route.ts) arrives
    with request.client.host equal to the proxy's own IP for every visitor
    — keying on it either throttles all visitors as one shared bucket or
    doesn't rate-limit anything meaningfully. The proxy forwards the real
    visitor IP in X-Client-IP; only trust it when the request also carries
    the correct X-Chat-Secret, so an unauthenticated caller can't spoof the
    header to manipulate someone else's bucket.
    """
    provided_secret = request.headers.get("X-Chat-Secret", "")
    if CHAT_PROXY_SECRET and hmac.compare_digest(provided_secret, CHAT_PROXY_SECRET):
        forwarded = request.headers.get("X-Client-IP", "").strip()
        if forwarded:
            return forwarded
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)
