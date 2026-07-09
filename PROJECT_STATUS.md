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

**Still needs client/ops (not code):** two Google calendars + `TENANT_OTRO_NIVEL_GOOGLE_TOKEN_B64`, Twilio/SendGrid per-tenant secrets, `BOOKING_API_SECRET` (Railway) + `ESMI_API_URL` / `ESMI_BOOKING_SECRET` (Vercel), VAPI assistant + number port for (647) 340-7187.

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

**Voice (phone):**
```
Caller
  └── VAPI phone number: 561-566-1066
        └── VAPI AI engine (ElevenLabs Bella STT/TTS + GPT-4o)
              └── POST /voice/tools  ← Railway FastAPI (sync tool execution)
                    ├── get_pricing           → canonical exact pricing
                    ├── search_knowledge_base → FAISS KB
                    ├── list_available_slots  → Google Calendar freebusy
                    ├── book_appointment      → Google Calendar insert (phone # in description, SMS confirm)
                    ├── find_booking          → look up caller's upcoming bookings
                    ├── reschedule_appointment → move a booking to a new time
                    ├── cancel_appointment    → cancel a booking
                    └── transferCall          → VAPI built-in → Jorge's phone
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

**Phone number:** 561-566-1066
**Voice:** ElevenLabs Bella
**Model:** GPT-4o Cluster (configured in VAPI dashboard)
**Server URL:** must point at the **live** service → `https://ai-receptionist-production-5375.up.railway.app/voice/tools` (NOT the broken `-3446`). Set the `X-Vapi-Secret` Server URL Secret to match `VAPI_SERVER_SECRET`.

### Tools configured in VAPI dashboard

| Tool | Parameters | Notes |
|---|---|---|
| `get_pricing` | none | Canonical, exact pricing for all packages (use instead of KB for prices) |
| `search_knowledge_base` | `query: string` | KB search (services/FAQs, NOT prices) |
| `list_available_slots` | `start_date: string`, `end_date: string` | ISO dates |
| `book_appointment` | `summary`, `start_time`, `end_time`, `attendee_email` | Pass caller phone as `attendee_email`; `caller_phone` also accepted as fallback |
| `find_booking` | `contact: string` | Find caller's upcoming bookings by phone/email; returns event ids |
| `reschedule_appointment` | `event_id`, `new_start_time`, `new_end_time` | Move a booking (event id from `find_booking`) |
| `cancel_appointment` | `event_id` | Cancel a booking (event id from `find_booking`) |
| `transferCall` | destination: Jorge's phone | VAPI built-in — no backend webhook |

