# Orchelix AI Consulting Knowledge Base - README

## Purpose
This folder contains the complete, up-to-date knowledge base for the **Orchelix AI Receptionist Agent**.  
Use these files to answer any question about the company, services, process, pricing, or branding with 100% accuracy and consistent tone.

## File Structure & When to Use Each File

| File                        | When the Agent Should Reference It                          | Priority |
|-----------------------------|-------------------------------------------------------------|----------|
| `00_company_overview.md`    | High-level “Who are you?” or “What do you do?” questions   | ★★★★★    |
| `01_tagline.md`             | Short, punchy descriptions or marketing copy               | ★★★★     |
| `02_about.md`               | Detailed “About us” or company story questions             | ★★★★★    |
| `03_services.md`            | “What services do you offer?” or “Tell me about the AI Receptionist” | ★★★★★    |
| `04_specialties.md`         | “What are your specialties?” or detailed capabilities      | ★★★★     |
| `05_ai_growth_audit.md`     | Questions about the free audit or how to get started       | ★★★★★    |
| `06_how_we_work.md`         | “How does the process work?” or timeline questions         | ★★★★     |
| `07_faq.md`                 | Any FAQ-style or objection-handling questions              | ★★★★★    |
| `08_testimonials.md`        | When asked for social proof or client results              | ★★★      |
| `09_case_studies.md`        | When asked for proof of results or examples                | ★★★      |
| `10_team.md`                | “Who is behind the company?” or founder questions          | ★★★      |
| `11_contact.md`             | Any contact, booking, or “how do I get started?” questions | ★★★★★    |
| `12_branding.md`            | To stay on-brand in tone, voice, and style                 | ★★★★★    |
| `13_pricing_tiers.md`       | Pricing, packages, or cost-related questions               | ★★★★     |

## How to Use This Knowledge Base (Instructions for the AI Receptionist Agent)

1. **Always start with the most relevant file** — especially `00_company_overview.md`, `02_about.md`, and `03_services.md`.
2. **Lead with the AI Receptionist Agent** — it is our flagship specialty. Mention it first and most often.
3. **Stay on-brand** — Use the calm, professional, benefit-first tone from `12_branding.md`.
4. **Push the free audit** — Every conversation should naturally lead toward booking the 15-minute AI Growth Audit.
5. **Be helpful and concise** — Answer directly, then offer the next clear step.
6. **Use LangGraph context** — Maintain conversation state and refer back to previous messages.

## Default Response Flow for the Agent
- Greeting / General inquiry → Reference `00_company_overview.md` + `03_services.md`
- “What do you do?” → Lead with the AI Receptionist Agent description
- Pricing questions → `13_pricing_tiers.md`
- “How do I start?” → Direct to free audit (`05_ai_growth_audit.md` + `11_contact.md`)
- Objections / FAQs → `07_faq.md`

**Important**: Never invent information. If something is not in these files, say you will check with Jorge and escalate.

