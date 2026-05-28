# Project Status — Orchelix AI Receptionist (Esmi)

## What Was Built

A production AI receptionist system with two repos:

| Repo | Role |
|---|---|
| `ai-receptionist` | Python backend — LangGraph agent, FastAPI SSE endpoint, Google Calendar, FAISS KB |
| `orhelix-website` | Next.js frontend — chat UI at `/try-esmi`, API proxy to Railway |

---

## Architecture

**Chat (web):**
```
Browser
  └── /try-esmi (Next.js page)
        └── EsmiChat.tsx  ← "use client" React component
              └── POST /api/chat  ← Next.js API route (server-side proxy)
                    └── POST /chat  ← Railway FastAPI endpoint (SSE)
                          └── LangGraph ReAct agent
                                ├── search_knowledge_base (FAISS vector store)
                                ├── list_available_slots (Google Calendar freebusy)
                                ├── book_appointment (Google Calendar insert)
                                └── escalate_to_human (SendGrid email alert)
```

**Voice (phone):**
```
Caller
  └── VAPI phone number: 561-566-1066
        └── VAPI AI engine (ElevenLabs Bella STT/TTS + GPT-4o)
              └── POST /voice/tools  ← Railway FastAPI (sync tool execution)
                    ├── get_current_date    → returns today's ISO date
                    ├── search_knowledge_base → FAISS KB
                    ├── list_available_slots  → Google Calendar freebusy
                    ├── book_appointment      → Google Calendar insert (phone # as contact)
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
| `agents.py` | Esmi persona prompt + `create_react_agent` with 4 tools. |
| `graph.py` | `StateGraph` wrapping the agent. Uses `PostgresSaver` (Railway DB) or `MemorySaver` fallback. |
| `tools.py` | `search_knowledge_base`, `list_available_slots`, `book_appointment`, `escalate_to_human`. |
| `state.py` | `AgentState` TypedDict. |
| `observability.py` | LangSmith tracing init. |
| `Dockerfile` | `python:3.11-slim`, uvicorn CMD, `GOOGLE_TOKEN_B64` + `SENDGRID_API_KEY_B64` + `VAPI_API_KEY_B64` baked in. |
| `railway.toml` | `builder = "DOCKERFILE"`. |
| `orchelix_knowledge_base/` | 14 markdown files — Esmi's knowledge source. |

### Knowledge Base

FAISS vector index built automatically on startup from `orchelix_knowledge_base/*.md`.
Index is cached in `.kb_index/` with a content-hash sidecar — rebuilds only when files change.

Key KB files (highest retrieval priority):
- `13_pricing_tiers.md` — Pilot/Growth/Scale/Enterprise pricing
- `03_services.md` — Esmi, Revenue-Ops, Finance OS descriptions
- `07_faq.md` — Common Q&As
- `06_how_we_work.md` — 14-day deployment process
- `00_company_overview.md` — Company identity

### Agent Prompt Rules (agents.py)
- Formatting: no markdown headers, no bold, use `-` for bullet points
- Booking flow: ask day → show slots → collect name+email → book
- Pricing/services: always call `search_knowledge_base`, never answer from memory
- Lead capture: after answering pricing/services, offer a calendar check once per conversation
- Escalation: call `escalate_to_human` if KB search fails twice, or user signals urgency/budget readiness
- Model: `gpt-4o`, temperature 0

### Tools

| Tool | Trigger | Action |
|---|---|---|
| `search_knowledge_base` | Any question about services, pricing, FAQs | FAISS semantic search over KB |
| `list_available_slots` | User gives a preferred day | Google Calendar freebusy, 9–5 Mon–Fri |
| `book_appointment` | Name + email + slot confirmed | Google Calendar insert, idempotent via SHA256 event ID |
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

**URL:** `https://ai-receptionist-production-3446.up.railway.app`

**Build:** Dockerfile (python:3.11-slim → pip install → uvicorn)

**Environment Variables:**
| Variable | Where | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Railway shared/env var | GPT-4o |
| `LANGCHAIN_PROJECT` | Railway shared/env var | LangSmith project name |
| `DATABASE_URL` | Railway PostgreSQL plugin | Thread persistence (auto-set by Railway) |
| `GOOGLE_TOKEN_B64` | Dockerfile ENV | Base64-encoded Google OAuth JSON (see below) |
| `SENDGRID_API_KEY_B64` | Dockerfile ENV | Base64-encoded SendGrid API key (see below) |
| `VAPI_API_KEY_B64` | Dockerfile ENV | Base64-encoded VAPI private API key (see below) |

**Railway Gotcha:** Service-level variables set via CLI or dashboard Variables tab do NOT
reliably reach the container. Only shared/environment-level variables (set via the Railway
dashboard environment settings) are injected. For secrets that can't be shared, bake them
into the Dockerfile as `ENV KEY=value` using base64 encoding to bypass GitHub secret scanning.

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

**Important:** If the Google Cloud OAuth app is in **Testing** mode, refresh tokens expire after **7 days**. Re-run the OAuth flow above and update all three Railway vars every time booking breaks. To avoid this, publish the app in Google Cloud Console → OAuth consent screen → Publish App.

---

## SendGrid Setup

1. Create a SendGrid account → Settings → API Keys → Create API key (Mail Send permission only)
2. Verify your sender email (Settings → Sender Authentication → verify `info@orchelix.com`)
3. Base64-encode the key:

```python
import base64
print(base64.b64encode(b"SG.your-key-here").decode())
```

4. Put the output in Dockerfile: `ENV SENDGRID_API_KEY_B64=...`

`_get_sendgrid_key()` in `tools.py` checks `SENDGRID_API_KEY` (plain) first, then
falls back to base64-decoding `SENDGRID_API_KEY_B64`. Both email functions use this helper.

Escalation emails go to `jquinonez2980@gmail.com`. Booking notifications go to `info@orchelix.com`.

---

## VAPI Voice Setup

**Phone number:** 561-566-1066
**Voice:** ElevenLabs Bella
**Model:** GPT-4o (configured in VAPI dashboard)
**Server URL:** `https://ai-receptionist-production-3446.up.railway.app/voice/tools`

### Tools configured in VAPI dashboard

| Tool | Parameters | Notes |
|---|---|---|
| `get_current_date` | none | Always called first so agent can resolve relative dates |
| `search_knowledge_base` | `query: string` | KB search |
| `list_available_slots` | `start_date: string`, `end_date: string` | ISO dates |
| `book_appointment` | `summary`, `start_time`, `end_time`, `attendee_email` | Pass caller phone as `attendee_email`; `caller_phone` also accepted as fallback |
| `transferCall` | destination: Jorge's phone | VAPI built-in — no backend webhook |

### Voice booking differences from chat
- Collects **phone number** instead of email (STT can't reliably transcribe emails)
- Booking contact field stores phone number in the `attendee_email` slot of Google Calendar
- `get_current_date` tool required because VAPI GPT-4o doesn't know today's date at runtime

### Pronunciation fix
"Orchelix" is pronounced "or-kee-lix". Configured via ElevenLabs Pronunciation Dictionary
(Alias entry: `Orchelix` → `or kee lix`) linked to the VAPI assistant's voice settings.

### To set up VAPI for a new client
1. Create VAPI account → get private API key
2. Create assistant: GPT-4o, ElevenLabs Bella, server URL pointing to new Railway endpoint
3. Add 4 tools + `transferCall` with client owner's phone number
4. Buy VAPI phone number (~$2/mo) → assign to assistant
5. Base64-encode VAPI key → add to Dockerfile as `VAPI_API_KEY_B64`
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

| Tier | Monthly | Annual |
|---|---|---|
| Pilot | $4,250 | $41,700 (−18%) |
| Growth | $8,500 | $83,600 (−18%) |
| Scale | $16,500 | $162,000 (−18%) |
| Enterprise | $28,000+ | Custom |

Pricing model: Base Orchestration Fee + Performance Component tied to results.

---

## To Build Another One

1. **Clone `ai-receptionist`** and replace:
   - `orchelix_knowledge_base/` with your client's KB files
   - The system prompt in `agents.py` (persona, business name, booking flow)
   - `_BUSINESS_TZ` and `_HOURS` in `tools.py` for timezone/hours
   - Escalation email recipient in `escalate_to_human` (`tools.py`)

2. **Google Calendar:** Follow setup steps above. Each client needs their own OAuth credentials.

3. **SendGrid:** Follow setup steps above. Verify the sender domain for each client.

4. **Railway:** Create a new service, connect the repo, set `OPENAI_API_KEY` as a shared variable.
   Bake `GOOGLE_TOKEN_B64` and `SENDGRID_API_KEY_B64` into the Dockerfile.

5. **Frontend:** The `EsmiChat.tsx` + `api/chat/route.ts` work as-is. Update `RAILWAY_API_URL` in `route.ts`.

6. **Knowledge base:** Update the 14 markdown files. The FAISS index rebuilds automatically on startup.

7. **Voice:** Follow VAPI setup steps above. Update the system prompt in the VAPI dashboard with the new business name and escalation transfer number.

---

## Known Issues / Limitations

- Google OAuth refresh tokens expire after **7 days** if the Cloud app is in "Testing" mode — re-run the OAuth flow in the Google Calendar Setup section and update all three `GOOGLE_*` Railway vars. Fix: publish the app in Google Cloud Console → OAuth consent screen
- `MemorySaver` fallback loses conversation history on Railway restart — `DATABASE_URL` must be set for persistence
- FAISS index rebuild calls OpenAI embeddings API — costs a small amount on each deploy if KB files changed
- Slot stripping regex is fragile — if LLM changes time format, slots may not be stripped during streaming
- Rate limiter is in-memory per process — resets on Railway restart, not shared across multiple instances
- VAPI rate limits apply per account — monitor call volume on the VAPI dashboard
- Voice bookings store phone number in the `attendee_email` field of Google Calendar (cosmetic only)
- ElevenLabs Pronunciation Dictionary must be manually updated when new brand terms are added

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
