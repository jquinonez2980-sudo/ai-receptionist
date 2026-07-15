# Coastline Condos — Esmi Onboarding

Client purchased Esmi for the Coastline Condos marketing site.

| Item | Value |
|------|--------|
| Tenant slug | `coastline-condos` |
| Company | Coastline Condos |
| Timezone | America/Guayaquil |
| Primary WhatsApp | +593 96 994 3941 |
| Secondary WhatsApp | +593 99 484 3667 |
| Email | hello@coastlinecondos.ec |
| Site | Coastline Condos static site (Vercel) |

## Done in code
- [x] `tenants/coastline-condos/config.json` + KB + prompts
- [x] Website chat widget → Esmi (SSE) with Esmi logo
- [x] Vercel `/api/chat` proxy (secrets stay server-side)

## Ops still needed
- [ ] Railway deploy of `ai-receptionist` includes this tenant (push `main`)
- [ ] Vercel env on Coastline project:
  - `ESMI_API_URL` = `https://ai-receptionist-production-5375.up.railway.app`
  - `CHAT_PROXY_SECRET` = same as Railway
  - `ESMI_TENANT_ID` = `coastline-condos` (optional; proxy defaults to this)
- [ ] Add Coastline origin to Railway `ALLOWED_ORIGINS` if the browser ever hits Railway directly (not required when using the Vercel proxy)
- [ ] Google Calendar for tour bookings + `TENANT_COASTLINE_CONDOS_GOOGLE_TOKEN_B64` (or shared default token)
- [ ] Escalation email routing verified (`hello@coastlinecondos.ec`)
- [ ] Smoke: site chat answers as Coastline; orchelix.com/try-esmi?tenant=coastline-condos&company=Coastline+Condos
- [ ] Voice / VAPI later if purchased

## Demo URL
`https://www.orchelix.com/try-esmi?tenant=coastline-condos&company=Coastline+Condos`
