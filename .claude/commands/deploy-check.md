---
description: Health-check the live Esmi backend (-5375) and confirm the broken -3446 wasn't disturbed
allowed-tools: Bash(curl *), Bash(railway *), PowerShell(railway *)
---

Run a deployment health check on the **live** Esmi backend.

Key facts (see CLAUDE.md hard rule #3):
- LIVE service = `ai-receptionist-production-5375` (Railway project `awake-nourishment`). This is what the website and VAPI use.
- `ai-receptionist-production-3446` (`aware-nature`) is a KNOWN-BROKEN stale duplicate. Never treat it as live, never try to "fix" it here.
- A push to `main` redeploys BOTH services — only `-5375` matters.

Steps:
1. Liveness: `curl -s https://ai-receptionist-production-5375.up.railway.app/health` — expect a healthy JSON response.
2. Calendar auth: `curl -s https://ai-receptionist-production-5375.up.railway.app/health/calendar` — confirm the step-by-step trace ends successfully (no failing step). Google token issues show here.
3. SendGrid (only if escalation/email code changed): `curl -s https://ai-receptionist-production-5375.up.railway.app/health/sendgrid`.
4. If a deploy just happened: `railway status` / `railway logs` to confirm `-5375` came up clean (not crash-looping).
5. If voice/VAPI changed: remind the user to verify the VAPI assistant's Server URL points at `-5375/voice/tools`, not `-3446`.

Report a concise **PASS/FAIL per check** with the key detail. Do not attempt to repair `-3446`.
