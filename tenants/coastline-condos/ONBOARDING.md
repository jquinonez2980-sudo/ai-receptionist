# Coastline Condos — Esmi Onboarding

Orchelix-managed tenant. Bookings/escalations notify the client Gmail; phone SMS from Twilio.

| Item | Value |
|------|--------|
| Tenant slug | `coastline-condos` |
| Company | Coastline Condos |
| Timezone | America/Guayaquil |
| Business hours | Mon–Sat 09:00–18:00 (America/Guayaquil) |
| Primary WhatsApp | +593 96 994 3941 |
| Secondary WhatsApp | +593 99 484 3667 |
| Client notify email | coastlinecondosecu@gmail.com |
| Twilio SMS / voice number | +17547992655 |
| Site | Coastline Condos static site (Vercel) |
| Calendar | Orchelix-managed (see `calendar_id` in config.json) |

## Done in code
- [x] `tenants/coastline-condos/config.json` + KB + prompts
- [x] `calendar_id` wired (Google Calendar group calendar)
- [x] Booking / escalation **to** `coastlinecondosecu@gmail.com`
- [x] SendGrid **from** `info@orchelix.com` (Orchelix-managed verified sender)
- [x] Website chat widget → Esmi (SSE) with Esmi logo
- [x] Vercel `/api/chat` proxy (secrets stay server-side)

## Ops still needed
- [ ] Railway env vars on **live service -5375** (never commit secrets):

```text
TENANT_COASTLINE_CONDOS_GOOGLE_REFRESH_TOKEN=<same as master GOOGLE_REFRESH_TOKEN>
TENANT_COASTLINE_CONDOS_GOOGLE_CLIENT_ID=<same as master GOOGLE_CLIENT_ID>
TENANT_COASTLINE_CONDOS_GOOGLE_CLIENT_SECRET=<same as master GOOGLE_CLIENT_SECRET>

TENANT_COASTLINE_CONDOS_SENDGRID_API_KEY=<SendGrid key that can send as info@orchelix.com>

TENANT_COASTLINE_CONDOS_TWILIO_ACCOUNT_SID=...
TENANT_COASTLINE_CONDOS_TWILIO_AUTH_TOKEN=...
TENANT_COASTLINE_CONDOS_TWILIO_SMS_FROM=+17547992655
```

- [ ] Share calendar with `coastlinecondosecu@gmail.com` (Viewer or Editor) if not already
- [ ] Push `main` so Railway deploys this tenant config (verify **-5375**)
- [ ] Vercel env on Coastline project:
  - `ESMI_API_URL` = `https://ai-receptionist-production-5375.up.railway.app`
  - `CHAT_PROXY_SECRET` = same as Railway
  - `ESMI_TENANT_ID` = `coastline-condos`
- [ ] Add Coastline origin to Railway `ALLOWED_ORIGINS` only if browser hits Railway directly
- [ ] Smoke: site chat answers as Coastline; book a tour → event on this calendar + email to client Gmail
- [ ] Voice / VAPI: create assistant, attach `+17547992655`, put IDs in `config.json` → `vapi.assistant_ids` / `vapi.phone_number_ids`

## Demo URL
`https://www.orchelix.com/try-esmi?tenant=coastline-condos&company=Coastline+Condos`
