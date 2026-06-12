# CLAUDE.md — Esmi AI Receptionist (backend)

Production AI receptionist + lead-qualification agent for Orchelix. This repo is the
Python backend. The web frontend lives in a separate repo (`orhelix-website`).

**Full reference:** read `PROJECT_STATUS.md` for architecture, env vars, deployment, VAPI
voice setup, and runbooks. This file is the short, must-not-violate summary.

## Stack
- **Agent:** LangGraph `create_agent` (GPT-4o, temp 0) in `agents.py`, wrapped by a
  `StateGraph` in `graph.py`. Two modes controlled by `USE_MULTI_AGENT` env var:
  - `USE_MULTI_AGENT=0` (default): single `receptionist_agent` with all 8 tools.
  - `USE_MULTI_AGENT=1`: three specialist agents — informer, booker, closer — with
    rule-based routing + LLM fallback. Both modes expose the same `graph` API surface.
- **API:** FastAPI in `api.py` — `POST /chat` (SSE streaming, web) and `POST /voice/tools`
  (sync, VAPI phone). Rate-limited 10/min/IP via `slowapi`.
- **Tools:** 8 LangChain `@tool`s in `tools.py` — KB search (FAISS), pricing, calendar
  slots/book/find/reschedule/cancel, escalate-to-human (SendGrid).
- **Persistence:** LangGraph `AsyncPostgresSaver` (Railway `DATABASE_URL`); `MemorySaver`
  fallback loses history on restart.
- **Channels:** web chat + VAPI voice (561-566-1066, ElevenLabs Bella). Voice prompt lives in
  the VAPI dashboard (mirrored in `PROJECT_STATUS.md`), not in this repo.

## Hard rules — do not violate
1. **Never write a live secret into a tracked file or the Dockerfile.** Secrets are Railway
   runtime env vars only — never `ENV ...KEY=` lines in the Dockerfile (that leaked before;
   see PROJECT_STATUS "Security"). A PreToolUse hook (`.claude/hooks/secret-scan.py`) enforces this.
2. **Pricing is canonical in `_PRICING` (`tools.py`).** Keep it in sync with
   `orchelix_knowledge_base/13_pricing_tiers.md`. The agent must answer prices via the
   `get_pricing` tool — never from the KB or from memory. Don't change one pricing source
   without the other.
3. **Pushing to `main` redeploys BOTH Railway services.** The LIVE customer backend is
   `ai-receptionist-production-5375` (`awake-nourishment`). `-3446` (`aware-nature`) is a
   broken stale duplicate — never assume a push only hit the live one; verify `-5375`.
4. **The agent persona, booking flow, and tool-usage rules are authoritative in
   `prompts/esmi_system.md`** (per-tenant overrides in `tenants/<id>/prompts/`).
   Voice has its own prompt in the VAPI dashboard — keep the two behaviorally consistent
   (booking read-back before `book_appointment`, escalate on budget/timeline/urgency,
   LATAM Spanish not Castilian).
5. **Don't read or echo secret files** (`.env`, `orhelix-esmi.txt`, `credentials.json`,
   `.railway_tmp.env`, `.streamlit/`, `*.key`, `*.pem`). They are git-ignored and deny-listed.

## Common commands
- Smoke-import the agent: `python -c "from agents import receptionist_agent; print('OK')"`
- Build graph locally: `python -c "from graph import graph; print('OK')"`
- Railway logs / status / vars: `railway logs`, `railway status`, `railway variables`
- Health checks (live): `/health`, `/health/calendar`, `/health/env`, `/health/sendgrid`

## Layout
- `agents.py` — single receptionist agent + specialist factory functions (informer/booker/closer)
- `tools.py` — tool implementations, `_PRICING`, `_BUSINESS_TZ`, `_HOURS`, calendar/SendGrid/SMS helpers
- `graph.py` — checkpointer factory + StateGraph (single-agent or multi-agent per `USE_MULTI_AGENT`)
- `api.py` — FastAPI: `/chat` (SSE), `/voice/tools`, health endpoints
- `state.py` — `AgentState` TypedDict
- `tenants.py` — tenant registry, per-tenant config/secrets, `load_tenant()`
- `prompts/` — system prompt files (`esmi_system.md`, `informer.md`, `booker.md`, `closer.md`)
- `tenants/` — per-tenant overrides (config.json, prompts/, kb/)
- `orchelix_knowledge_base/` — 14 markdown KB docs (FAISS index auto-builds to `.kb_index/`)
- `PROJECT_STATUS.md` — the deep reference for everything above