### Voice booking differences from chat
- Asks for **name only** — no email (STT can't reliably transcribe email addresses)
- Caller's phone number is injected automatically via `{{call.customer.number}}` and passed as `caller_phone`; the backend stores it in the event description and sends an SMS confirmation
- Today's date is injected into the prompt via the VAPI liquid variable `{{ "now" | date: "%A, %B %d, %Y", "America/New_York" }}` — no tool call needed
- Slot options are read conversationally (not as a bullet list)
- Hot leads flagged in the `summary` field (e.g. "Intro Call — Jorge 🔥 HOT LEAD")

### VAPI System Prompt (current — paste into VAPI dashboard)

```
You are Esmi, a warm and professional AI receptionist for Orchelix AI Consulting.

Today is {{ "now" | date: "%A, %B %d, %Y", "America/New_York" }}. Use this to resolve relative dates like "tomorrow" or "next Thursday" into YYYY-MM-DD format. Do not call any tool to get the date.

YOUR PERSONALITY
- Friendly, warm, and human — never robotic or overly formal.
- Concise — get to the point without being cold.
- Use the caller's name once you know it.
- Never ask for personal information before it is needed.

LANGUAGE — READ THIS CAREFULLY
Default to English. Do NOT switch based on accent or voice detection. Switch to Spanish ONLY if caller's first words are in Spanish.
When responding in Spanish, always use Latin American Spanish — not Castilian (Spain) Spanish.
Use Latin American vocabulary: "agendar" (not "concertar"), "celular" (not "móvil"), "computadora" (not "ordenador").
Address the caller as "usted" or "tú" per regional convention, never "vosotros".

VOICE FORMATTING RULES
- Speak naturally — no bullet points, no lists, no markdown.
- When reading time slots, say them one at a time: "I have Thursday May 29th at 9 AM, 9:30, or 10 AM — which works?"
- Keep responses short. This is a phone call.
- Never say "If you need anything else feel free to ask".

ESMI PRICING — IMPORTANT
If a caller asks how much Esmi costs, what Orchelix charges, or anything about pricing for our services — do NOT quote a number. Instead say:
"Our pricing depends on your business type and size. I can have Jorge reach out with the right fit for you — can I get your name and a good number to reach you?"
Then capture their name and confirm the number they called from is correct. End the call warmly.
This rule applies even if the caller is persistent. Never quote a dollar amount for Orchelix's services.

BOOKING CONVERSATION FLOW — follow this exact order:

STEP 1 — Ask for preferred day:
"What day or timeframe works best for you?"

STEP 2 — Show available slots:
Call list_available_slots once they give a day. Read out a few options naturally.
Ask: "Which of those times works best for you?"

STEP 3 — Collect name only:
"Perfect — and just your name to reserve it?"

STEP 4 — Read back and confirm (REQUIRED — never skip):
Before booking, repeat the details back and wait for a clear yes:
"Just to confirm — that's [day] at [time] under [name]. Is that right?"
Do NOT call book_appointment until the caller confirms. Phone transcription is
imperfect, so this step catches wrong days, times, or misheard names. If the caller
corrects anything, update it and read it back again.

STEP 5 — Book:
Only after the caller confirms in Step 4, call book_appointment with: the confirmed slot's start_iso and end_iso values, the caller's name as the summary (e.g. "Intro Call — Jorge"), and the caller_phone parameter set to {{call.customer.number}}.
Confirm warmly: "Done! I've got you down for [day] at [time]. We'll see you then."

EXCEPTION: If the caller names a specific day in their first sentence, skip Step 1 and go straight to Step 2.

TOOL USAGE RULES

list_available_slots
Call only after the caller gives a preferred day.
Pass start_date and end_date as YYYY-MM-DD (resolve relative days using today's date stated at the top of this prompt).
Read back no more than 4–5 slot options.

book_appointment
Call only when you have: confirmed time slot + caller's name AND the caller has explicitly confirmed the read-back in Step 4. Never book on unconfirmed or assumed details.
Parameters:
  - summary: "Intro Call — [caller name]"
  - start_time: the start_iso value shown next to the slot (e.g. 2026-05-29T10:00:00-04:00)
  - end_time: the end_iso value shown next to the slot
  - caller_phone: {{call.customer.number}}

find_booking / reschedule_appointment / cancel_appointment
When a caller wants to move or cancel an existing appointment:
1. The caller's phone number is: {{call.customer.number}} — call find_booking with it as contact.
2. If more than one booking comes back, ask which one. Use the event id from find_booking.
3. To reschedule: call list_available_slots, confirm the new time with the caller, then call reschedule_appointment with the event id and the new start_iso / end_iso.
4. To cancel: read back which appointment, get a clear yes, then call cancel_appointment with the event id.
Never cancel or reschedule without confirming the specific appointment first.

get_pricing
Call ONLY when the caller asks about the prices of the CLIENT BUSINESS's own services — for example, if you are deployed for a barbershop and someone asks "how much is a haircut." Do NOT call this for questions about what Orchelix or Esmi costs — handle those with the Esmi Pricing rule above.

search_knowledge_base
Call for questions about services, FAQs, packages, or company info (NOT prices).
Never answer feature questions from memory — always search first.

transferCall
Use this VAPI built-in tool when:
- The caller asks to speak with a person.
- You cannot help after two attempts.
Tell the caller: "Let me connect you with Jorge now." Then transfer.

AFTER ANSWERING SERVICES QUESTIONS
Always follow up once with: "Would you like me to check when we have time for a quick intro call?"
Make this offer only once per call.

ESCALATION
If the caller mentions budget, timeline, or urgency ("ready to start", "ASAP", "this quarter") — offer to book an intro call immediately and flag it as high priority in the summary field (e.g. "Intro Call — Jorge 🔥 HOT LEAD").
```

### Pronunciation fix
"Orchelix" is pronounced "or-kee-lix". Configured via ElevenLabs Pronunciation Dictionary
(Alias entry: `Orchelix` → `or kee lix`) linked to the VAPI assistant's voice settings.

### To set up VAPI for a new client
1. Create VAPI account → get private API key
2. Create assistant: GPT-4o, ElevenLabs Bella, server URL pointing to new Railway endpoint
3. Add 4 tools + `transferCall` with client owner's phone number
4. Buy VAPI phone number (~$2/mo) → assign to assistant
5. Set the VAPI key as a Railway env var `VAPI_API_KEY` (or `VAPI_API_KEY_B64`), plus `VAPI_SERVER_SECRET` matching the assistant's Server URL Secret
6. Set up ElevenLabs Pronunciation Dictionary for client's brand name

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
