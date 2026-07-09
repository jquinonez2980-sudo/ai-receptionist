# Otro Nivel — Esmi Onboarding Runbook

Client-facing checklist. Code and tenant content ship without these; live phone/web booking needs the items below.

## 1. Google Calendars (required for real bookings)

Create **two** calendars on a client-controlled or Orchelix-managed Google account:

| Calendar name | Location | Suggested calendar id usage |
|---|---|---|
| Otro Nivel — Weston | 2851 Weston Road | Set `locations.weston.calendar_id` in `config.json` |
| Otro Nivel — Keele | 2266 Keele Street | Set `locations.keele.calendar_id` in `config.json` |

Until real IDs exist, both locations use `"primary"` (fine for a single test calendar; **not** for production — will double-book across shops).

OAuth: produce a refreshable token JSON, base64-encode it, set on Railway:

```
TENANT_OTRO_NIVEL_GOOGLE_TOKEN_B64=<base64 of token.json>
```

Optional individual vars: `TENANT_OTRO_NIVEL_GOOGLE_REFRESH_TOKEN`, `_CLIENT_ID`, `_CLIENT_SECRET`.

Share both calendars with the OAuth account (write access).

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

## 4. Booking API secret (website ↔ Railway)

Same value in both places:

| Where | Env var |
|---|---|
| Railway (Esmi) | `BOOKING_API_SECRET` |
| Vercel (website) | `ESMI_BOOKING_SECRET` |
| Vercel | `ESMI_API_URL=https://<esmi-railway-host>` |
| Vercel | `ESMI_TENANT_ID=otro-nivel` (optional; proxy defaults to this) |

Website never exposes the secret to the browser — only Next.js route handlers call Railway.

## 5. VAPI voice assistant

1. Create assistant in VAPI dashboard (ElevenLabs bilingual voice recommended).
2. Attach / port number **(647) 340-7187** (porting still pending per knowledge base §12).
3. Point tool webhook at `https://<esmi-host>/voice/tools` with `VAPI_SERVER_SECRET`.
4. Put real IDs into `tenants/otro-nivel/config.json`:

```json
"vapi": {
  "assistant_ids": ["asst_..."],
  "phone_number_ids": ["pn_..."]
}
```

5. Transfer number for human handoff: **647-569-1194** (`transfer_phone` in config).

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
