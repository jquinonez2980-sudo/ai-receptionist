# Esmi — AI Receptionist Backend

Esmi is a production AI receptionist and lead-qualification agent built for Orchelix AI Consulting.
It handles inbound inquiries over web chat (FastAPI SSE streaming) and phone (VAPI voice), books
Google Calendar appointments, answers pricing and service questions from a private knowledge base,
and escalates hot leads to a human via SendGrid email. The agent is powered by GPT-4o via LangGraph
and supports multiple tenants through per-tenant config, secrets, and calendar credentials.

---

## Run locally

**Environment variables required** (copy `.env.example` to `.env` and fill in):

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | GPT-4o + embeddings |
| `GOOGLE_TOKEN_B64` | Base64-encoded Google OAuth JSON (see PROJECT_STATUS.md) |
| `SENDGRID_API_KEY` | SendGrid for booking/escalation emails |
| `VAPI_SERVER_SECRET` | Shared secret for `/voice/tools` webhook auth |
| `ALLOW_UNAUTHENTICATED_VOICE=1` | Local dev bypass for voice auth (never set in prod) |

```bash
pip install -r requirements.txt
uvicorn api:app --reload --port 8000
```

The API is now available at `http://localhost:8000`. Health check: `GET /health`.

---

## Run tests

```bash
pip install -r requirements-dev.txt
pytest evals/ -k "not llm" -v
```

The `-k "not llm"` filter skips tests that require a real `OPENAI_API_KEY`. All pure-logic
unit tests in `evals/test_units.py` and isolation tests in `evals/test_multi_tenant.py` run
without any API keys.

---

## Lint

```bash
pip install ruff
ruff check .
ruff check . --fix   # auto-fix safe issues
```

---

## Deploy (Railway)

Push to `main` — Railway auto-deploys from the Dockerfile. The live service is
`ai-receptionist-production-5375` (`awake-nourishment`).

**All secrets are Railway runtime env vars — never baked into the image.**
See `PROJECT_STATUS.md` for the full deployment runbook, env var list, credential
rotation steps, and Railway service details.

Health checks after deploy:
- `GET /health` — liveness
- `GET /health/calendar` — Google Calendar auth trace
- `GET /health/sendgrid` — sends a test email (requires `X-Vapi-Secret` header)

---

## Entry points

| Channel | Details |
|---|---|
| **Web chat** | `POST /chat` (SSE streaming) — proxied by the Next.js frontend at `/try-esmi` |
| **Voice (phone)** | Per-tenant VAPI numbers (Orchelix 561-566-1066, otro-nivel 437-292-3949, coastline-condos 754-799-2655) → `POST /voice/tools` webhook |

For full architecture, KB structure, VAPI prompt, and multi-tenant runbook, see
[PROJECT_STATUS.md](PROJECT_STATUS.md).
