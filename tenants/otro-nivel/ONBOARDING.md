# Otro Nivel — Esmi Onboarding Runbook

Client-facing checklist. Code and tenant content ship without these; live phone/web booking needs the items below.

## 1. Google Calendars (required for real bookings)

Create **two** calendars on a client-controlled or Orchelix-managed Google account:

| Calendar name | Location | Suggested calendar id usage |
|---|---|---|
| Otro Nivel — Weston | 2851 Weston Road | Set `locations.weston.calendar_id` in `config.json` |
| Otro Nivel — Keele | 2266 Keele Street | Set `locations.keele.calendar_id` in `config.json` |

Until real IDs exist, both locations use `"primary"` (fine for a single test calendar; **not** for production — will double-book across shops).

DONE (2026-07-14): both calendars created under the Orchelix-managed Google account
(policy — see `sales/CLIENT_ONBOARDING_CHECKLIST.md` Phase 2) and real calendar IDs are in
`config.json`. Credentials copied from the master Orchelix Google account (individual vars,
matching how the master credential itself is stored on Railway):

```
TENANT_OTRO_NIVEL_GOOGLE_REFRESH_TOKEN=<same value as master GOOGLE_REFRESH_TOKEN>
TENANT_OTRO_NIVEL_GOOGLE_CLIENT_ID=<same value as master GOOGLE_CLIENT_ID>
TENANT_OTRO_NIVEL_GOOGLE_CLIENT_SECRET=<same value as master GOOGLE_CLIENT_SECRET>
```

Verified live via `/bookings/availability` for both `weston` and `keele`.

## 2. Twilio SMS confirmations

```
TENANT_OTRO_NIVEL_TWILIO_ACCOUNT_SID=
TENANT_OTRO_NIVEL_TWILIO_AUTH_TOKEN=
TENANT_OTRO_NIVEL_TWILIO_SMS_FROM=   # E.164, e.g. +1647...
```

Templates live in `config.json` → `sms_templates` (EN/ES).

## 3. SendGrid email (ops + escalation)

```
TENANT_OTRO_NIVEL_SENDGRID_API_KEY=
```

Config already points:
- `emails.from` → info@otronivelbarbershop.com (must be verified sender in SendGrid)
- `emails.booking_to` / `escalation_to` → dawnatemporal1111@gmail.com

## 4. Website ↔ Railway secrets

| Where | Env var | Purpose |
|---|---|---|
| Railway (Esmi) | `CHAT_PROXY_SECRET` | Protects `POST /chat` |
| Vercel (website) | `CHAT_PROXY_SECRET` | **Same value** — sent as `X-Chat-Secret` by `/api/chat` |
| Railway (Esmi) | `BOOKING_API_SECRET` | Protects `/bookings/*` |
| Vercel (website) | `ESMI_BOOKING_SECRET` | **Same value** as Railway `BOOKING_API_SECRET` |
| Vercel | `ESMI_API_URL=https://ai-receptionist-production-5375.up.railway.app` | |
| Vercel | `ESMI_TENANT_ID=otro-nivel` | Optional; proxy defaults to this |

Without matching `CHAT_PROXY_SECRET` on both sides, the chat widget shows
“Esmi is having trouble right now” (upstream 401).

Website never exposes secrets to the browser — only Next.js route handlers call Railway.

## 5. VAPI voice assistant — DONE (2026-07-18)

- Assistant `Otro_Nivel_Esmi` (`32994d60-3712-4183-a7db-edc3badeabec`), GPT-4.1,
  ElevenLabs voice, Deepgram flux EN/ES, 10 tools attached (slots/book/find/
  code/cancel/reschedule/pricing/KB/escalate + transferCall → 647-569-1194).
- Twilio CA number **+1 437 292 3949** (`8313f753-67f4-4c11-8f31-778b11692089`)
  imported to VAPI and pointed at the assistant.
- Tool webhook → `.../voice/tools` with `X-Vapi-Secret` header on every function
  tool; assistant server uses the "Esmi Production Secret" VAPI credential.
- Real IDs are in `tenants/otro-nivel/config.json` → `vapi.*` (tenant routing verified).

**Still pending (client-side):** have the shop call-forward **(647) 340-7187**
to +1 437 292 3949; port the number natively later if desired (2–4 weeks).

## 6. Deploy

- **Backend:** push `ai-receptionist` to `main` → Railway auto-deploy.
- **Website:** `vercel deploy --prod --yes` from `website/` after env vars are set.
- Smoke test: book once online → event on correct location calendar → SMS arrives.

## 7. Verify locally (no production secrets)

```bash
# From ai-receptionist/
uvicorn api:app --reload --port 8000

# Availability (Saturday should return walk_in_only + empty slots)
curl "http://127.0.0.1:8000/bookings/availability?location=weston&service=fade&date=2026-07-11" \
  -H "X-Tenant-Id: otro-nivel"

# Create (needs real Google creds)
curl -X POST http://127.0.0.1:8000/bookings \
  -H "Content-Type: application/json" \
  -H "X-Tenant-Id: otro-nivel" \
  -H "Idempotency-Key: test-key-1" \
  -d '{"location":"weston","service":"fade","start_time":"2026-07-10T11:00:00-04:00","name":"Test","phone":"+14165551234","lang":"en"}'
```

## Out of scope for engineering alone

- VAPI dashboard clicks
- Google account creation / OAuth consent
- Number porting for (647) 340-7187
- Twilio / SendGrid account credentials
- Client confirmation of Keele hours if intake was ambiguous
