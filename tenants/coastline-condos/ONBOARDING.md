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
- [x] Voice / VAPI (done 2026-07-18): assistant `Coastline_Condos` (`a351deb6-bf22-4cda-a3f3-67bca8ac6346`) with dedicated voice prompt (tour booking flow, EN/ES, America/Guayaquil), 10 tools (slots/book/find/code/cancel/reschedule/pricing/KB/escalate + transferCall → +593 96 994 3941), Twilio number `+17547992655` (`dc03d2e6-f090-4e8f-bffa-cdfaeaf30777`) attached; IDs live in `config.json` → `vapi.*`, tenant routing verified end-to-end

## Demo URL
`https://www.orchelix.com/try-esmi?tenant=coastline-condos&company=Coastline+Condos`
