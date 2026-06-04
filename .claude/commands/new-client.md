---
description: Guided runbook to clone Esmi for a new client / tenant
---

Walk the user through standing up a new client instance of Esmi. This is a guided runbook — **confirm each step with the user** and never touch live credentials yourself. See PROJECT_STATUS.md "To Build Another One" for full detail.

In-repo changes (you can do these, confirm values with the user first):
1. `orchelix_knowledge_base/` → swap in the client's KB markdown files. The FAISS index rebuilds automatically on startup (and is cached by content hash).
2. `_SYSTEM_PROMPT` in `agents.py` → client persona, business name, booking flow, language rules.
3. `_PRICING` in `tools.py` AND `orchelix_knowledge_base/13_pricing_tiers.md` → the client's pricing. Keep the dollar amounts in sync between the two (the `pricing-sync` hook checks this; see CLAUDE.md rule #2).
4. `_BUSINESS_TZ` and `_HOURS` in `tools.py` → client timezone and business hours.
5. `escalate_to_human` recipient + booking-notification email in `tools.py`.
6. Voice prompt: mirror the agents.py behavior into the client's VAPI dashboard prompt (booking read-back before booking, escalation triggers, language register).

External setup — the USER must do these (they involve secrets the secret-scan hook will block from entering the repo):
7. Google Calendar: client's own OAuth `credentials.json` → run the OAuth flow → set `GOOGLE_TOKEN_B64` (+ fallbacks) on the new Railway service.
8. SendGrid: verify the client's sender domain; set `SENDGRID_API_KEY`.
9. New Railway service: connect the repo, set ALL secrets as runtime env vars (`OPENAI_API_KEY`, `GOOGLE_TOKEN_B64`, `SENDGRID_API_KEY`, `VAPI_API_KEY`, `VAPI_SERVER_SECRET`, `TWILIO_*`). NEVER bake secrets into the Dockerfile.
10. VAPI: new assistant (GPT-4o + ElevenLabs voice), Server URL → new Railway `/voice/tools`, Server URL Secret matching `VAPI_SERVER_SECRET`, buy a phone number, set up the brand-name pronunciation dictionary.
11. Frontend: update `RAILWAY_API_URL` in `orhelix-website` `app/api/chat/route.ts` to the new service URL.

After in-repo changes, run the `smoke-import` path (edit triggers it) and `/verify-pricing` against the new deploy.
