---
description: Health-check the live Esmi backend (-5375) and confirm the broken -3446 wasn't disturbed
allowed-tools: Bash(curl *), Bash(railway *), Bash(python *), PowerShell(railway *)
---

Run a deployment health check on the **live** Esmi backend.

Key facts (see CLAUDE.md hard rule #3):
- LIVE service = `ai-receptionist-production-5375` (Railway project `awake-nourishment`). This is what the website and VAPI use.
- `ai-receptionist-production-3446` (`aware-nature`) is a KNOWN-BROKEN stale duplicate. Never treat it as live, never try to "fix" it here.
- A push to `main` redeploys BOTH services — only `-5375` matters.
- `/health/calendar`, `/health/env`, `/health/sendgrid` are **gated** — they require `X-Vapi-Secret: $VAPI_SERVER_SECRET` and return 401 if unauthenticated. Use the `$VAPI_SERVER_SECRET` Railway env var to hit them; never hard-code it here.

Steps:
1. **Liveness:** `curl -s https://ai-receptionist-production-5375.up.railway.app/health` — expect `{"status":"ok","agent":"esmi"}`. This endpoint is public and unauth.

2. **Pricing + streaming** (the real smoke test — no auth needed, hits `/chat`):
   POST a pricing question to `/chat` and assert the six canonical amounts appear in the reply via `token` events. Use `/verify-pricing` for this.

3. **Voice auth:** confirm `/voice/tools` WITHOUT a secret returns `401` (not `200`):
   ```bash
   curl -s -o /dev/null -w "%{http_code}" -X POST \
     https://ai-receptionist-production-5375.up.railway.app/voice/tools \
     -H "Content-Type: application/json" \
     -d '{"message":{"type":"tool-calls","toolCallList":[{"id":"probe","function":{"name":"get_current_date","arguments":"{}"}}]}}'
   ```
   Expect `401`. If `200`, `VAPI_SERVER_SECRET` is not set — that's a security gap (see PROJECT_STATUS Security section).

4. **Deploy status:** `railway status` / `railway logs` to confirm `-5375` came up clean (not crash-looping), and that the `VAPI_SERVER_SECRET is not set` warning is absent from logs.

5. **If voice/VAPI changed:** remind the user to verify the VAPI assistant's Server URL points at `-5375/voice/tools`, not `-3446`, and that the VAPI Server URL Secret matches `VAPI_SERVER_SECRET`.

Report a concise **PASS/FAIL per check** with the key detail. Do not attempt to repair `-3446`.
