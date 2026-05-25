# Project Status — Orchelix AI Receptionist (Esmi)

## What Was Built

A production AI receptionist system with two repos:

| Repo | Role |
|---|---|
| `ai-receptionist` | Python backend — LangGraph agent, FastAPI SSE endpoint, Google Calendar, FAISS KB |
| `orhelix-website` | Next.js frontend — chat UI at `/try-esmi`, API proxy to Railway |

---

## Architecture

```
Browser
  └── /try-esmi (Next.js page)
        └── EsmiChat.tsx  ← "use client" React component
              └── POST /api/chat  ← Next.js API route (server-side proxy)
                    └── POST /chat  ← Railway FastAPI endpoint (SSE)
                          └── LangGraph ReAct agent
                                ├── search_knowledge_base (FAISS vector store)
                                ├── list_available_slots (Google Calendar freebusy)
                                └── book_appointment (Google Calendar insert)
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
| `api.py` | FastAPI app. `POST /chat` streams SSE. `GET /health/calendar` for diagnostics. |
| `agents.py` | Esmi persona prompt + `create_react_agent` with 3 tools. |
| `graph.py` | `StateGraph` wrapping the agent. Uses `PostgresSaver` (Railway DB) or `MemorySaver` fallback. |
| `tools.py` | `search_knowledge_base`, `list_available_slots`, `book_appointment`. |
| `state.py` | `AgentState` TypedDict. |
| `observability.py` | LangSmith tracing init. |
| `Dockerfile` | `python:3.11-slim`, uvicorn CMD, `GOOGLE_TOKEN_B64` ENV baked in. |
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
- Model: `gpt-4o-mini`, temperature 0

### Streaming Gotcha — LangGraph 1.1.10
With `create_react_agent` wrapped in a `StateGraph`, `on_chat_model_stream` events
from the inner graph don't always surface through `astream_events(version="v2")`.
Fix: capture the final response from `on_chain_end` as a fallback (`api.py: _stream_chat`).

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
- Thread ID persistence via `localStorage`
- Reset conversation button

### Slot Flash Fix
During streaming, slot bullet lines are stripped client-side via `stripSlotLines()` so
they never appear as text. Cards render cleanly when the `done` event arrives.

### Design System
Tailwind v4 with `@theme` block in `globals.css`:
- `--color-navy-*`: `#0A2540` family
- `--color-teal-*`: `#00D4EE` family

---

## Deployment — Railway

**URL:** `https://ai-receptionist-production-5375.up.railway.app`

**Build:** Dockerfile (python:3.11-slim → pip install → uvicorn)

**Environment Variables (must be set):**
| Variable | Where | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Railway shared/env var | GPT-4o-mini |
| `LANGCHAIN_PROJECT` | Railway shared/env var | LangSmith project name |
| `SENDGRID_API_KEY` | Railway shared/env var | Booking email notifications |
| `DATABASE_URL` | Railway PostgreSQL plugin | Thread persistence (auto-set by Railway) |
| `GOOGLE_TOKEN_B64` | Dockerfile ENV | Base64-encoded Google OAuth JSON (see below) |

**Railway Gotcha:** Service-level variables set via CLI or dashboard Raw Editor do NOT
reliably reach the container. Only shared/environment-level variables (set via the
Railway dashboard's environment settings) are injected. For secrets that can't be shared,
bake them into the Dockerfile as `ENV KEY=value`.

---

## Google Calendar Setup

1. Create a Google Cloud project → enable Calendar API
2. Create OAuth 2.0 credentials (Desktop app type)
3. Run the OAuth flow locally to get `token.json`
4. Base64-encode the credentials (without the expired `token` field):

```python
import json, base64
data = {
    "refresh_token": "...",
    "client_id": "...",
    "client_secret": "...",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": ["https://www.googleapis.com/auth/calendar"],
    "universe_domain": "googleapis.com"
}
print(base64.b64encode(json.dumps(data).encode()).decode())
```

5. Put the output in Dockerfile: `ENV GOOGLE_TOKEN_B64=eyJ...`

The `refresh_token` auto-renews the expired access token on first API call.

**Diagnostic endpoints:**
- `GET /health` — liveness check
- `GET /health/calendar` — step-by-step auth trace
- `GET /health/env` — all env var names visible in container

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

2. **Google Calendar:** Follow setup steps above. Each client needs their own OAuth credentials.

3. **Railway:** Create a new service, connect the repo, set `OPENAI_API_KEY` as a shared variable. Bake the Google token into the Dockerfile.

4. **Frontend:** The `EsmiChat.tsx` + `api/chat/route.ts` work as-is. Update `RAILWAY_API_URL` in `route.ts`.

5. **Knowledge base:** Update the 14 markdown files. The FAISS index rebuilds automatically on startup.

---

## Known Issues / Limitations

- `token.json` refresh tokens expire if not used for 6 months — re-run OAuth flow to refresh
- `MemorySaver` fallback loses conversation history on Railway restart — `DATABASE_URL` must be set for persistence
- FAISS index rebuild calls OpenAI embeddings API — costs a small amount on each deploy if KB files changed
- Slot stripping regex is fragile — if LLM changes time format, it may not be stripped correctly
