# Project Status — Orchelix AI Receptionist (Esmi)

## What Was Built

A production AI receptionist system with two repos:

| Repo | Role |
|---|---|
| `ai-receptionist` | Python backend — LangGraph agent, FastAPI SSE endpoint, Google Calendar, FAISS KB |
| `orhelix-website` | Next.js frontend — chat UI at `/try-esmi`, API proxy to Railway |
| `Otro Nivel Barbershop/website` | Client site — online booking wizard → Esmi `/bookings/*` |

### Multi-location booking (2026-07)

- **Tenant model** supports optional `locations` + `services` maps (`tenants.py`).
  - Per location: `calendar_id`, `business_hours` / `day_hours`, `business_days`, **`booking_days`** (appointments ≠ open; e.g. Sat walk-in only).
  - Per service: `duration_min`, `price` + `price_by_location`.
  - Single-location tenants unchanged (synthesized default location from legacy fields).
- **Shared calendar core** in `tools.py`: `compute_available_slots`, `book_appointment_core` used by voice tools and REST.
- **REST (website)**: `GET /bookings/availability`, `POST /bookings`, `GET /bookings/lookup` — auth via `X-Booking-Secret` + `X-Tenant-Id`.
- **Tenant `otro-nivel`**: Weston + Keele, bilingual prompt/KB, SMS templates EN/ES. Runbook: `tenants/otro-nivel/ONBOARDING.md`.

