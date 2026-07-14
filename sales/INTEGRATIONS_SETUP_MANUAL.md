# Integrations Setup — Twilio, SendGrid, Website, VAPI (per new client)

Scope: the three remaining onboarding phases after the calendar is wired up (see
`sales/CALENDAR_SETUP_MANUAL.md`). This covers SMS + email secrets, the booking API
secret between the client's website and Railway, and the VAPI voice assistant + phone
number. Full flow: `sales/CLIENT_ONBOARDING_CHECKLIST.md`.

All secrets below are **Railway/Vercel environment variables only** — never commit a
secret to the repo (the secret-scan hook blocks it).

## What you need before you start

- Client's tenant slug (e.g. `otro-nivel`) already live in the repo
- A Twilio account (yours, managed on the client's behalf, or theirs)
- A SendGrid account with the client's sender domain access
- The client's website repo, if you're also building their site
- ~20–30 min if the client already has (or doesn't need) a ported phone number;
  add days/weeks if porting an existing number (see the VAPI section)

---

## Part A — Twilio SMS confirmations

Booking/reschedule/cancel confirmations text the caller. Code reads three per-tenant vars
(`tools.py`, `_send_sms_confirmation` / `_send_confirmation_code_sms`) — if any is missing,
SMS sends are skipped silently (logged, not fatal).

1. Buy or reuse an SMS-capable Twilio number for this client (this is also the number
   VAPI will use if the client doesn't already have one — see Part D).
2. From the Twilio console, copy the Account SID and Auth Token.
3. Set on Railway:

```
TENANT_<SLUG>_TWILIO_ACCOUNT_SID = <Account SID>
TENANT_<SLUG>_TWILIO_AUTH_TOKEN  = <Auth Token>
TENANT_<SLUG>_TWILIO_SMS_FROM    = <the number, E.164, e.g. +16473407187>
```

`<SLUG>` is the tenant id, uppercased, hyphens → underscores (e.g. `otro-nivel` →
`OTRO_NIVEL`).

4. Confirmation message text comes from `tenants/<slug>/config.json` → `sms_templates`
   (`confirmation_en` / `confirmation_es`) — already set if the tenant was scaffolded
   from `new-client`.

## Part B — SendGrid email (booking notifications + escalations)

1. In SendGrid: Settings → Sender Authentication → verify the client's `emails.from`
   domain (from `tenants/<slug>/config.json`).
2. Settings → API Keys → Create API Key (Mail Send permission only).
3. Set on Railway:

```
TENANT_<SLUG>_SENDGRID_API_KEY = <the key>
```

(A `_SENDGRID_API_KEY_B64` base64 variant is also accepted if you'd rather not paste a
raw key — same idea, base64-encode it first.)

4. Confirm `emails.from` / `booking_to` / `escalation_to` in `config.json` are the
   client's real addresses, not placeholders.

## Part C — Website ↔ Railway booking API

Only needed if the client's website books through Esmi's REST API
(`/bookings/availability`, `/bookings`, `/bookings/lookup`).

1. Generate a long random secret (e.g. `openssl rand -hex 32`).
2. Set the **same value** in both places:

```
Railway (Esmi backend)      BOOKING_API_SECRET   = <the secret>
Vercel (client website)     ESMI_BOOKING_SECRET   = <same secret>
```

3. Also set on Vercel:

```
ESMI_API_URL    = https://ai-receptionist-production-5375.up.railway.app
ESMI_TENANT_ID  = <slug>
```

4. If the site also embeds the Esmi chat widget (not just the booking wizard), it needs
   `CHAT_PROXY_SECRET` too — same value as Railway's `CHAT_PROXY_SECRET`, plus the same
   `ESMI_API_URL` / `ESMI_TENANT_ID`.
5. `BOOKING_API_SECRET` is a single global secret, not per-tenant — every client's
   website uses the same Railway-side value, paired with `X-Tenant-Id` to scope requests
   to the right tenant.
6. Deploy the website: `vercel deploy --prod --yes`.

## Part D — VAPI voice assistant + phone number

**Decide the phone number path first** (see `sales/CLIENT_ONBOARDING_CHECKLIST.md` Phase 3
for the full decision table):
- No existing number → buy one directly in VAPI (US) or Twilio (non-US) → import to VAPI.
- Existing number, want it live now → buy a new number, client forwards their old one to it.
- Existing number, want it native → port into Twilio → import to VAPI (2–4 weeks).

Steps:

1. **Create the assistant** in the VAPI dashboard: GPT-4o model, ElevenLabs voice
   (bilingual if the client needs EN/ES).
2. **Server URL**: `https://ai-receptionist-production-5375.up.railway.app/voice/tools`.
   **Server URL Secret**: the same value already set as `VAPI_SERVER_SECRET` on Railway —
   this is one shared secret for every tenant's assistant, not per-client.
3. **Add tools** — one per row below, pointing at the same Server URL:

   | Tool | Parameters |
   |---|---|
   | `get_pricing` | none |
   | `search_knowledge_base` | `query: string` |
   | `list_available_slots` | `start_date`, `end_date`, `location`, `service` (last two only if the tenant has multiple locations/services) |
   | `book_appointment` | `summary`, `start_time`, `end_time`, `attendee_email`, `location`, `service` |
   | `find_booking` | `contact`, `location` (optional) |
   | `request_cancellation_code` | `event_id` |
   | `reschedule_appointment` | `event_id`, `new_start_time`, `new_end_time`, `confirmation_code`, `location` |
   | `cancel_appointment` | `event_id`, `confirmation_code` |
   | `escalate_to_human` | `reason`, `user_summary` |
   | `transferCall` (VAPI built-in) | destination = the client's `transfer_phone` from `config.json` |

4. **System prompt**: paste `tenants/<slug>/prompts/system.md` (or the shared
   `prompts/esmi_system.md` if the tenant has no override), then add at the top:
   `Today is {{ "now" | date: "%A, %B %d, %Y", "<business_tz>" }}. Use this to resolve
   relative dates. Do not call any tool to get the date.` — swap in the tenant's real
   timezone. Voice callers give name only, not email — the caller's number is injected
   automatically via `{{call.customer.number}}`.
5. **Pronunciation dictionary**: add the business name if it's non-obvious to TTS.
6. **Attach the phone number** to the assistant.
7. **Write the real IDs into the tenant config** — `tenants/<slug>/config.json`:

```json
"vapi": {
  "assistant_ids": ["asst_..."],
  "phone_number_ids": ["pn_..."]
}
```

This is how inbound calls get routed to the right tenant (`resolve_vapi_tenant` matches
either id — only one needs to be populated, but both is safer).

8. Commit + push `tenants/<slug>/config.json` → Railway auto-deploys.
9. If forwarding an existing number: have the client set up carrier call-forwarding to
   the new VAPI number now.

## Verify (all four parts)

- Book once via the website wizard (if built) → event on the calendar, SMS arrives,
  booking notification email arrives.
- Call the VAPI number → book an appointment → calendar event + SMS confirmation.
- Trigger an escalation (ask to speak to a person, or mention budget/urgency) → email
  lands at `escalation_to`, and `transferCall` rings the right number.
- Ask a question in Spanish (if bilingual) → confirms voice + prompt language handling.
