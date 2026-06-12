# Spin Up a Personalized Demo (≈15 min before a call)

The move: walk into the demo having **already built Esmi for their business**. Prospect calls
*their own* AI receptionist. This is the close. Here's the repeatable runbook.

## What you need from the prospect (gather before)
- Company name + city
- 4–6 services they offer (scrape their website / Google Business Profile — 5 min)
- Business hours + timezone
- 2–3 FAQ answers (pricing ballpark, "do you offer free estimates?", service area)
- (Optional) their pricing — if unknown, use realistic placeholders

## Step 1 — Create the tenant config
Create `tenants/<slug>/config.json` (slug = lowercase, no spaces, e.g. `acme-hvac`):

```json
{
  "company_name": "Acme Air Conditioning",
  "business_tz": "America/New_York",
  "business_hours": [8, 18],
  "slot_minutes": 30,
  "emails": {
    "from": "you@orchelix.com",
    "booking_to": "you@orchelix.com",
    "escalation_to": "you@orchelix.com"
  },
  "sms_signature": "Acme Air Conditioning",
  "voice_default_summary": "Acme AC Service Call",
  "pricing": [
    { "name": "Service / Diagnostic Visit", "popular": true, "setup_from": 0, "monthly_from": 89,
      "best_for": "Any no-cool / no-heat call", "highlights": ["Same-day where available", "Upfront pricing"] }
  ]
}
```
> Point the demo emails at **your own** inbox so you see the booking/escalation land during the demo.

## Step 2 — Drop in their knowledge base
Create `tenants/<slug>/kb/services.md` — paste their services, service area, hours, FAQs,
"do you offer free estimates", warranties, brands serviced. 1 file is enough for a demo.
(Mirror the format in `tenants/acme/kb/services.md`.)

## Step 3 — Ship it
- `git add tenants/<slug>` → commit → push. Railway auto-deploys (~3 min).
- The tenant's KB index builds automatically on first message.

## Step 4 — Demo it

**Web chat (LIVE — just send a link):**
`https://www.orchelix.com/try-esmi?tenant=<slug>` — Esmi answers as their business (their KB,
pricing, persona) in EN/ES. Add `&company=Acme%20HVAC` to control the exact display name in the
welcome + heading ("Talk to Acme HVAC's receptionist — right now"). If you omit `company`, it
prettifies the slug (`acme-hvac` → "Acme Hvac"). No `?tenant` = the normal Orchelix demo.
- Example: `orchelix.com/try-esmi?tenant=acme-hvac&company=Acme+Air+Conditioning`
- This is the move: drop the tenant folder (Steps 1–3), text the prospect the link, done.

**Voice (the magic):** the live 561 number currently answers as Orchelix (default tenant). Two ways
to run a voice demo:
- *Fastest:* use the existing number as a "here's what yours sounds like" voice demo, paired with
  their **branded web chat** (their name/services/pricing). Covers the wow + the personalization.
- *Fully branded voice:* provision a VAPI assistant for the prospect and add its id to the tenant's
  `vapi.assistant_ids` (the backend already maps it via `resolve_vapi_tenant`). Do this for hot,
  high-value prospects worth the extra 20 min.

## Step 5 — On the call
1. "I built Esmi for [Business] last night. Here's the link / number — go try to stump it."
2. Have them **book an appointment** → show it hit your calendar + the escalation email.
3. Have them **ask in Spanish** → watch their face.
4. Recap their ROI numbers (use `roi-calculator.html`) → close.

## After the demo
- If they buy: the tenant's already built. Swap demo emails → their real inbox, connect their
  real Google Calendar (`TENANT_<SLUG>_GOOGLE_TOKEN_B64` on Railway), set their real pricing/KB,
  point their phone number / VAPI assistant at it. You're live same week.
- If not: keep the tenant folder; the build cost you 15 minutes and you can revive it.

## Cleanup
Delete `tenants/<slug>/` (and the Railway `TENANT_<SLUG>_*` vars, if any) for dead demos so the
registry stays tidy. Secrets are env-only; nothing sensitive lives in the tenant folder.