**Still needs client/ops (not code):** client call-forwarding of (647) 340-7187 → the VAPI Twilio number +1 437 292 3949 (native porting optional, later). Done as of 2026-07-18: Google calendars + per-tenant Google/Twilio/SendGrid secrets on Railway, `BOOKING_API_SECRET`, VAPI assistants + numbers for otro-nivel and coastline-condos (IDs in each tenant's `config.json`).

---

## Architecture

**Chat (web):**
```
Browser
  └── /try-esmi (Next.js page)
        └── EsmiChat.tsx  ← "use client" React component
              └── POST /api/chat  ← Next.js API route (server-side proxy)
                    └── POST /chat  ← Railway FastAPI endpoint (SSE)
                          └── LangGraph ReAct agent (8 tools)
                                ├── search_knowledge_base (FAISS vector store)
                                ├── get_pricing (canonical exact pricing)
                                ├── list_available_slots (Google Calendar freebusy)
                                ├── book_appointment (Google Calendar insert)
                                ├── find_booking / reschedule_appointment / cancel_appointment
                                └── escalate_to_human (SendGrid email alert)
```

**Voice (phone) — one VAPI assistant + number per tenant:**
```
Caller
  └── VAPI phone number ─ 561-566-1066 → Orchelix_Esmi        (tenant: default)
                        ─ 437-292-3949 → Otro_Nivel_Esmi      (tenant: otro-nivel)
                        ─ 754-799-2655 → Coastline_Condos     (tenant: coastline-condos)
        └── VAPI AI engine (ElevenLabs voice, Deepgram flux EN/ES, GPT-4.1)
              └── POST /voice/tools  ← Railway FastAPI (sync tool execution)
                    │     resolve_vapi_tenant(): assistantId/phoneNumberId → tenant_id
                    ├── get_pricing               → canonical exact pricing (per tenant)
                    ├── search_knowledge_base     → FAISS KB (per tenant)
                    ├── list_available_slots      → Google Calendar freebusy (+location/service)
                    ├── book_appointment          → Calendar insert (phone # in description, SMS confirm)
                    ├── find_booking              → look up caller's upcoming bookings
                    ├── request_cancellation_code → 6-digit code to contact on file
                    ├── reschedule_appointment    → move a booking (needs code)
                    ├── cancel_appointment        → cancel a booking (needs code)
                    ├── escalate_to_human         → SendGrid email to tenant's team
                    └── transferCall              → VAPI built-in → tenant's transfer_phone
```

**Streaming protocol:** Server-Sent Events (SSE) over `text/event-stream`. Each SSE line is `data: {json}\n\n`.

**Event types:**
| Type | Payload | Meaning |
|---|---|---|
| `token` | `content: string` | Partial LLM response |
| `tool_start` | `tool: string` | Agent calling a tool |
| `tool_end` | — | Tool returned |
| `done` | `full_text`, `slots?`, `date_label?` | Stream finished |
| `error` | `message: string` | Something failed |

---

## Backend — `ai-receptionist`

### Key Files

| File | Purpose |
|---|---|
| `api.py` | FastAPI app. `POST /chat` streams SSE. `POST /voice/tools` for VAPI. Rate-limited. Diagnostic endpoints. |
| `agents.py` | Esmi persona prompt + `create_react_agent` with 8 tools. Model `gpt-4o`, temp 0. |
| `graph.py` | `StateGraph` wrapping the agent. Uses `PostgresSaver` (Railway DB) or `MemorySaver` fallback. |
| `tools.py` | `search_knowledge_base`, `get_pricing`, `list_available_slots`, `book_appointment`, `find_booking`, `reschedule_appointment`, `cancel_appointment`, `escalate_to_human`. |
| `state.py` | `AgentState` TypedDict. |
| `observability.py` | LangSmith tracing init. |
| `Dockerfile` | `python:3.11-slim`, uvicorn CMD. **No secrets baked in** — all credentials are read from Railway runtime env vars (see Deployment). |
| `railway.toml` | `builder = "DOCKERFILE"`. |
| `orchelix_knowledge_base/` | 14 markdown files — Esmi's knowledge source. |

### Knowledge Base

FAISS vector index built automatically on startup from `orchelix_knowledge_base/*.md`
(15 files — 14 numbered knowledge docs + a README).
Index is cached in `.kb_index/` with a content-hash sidecar — rebuilds only when files change.

Key KB files (highest retrieval priority):
- `13_pricing_tiers.md` — per-agent pricing (one-time setup + monthly managed service)
- `03_services.md` — Esmi, Revenue-Ops, Finance OS descriptions
- `07_faq.md` — Common Q&As
- `06_how_we_work.md` — 14-day deployment process
- `00_company_overview.md` — Company identity

### Agent Prompt Rules (agents.py)
- Formatting: no markdown headers, no bold, use `-` for bullet points
- Booking flow: ask day → show slots → collect name+email → book
- Pricing: always call `get_pricing` (exact, authoritative numbers). Services/FAQs: `search_knowledge_base`. Never answer either from memory
- Lead capture: after answering pricing/services, offer a calendar check once per conversation
- Escalation: call `escalate_to_human` if KB search fails twice, or user signals urgency/budget readiness
- Model: `gpt-4o`, temperature 0

### Tools

| Tool | Trigger | Action |
|---|---|---|
| `search_knowledge_base` | Questions about services, FAQs, company info (NOT prices) | FAISS semantic search over KB |
| `get_pricing` | Any pricing/cost/setup/monthly question | Returns canonical exact pricing from `_PRICING` |
| `list_available_slots` | User gives a preferred day | Google Calendar freebusy, 9–5 Mon–Fri |
| `book_appointment` | Name + email + slot confirmed | Google Calendar insert, idempotent via SHA256 event ID |
| `find_booking` | User wants to change/cancel an existing booking | Look up upcoming bookings by email/phone; returns event ids |
| `reschedule_appointment` | New time confirmed for an existing booking | Move the booking (event id from `find_booking`) |
| `cancel_appointment` | User confirms which booking to cancel | Cancel the booking (event id from `find_booking`) |
| `escalate_to_human` | KB fails twice, or urgency/budget/frustration detected | SendGrid email to `jquinonez2980@gmail.com` |

### Rate Limiting
`slowapi` limits `POST /chat` to 10 requests/minute per IP. No Redis needed — in-memory per process.

### Streaming — LangGraph 1.1.10
With `create_react_agent` wrapped in a `StateGraph`, `on_chat_model_stream` events from the inner
graph surface through `astream_events(version="v2", include_subgraphs=True)`. The `on_chain_end`
fallback is still in place for resilience if token events are missed.

---

## Frontend — `orhelix-website`

### Key Files

| File | Purpose |
|---|---|
| `app/try-esmi/EsmiChat.tsx` | Full chat UI — `"use client"` component |
| `app/try-esmi/page.tsx` | Server component page wrapper |
| `app/api/chat/route.ts` | Next.js server-side proxy to Railway (keeps Railway URL secret) |
| `app/globals.css` | `@keyframes esmi-spin` for loading spinner |

### EsmiChat.tsx Features
- SSE streaming with buffer accumulation (`ReadableStream` + `TextDecoder`)
- Message bubbles (user navy, assistant white)
- Typing indicator (animated dots) while tools run
- Slot picker cards for time slots (instead of bullet list)
- Quick reply chips (shown only before first user message)
- Thread ID persistence via `localStorage("esmi-thread-id")`
- **Message persistence** via `localStorage("esmi-messages-{threadId}")` — restores on reload, capped at 30 messages, cleared on New Chat
- Reset conversation button
- **EN/ES language toggle** — click EN or ES in the chat header to switch language; resets welcome message and quick replies; agent auto-detects and responds in the selected language using Latin American Spanish (not Castilian)

### Slot Flash Fix
During streaming, slot bullet lines are stripped client-side via `stripSlotLines()` so
they never appear as text. Cards render cleanly when the `done` event arrives.

### Design System
Tailwind v4 with `@theme` block in `globals.css`:
- `--color-navy-*`: `#0A2540` family
- `--color-teal-*`: `#00D4EE` family

---

## Deployment — Railway

**⚠ Two Railway services deploy from this same repo (`jquinonez2980-sudo/ai-receptionist`).
Both auto-deploy on push to `main`.**

| Service / Project | URL | Status | Used by |
|---|---|---|---|
| `ai-receptionist` / **`awake-nourishment`** | `https://ai-receptionist-production-5375.up.railway.app` | **LIVE** | The website (`/api/chat` proxy) — this is the real Esmi backend |
| `ai-receptionist` / `aware-nature` | `https://ai-receptionist-production-3446.up.railway.app` | **BROKEN** (crash-loops on a stale Streamlit dashboard Start Command; `OPEN_API_KEY` typo; missing VAPI/Twilio vars) | Nothing customer-facing — stale duplicate. Either retire it or fix its Start Command + env. |

Because both services watch `main`, a single push redeploys both. The live one is **`-5375`**.
Verify the VAPI assistant's Server URL points at the live service, not the broken `-3446`.

**Build:** Dockerfile (python:3.11-slim → pip install → uvicorn)

**Environment Variables (set on the Railway service — NOT baked into the image):**
| Variable | Notes |
|---|---|
| `OPENAI_API_KEY` | GPT-4o + embeddings. **Must be exactly this name** (the OpenAI SDK/LangChain reads it). The `aware-nature` service has it mis-typed as `OPEN_API_KEY`. |
| `LANGCHAIN_PROJECT` | LangSmith project name |
| `DATABASE_URL` | Railway PostgreSQL plugin — thread persistence (auto-set by Railway) |
| `GOOGLE_TOKEN_B64` | Base64-encoded Google OAuth JSON (see below). Fallbacks: `GOOGLE_REFRESH_TOKEN`, `GOOGLE_TOKEN_JSON` |
| `SENDGRID_API_KEY` | SendGrid key (or `SENDGRID_API_KEY_B64`) |
| `VAPI_API_KEY` | VAPI private key (or `VAPI_API_KEY_B64`) |
| `VAPI_SERVER_SECRET` | Shared secret for `/voice/tools` webhook auth — set the same value in the VAPI assistant's Server URL Secret. If unset, voice endpoints are UNAUTHENTICATED. |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_SMS_FROM` | Voice booking SMS confirmation. If unset, the confirmation SMS is skipped. |

**Secrets are runtime env vars only — never baked into the Docker image.** (They were previously
baked into the Dockerfile as base64 `ENV` lines; that was removed for security. Those old values
remain in git history and are compromised — rotate them.)

---

## Google Calendar Setup

1. Create a Google Cloud project → enable Calendar API
2. Create OAuth 2.0 credentials (Desktop app type) → download as `credentials.json`
3. Run the OAuth flow locally to generate a fresh token:

```bash
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
import json, base64
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', ['https://www.googleapis.com/auth/calendar'])
creds = flow.run_local_server(port=0)
data = {
  'token': creds.token,
  'refresh_token': creds.refresh_token,
  'token_uri': creds.token_uri,
  'client_id': creds.client_id,
  'client_secret': creds.client_secret,
  'scopes': list(creds.scopes),
  'universe_domain': 'googleapis.com',
  'expiry': creds.expiry.isoformat() if creds.expiry else None,
}
print('TOKEN_B64=' + base64.b64encode(json.dumps(data).encode()).decode())
"
```

4. Update Railway environment variables:
   - `GOOGLE_TOKEN_B64` — base64-encoded full JSON (checked first by `tools.py`)
   - `GOOGLE_REFRESH_TOKEN` — just the refresh token string (fallback method)
   - `GOOGLE_TOKEN_JSON` — same full JSON as plain string (fallback method)

The `refresh_token` auto-renews the expired access token on each API call.

**Note:** The Google Cloud OAuth app is **published** (not in Testing mode), so the `refresh_token` does **not** expire. If it ever reverts to Testing mode, refresh tokens would expire after **7 days** — in that case re-run the OAuth flow above and update all three Railway vars.

---

## SendGrid Setup

1. Create a SendGrid account → Settings → API Keys → Create API key (Mail Send permission only)
2. Verify your sender email (Settings → Sender Authentication → verify `info@orchelix.com`)
3. Base64-encode the key:

```python
import base64
print(base64.b64encode(b"SG.your-key-here").decode())
```

4. Set it as a Railway env var: `SENDGRID_API_KEY` (plain) or `SENDGRID_API_KEY_B64` (base64). **Do not bake it into the Dockerfile.**

`_get_sendgrid_key()` in `tools.py` checks `SENDGRID_API_KEY` (plain) first, then
falls back to base64-decoding `SENDGRID_API_KEY_B64`. Both email functions use this helper.

Escalation emails go to `jquinonez2980@gmail.com`. Booking notifications go to `info@orchelix.com`.

---

## VAPI Voice Setup

One assistant + one phone number per tenant, all in the single Orchelix VAPI org, all
pointing at the same live webhook. `resolve_vapi_tenant()` (tenants.py) maps the
`assistantId` / `phoneNumberId` in each webhook payload to a tenant via the `vapi` block
in `tenants/<id>/config.json`.

| Assistant | ID | Number | Tenant | transferCall → |
|---|---|---|---|---|
| `Orchelix_Esmi` | `d5e020bf-0235-4214-a57f-de30e8072b0b` | 561-566-1066 (vapi) | `default` | Jorge +1 416 771 0667 |
| `Otro_Nivel_Esmi` | `32994d60-3712-4183-a7db-edc3badeabec` | 437-292-3949 (Twilio CA) | `otro-nivel` | Owner 647-569-1194 |
| `Coastline_Condos` | `a351deb6-bf22-4cda-a3f3-67bca8ac6346` | 754-799-2655 (Twilio) | `coastline-condos` | Sales +593 96 994 3941 |

Spare unassigned VAPI number: +1 786 927 9212.

**All three:** GPT-4.1, ElevenLabs voice, Deepgram flux transcriber (EN+ES).
**Server URL:** must point at the **live** service → `https://ai-receptionist-production-5375.up.railway.app/voice/tools`.
**Webhook auth** (backend is fail-closed — 503 unset / 401 mismatch):
- every function tool sends `X-Vapi-Secret: <VAPI_SERVER_SECRET>` via `tool.server.headers`;
- each assistant's `server` additionally references the VAPI credential **"Esmi Production Secret"** (`b73329e6-…`).
- Note: VAPI **GET responses redact secrets** — an API read showing no server secret does NOT mean auth is unset.

### Tools (standalone, attached per assistant via `model.toolIds`)

Tools are org-level standalone objects; each tenant assistant gets its own set (10 each
for otro-nivel and coastline-condos). Full parameter reference:
`sales/INTEGRATIONS_SETUP_MANUAL.md` Part D. Tenant specifics:
- **otro-nivel** tools carry `location` (`weston`/`keele`) and `service` enums;
  `find_booking`/`reschedule_appointment` also accept `""` (searches/falls back across both shops).
- **coastline-condos** is single-location: no `location`/`service` params; 30-min tour slots.
- **Orchelix legacy:** the `default` assistant still runs an older 4-tool set
  (`search_knowledge_base`, `list_available_slots`, `book_appointment`, `transferCall`);
  its `list_available_slots` has a known-quirky `"service "` (trailing space) param.
  Orphan duplicate tools from early dashboard experiments exist in the org and are unused.

### Voice booking differences from chat
- Asks for **name only** — no email (STT can't reliably transcribe email addresses)
- Caller's phone number is injected automatically via `{{call.customer.number}}` (as `caller_phone` or `attendee_email`); the backend prefers the real caller number, stores it in the event description, and sends an SMS confirmation
- Today's date: tenant prompts use the VAPI liquid variable `{{ "now" | date: "%A, %B %d, %Y", "<business_tz>" }}` (Toronto for otro-nivel, Guayaquil for coastline); the live Orchelix prompt instead calls the `get_current_date` backend tool at call start
- Slot options are read conversationally (not as a bullet list)
- Hot leads flagged in the `summary` field (e.g. "Intro Call — Jorge 🔥 HOT LEAD")
- Cancel/reschedule requires the 6-digit `request_cancellation_code` flow (code sent to the contact on file, read back by the caller)

### VAPI system prompts — where they live

The **authoritative copy of every voice prompt is the VAPI dashboard**. Each is mirrored
in the repo (synced 2026-07-18) — edit the dashboard, then re-sync the mirror:

| Assistant | Repo mirror |
|---|---|
| `Orchelix_Esmi` | `prompts/vapi_system.md` |
| `Otro_Nivel_Esmi` | `tenants/otro-nivel/prompts/vapi_voice.md` |
| `Coastline_Condos` | `tenants/coastline-condos/prompts/vapi_voice.md` |

Keep each voice prompt behaviorally consistent with the tenant's chat prompt
(`prompts/esmi_system.md` / `tenants/<id>/prompts/system.md`): booking read-back before
`book_appointment`, escalation triggers, LATAM Spanish, never quote Orchelix pricing.

### Pronunciation fix
"Orchelix" is pronounced "or-kee-lix". Configured via ElevenLabs Pronunciation Dictionary
(Alias entry: `Orchelix` → `or kee lix`) linked to the VAPI assistant's voice settings.

### To set up VAPI for a new client
Everything below can be done via the VAPI REST API (preferred — repeatable) or the
dashboard. Full step-by-step: `sales/INTEGRATIONS_SETUP_MANUAL.md` Part D.

1. Create assistant in the shared Orchelix VAPI org: GPT-4.1, ElevenLabs voice, Deepgram
   flux EN/ES transcriber, server URL → live `/voice/tools`, server credential
   "Esmi Production Secret".
2. Create the standard 9 function tools (+ `transferCall` → client's `transfer_phone`)
   with `X-Vapi-Secret` in each tool's `server.headers`; attach via `model.toolIds`.
   Copy schemas from an existing tenant's tools (otro-nivel = multi-location template,
   coastline-condos = single-location template).
3. Voice prompt: start from `tenants/<id>/prompts/vapi_voice.md` of a similar tenant;
   include the `{{ "now" | date: ..., "<business_tz>" }}` line, booking read-back, LATAM
   Spanish rules. Paste into dashboard, mirror into `tenants/<id>/prompts/vapi_voice.md`.
4. Phone number: US → buy in VAPI; Canada/other → buy in Twilio, import to VAPI.
   Attach to the assistant.
5. Write real assistant/phone IDs into `tenants/<id>/config.json` → `vapi.*`, push, and
   verify tenant routing (see below).
6. ElevenLabs Pronunciation Dictionary for the brand name if TTS mangles it.

**API access gotchas (learned 2026-07-18):**
- Run scripts with `railway run python <script>` so `VAPI_API_KEY` / `VAPI_SERVER_SECRET`
  come from Railway env without echoing values.
- Send a real User-Agent (e.g. `curl/8.9.1`) — Python urllib's default gets Cloudflare
  **403 "error code: 1010"**, which masquerades as a bad API key.
- VAPI GET responses redact secrets — don't conclude auth is missing from a read.
- VAPI's `/chat` API needs a card on file (402); e2e-test instead by POSTing a simulated
  `tool-calls` payload (real `assistantId`, `x-vapi-secret` header) to live `/voice/tools`
  and checking the right tenant's prices/slots come back.

---

## Diagnostic Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness check |
| `GET /health/calendar` | Step-by-step Google Calendar auth trace |
| `GET /health/env` | All env var names visible in container |
| `GET /health/sendgrid` | Sends a test email to confirm SendGrid works end-to-end |

---

## Pricing (current)

Canonical source: `_PRICING` in `tools.py` (returned by the `get_pricing` tool).
Keep in sync with `orchelix_knowledge_base/13_pricing_tiers.md`.

| Package | One-time Setup | Monthly Managed Service |
|---|---|---|
| Esmi — AI Virtual Receptionist & Lead Qualification ★ | from $8,500 | from $1,099/mo |
| Revenue Operations Agents (AI Sales & Lead Management) | from $9,500 | from $1,299/mo |
| Firm OS — Custom Multi-Agent Operations System | from $24,000 | from $2,499/mo |

Pricing model: one-time setup fee + monthly managed service (monitoring, optimization,
updates, support). No long-term contract on the monthly service.

---

## To Build Another One

1. **Clone `ai-receptionist`** and replace:
   - `orchelix_knowledge_base/` with your client's KB files
   - The system prompt in `agents.py` (persona, business name, booking flow)
   - `_BUSINESS_TZ` and `_HOURS` in `tools.py` for timezone/hours
   - Escalation email recipient in `escalate_to_human` (`tools.py`)

2. **Google Calendar:** Follow setup steps above. Each client needs their own OAuth credentials.

3. **SendGrid:** Follow setup steps above. Verify the sender domain for each client.

4. **Railway:** Create a new service, connect the repo, and set ALL secrets as Railway env vars
   (`OPENAI_API_KEY`, `GOOGLE_TOKEN_B64`, `SENDGRID_API_KEY`, `VAPI_API_KEY`, `VAPI_SERVER_SECRET`,
   `TWILIO_*`). Never bake secrets into the Dockerfile.

5. **Frontend:** The `EsmiChat.tsx` + `api/chat/route.ts` work as-is. Update `RAILWAY_API_URL` in `route.ts`.

6. **Knowledge base:** Update the markdown files in `orchelix_knowledge_base/`. The FAISS index rebuilds automatically on startup.

7. **Voice:** Follow VAPI setup steps above. Update the system prompt in the VAPI dashboard with the new business name and escalation transfer number.

---

## Known Issues / Limitations

- `MemorySaver` fallback loses conversation history on Railway restart — `DATABASE_URL` must be set for persistence
- FAISS index rebuild calls OpenAI embeddings API — costs a small amount on each deploy if KB files changed
- Slot stripping regex is fragile — if LLM changes time format, slots may not be stripped during streaming
- Rate limiter is in-memory per process — resets on Railway restart, not shared across multiple instances
- VAPI rate limits apply per account — monitor call volume on the VAPI dashboard
- Voice bookings store phone number in the `attendee_email` field of Google Calendar (cosmetic only)
- ElevenLabs Pronunciation Dictionary must be manually updated when new brand terms are added
## Security

### All Closed (June 2026)
- **`VAPI_SERVER_SECRET` enforced** — `/voice/tools` fail-closed (503 unset, 401 mismatch). Local dev: `ALLOW_UNAUTHENTICATED_VOICE=1`.
- **Secrets removed from Dockerfile** — now Railway runtime env vars only. `secret-scan` hook blocks re-introduction.
- **All credentials rotated (June 2026)** — Google OAuth, SendGrid, VAPI, OpenAI, Twilio all rotated; new keys in Railway only.
- **Git history scrubbed (June 2026)** — `git filter-repo` removed secret files and redacted base64 blobs across all commits. Force-pushed to GitHub.
- **`orhelix-esmi.txt` and `.railway_tmp.env` deleted** from working tree.
- **Railway service `-3446` (`aware-nature`) deleted** — was a broken empty duplicate.
- **Per-tenant calendar isolation** — fail-closed for non-default tenants; never falls back to Orchelix's calendar.
- **HTML escaping in emails** — `html.escape()` on all user/LLM content in SendGrid HTML bodies.
- **`/chat` hardened** — `max_length=4000` on messages; `X-Chat-Secret` enforced; CORS locked to `ALLOWED_ORIGINS`.
- **CI pipeline** — GitHub Actions runs smoke imports + unit tests + ruff on every push.

## Completed Improvements (May 2026)

| # | Improvement | Files Changed |
|---|---|---|
| 1 | Model upgraded `gpt-4o-mini` → `gpt-4o` | `agents.py` |
| 2 | Rate limiting — `slowapi` 10 req/min/IP | `api.py`, `requirements.txt` |
| 3 | Streaming fix — `include_subgraphs=True` surfaces inner agent tokens | `api.py` |
| 4 | Message persistence across page reloads via localStorage | `EsmiChat.tsx` |
| 5 | Human escalation — `escalate_to_human` tool + SendGrid email | `tools.py`, `agents.py`, `Dockerfile` |
| 6 | Proactive lead capture — prompt rule after pricing/services answers | `agents.py` |
| 7 | Phone/voice — VAPI.ai integration, ElevenLabs Bella, 561-566-1066 | `api.py`, `Dockerfile`, VAPI dashboard |
| 8 | Spanish support — EN/ES toggle in chat UI, agent responds in detected language | `EsmiChat.tsx`, `agents.py` |
| 9 | Latin American Spanish — agent uses LATAM vocabulary/register, never Castilian | `agents.py`, `EsmiChat.tsx` |
| 10 | Google OAuth token refresh — re-ran OAuth, updated all 3 Railway vars (`GOOGLE_TOKEN_B64`, `GOOGLE_TOKEN_JSON`, `GOOGLE_REFRESH_TOKEN`); fixed voice `book_appointment` bug (was using `caller_phone` key instead of `attendee_email`); fixed `/health/calendar` to test correct credential source | `api.py`, Railway vars |
| 11 | Voice booking UX fix — VAPI system prompt updated to ask for name only (not email); caller's phone auto-injected via `{{call.customer.number}}`; slots read conversationally | VAPI dashboard |
| 12 | Pricing bug fix — reconciled the KB to the canonical per-agent model (removed the abandoned Pilot/Growth/Scale/Enterprise tiered-subscription wording that made Esmi quote wrong Revenue-Ops pricing) | `07_faq.md`, `06_how_we_work.md`, `00_company_overview.md` |
| 13 | Revenue-Ops naming fix — renamed the `_PRICING` label "AI Sales & Lead Management Assistant" → "Revenue Operations Agents (AI Sales & Lead Management)" so `get_pricing` resolves when asked by that name (verified live) | `tools.py` |
| 14 | Secret-handling hardening — removed base64-baked secrets from the Dockerfile (now runtime env vars); git-ignored `orhelix-esmi.txt` and `.claude/` | `Dockerfile`, `.gitignore` |
| 15 | Claude Code hardening — `CLAUDE.md`, `.claude/settings.json` (deny-list + curated allowlist), `secret-scan` PreToolUse hook, `pricing-sync` + `smoke-import` PostToolUse hooks, `/deploy-check` `/verify-pricing` `/new-client` `/run-evals` slash commands | `.claude/` |
| 16 | Versioned system prompt — extracted from `agents.py` inline string to `prompts/esmi_system.md`; `{today}` resolved per-request via `.replace` | `agents.py`, `prompts/esmi_system.md` |
| 17 | Behavioral eval harness — 10 pytest invariants (pricing routing, booking flow, reschedule/cancel, KB-miss escalation, LATAM Spanish, SSE streaming); all green vs live gpt-4o | `evals/` |
| 18 | Dropped-lead escalation bug fixed — agent was saying "someone will follow up" without calling `escalate_to_human` on KB misses (silent dropped leads). Prompt now requires the tool call before promising follow-up | `prompts/esmi_system.md` |
| 19 | `create_react_agent` → `create_agent` migration — deprecated LangGraph API replaced with `langchain.agents.create_agent` + `dynamic_prompt` middleware; all evals + streaming verified live | `agents.py`, `evals/harness.py` |
| 20 | Voice webhook secured — `VAPI_SERVER_SECRET` set on Railway; `_verify_vapi_secret` hardened to fail-closed (503 when unset, 401 on mismatch); was silently open (200) before | `api.py`, Railway env vars |
