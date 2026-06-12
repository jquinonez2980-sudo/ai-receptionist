# Esmi Go-To-Market Kit

Sales assets for selling Esmi (the AI receptionist). Beachhead: **South Florida home services,
bilingual (EN/ES)**. Sequence: founder-led sales → first 5 founder customers (cheap, for proof)
→ raise price → hire to scale. Full strategy is in the chat plan; this folder is the toolkit.

## Built
- **`roi-calculator.html`** — branded, self-contained ROI calculator. Open in any browser or
  deploy as a static page. Use it live in every demo: enter the prospect's real numbers, let
  them watch the lost-revenue figure climb. Presets for Home Services / Med Spa / Dental.
- **`outreach-scripts.md`** — cold call (after-hours opener), voicemail, 4-email sequence,
  IG/LinkedIn DMs, referral ask, agency reseller pitch. Copy-paste ready.
- **`demo-tenant-checklist.md`** — spin up a prospect-branded Esmi in ~15 min before a call,
  using the multi-tenant backend. Turns "I built you a working version" into a repeatable move.
- **Branded demo links — LIVE.** `orchelix.com/try-esmi?tenant=<slug>&company=<Name>` serves a
  prospect-branded demo (their KB/pricing/persona) on the live site. Drop a `tenants/<slug>/`
  folder, text the link. (Frontend: `orhelix-website` commit `53169b4`.)

## How to use the ROI calculator
- **Quick:** double-click `roi-calculator.html` — runs in the browser, no server.
- **Share/host:** drop it on Netlify/Vercel/any static host, or add a `/roi` route on
  orchelix.com. Then text the link to prospects after a call.

## Next assets (ask Claude to build)
1. **One-page sales sheet** (PDF/print) — problem, solution, ROI, pricing, proof.
2. **Demo video script** — the 2-minute Loom you put on the landing page and in cold emails.
3. **Objection-handling one-pager** — expanded from the plan's cheat sheet.
4. **Case study template** — fill after founder win #1.
5. **Founder-deal proposal template** + Stripe setup notes (billing).
6. **Landing-page copy** refresh for the try-esmi page (headline, ROI, social proof, CTA).

## Done
- ✅ **`?tenant=` branded demo link** — live on orchelix.com/try-esmi (see above).
