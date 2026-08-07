# Esmi Product Design Spec
**Voice Studio first · Dashboard · Public try-esmi**

**Brand**: Orchelix
**Primary colors**: Navy `#0A2540` + Teal `#00B8D4`
**Stack notes**: Grounded in the existing `app.orchelix.com` platform plan and VAPI / ElevenLabs voice delivery.
**Product feel**: A capable bilingual teammate — warmer than Linear, as polished as Stripe, with Smith.ai-level voice transparency.

> **Before building Voice Studio, read Section 12.** `voice_id`/`speed`/`language_pref` now persist and sync to the real VAPI assistant for two of the three live tenants (Section 12.1) — but the dashboard UI described in Section 3 to drive them still doesn't exist, the sync is a manual script step (not wired to "Save"), `default`/Orchelix is deliberately excluded from sync, and `greeting` exists on `TenantConfig` but is still not wired into the live prompt. Section 12 lays out what's done and what's left.

---

## 1. Design System

### Color Tokens
| Token     | Value     | Use                                      |
|-----------|-----------|-------------------------------------------|
| navy-950  | `#0A2540` | Primary text, sidebar, strong CTAs       |
| navy-800  | `#0F3A5F` | Hover states, secondary surfaces         |
| teal-500  | `#00B8D4` | Accent, play buttons, focus rings, live indicators |
| teal-50   | `#E6F9FC` | Soft highlight backgrounds               |
| sand-50   | `#F7F6F3` | Page background (warmer than pure gray)  |
| slate-100 | `#F1F5F9` | Cards nested on sand                     |
| slate-500 | `#64748B` | Helper text                              |
| success   | `#0D9488` | Booked / connected                       |
| warning   | `#D97706` | Needs attention                          |
| danger    | `#DC2626` | Escalated / missed                       |

**Rationale**: Orchelix already owns teal as "intelligence + calm." Warm neutrals with sand so Esmi does not feel clinical.

### Typography
- UI: Inter or Geist Sans
- Display / wordmark accents: Same family, semibold
- Transcripts: 15–16px, 1.55 line-height, generous padding

### Motion
- Play button: 150ms scale + teal glow
- Waveform bars: 60fps CSS / `requestAnimationFrame` (avoid flashy Lottie)
- Language toggle: soft crossfade

### Language
- Global `EN | ES` toggle in header (dashboard + marketing)
- Voice Studio stores per-language greeting + preferred voice
- Default for South Florida / Canadian bilingual clients: both languages enabled

---

## 2. Information Architecture — Esmi Dashboard

```
app.orchelix.com
├── /                  Overview (Home)
├── /calls             Call history + recordings
├── /chats             Web chat sessions
├── /leads             Lead inbox
├── /appointments      Calendar-synced bookings
├── /voice             Voice Studio ← hero product surface
│   ├── Personality
│   ├── Greeting
│   ├── Quality Studio
│   └── Live test
├── /knowledge         Knowledge Base & Playbooks
├── /scheduling        Calendar + booking rules
├── /analytics         Trends & language mix
├── /integrations      Calendar, CRM, Zapier…
├── /settings
│   ├── Business
│   ├── Hours & after-hours
│   ├── Notifications
│   ├── Phone numbers
│   └── Team & billing
└── /onboarding/*      First-run wizard (gated until complete)
```

**Multi-tenant note**: as of this spec, three tenants are live (Orchelix `default`, `otro-nivel`, `coastline-condos`), each with its own VAPI assistant + phone number. This dashboard is per-tenant — see Section 12.3 for how a session resolves which tenant it's editing.

### Navigation Labels
| EN                  | ES                 |
|---------------------|--------------------|
| Overview            | Resumen            |
| Calls               | Llamadas           |
| Voice & Personality | Voz y personalidad |
| Knowledge           | Conocimiento       |
| Scheduling          | Agenda             |
| Analytics           | Analítica          |
| Integrations        | Integraciones      |
| Settings            | Configuración      |

### Persistent Chrome
- Left sidebar (collapses to icons on mobile)
- Top bar: tenant name, Esmi status pill (`Live` / `Test mode` / `Setup`), `EN|ES`, avatar
- Floating "Test Esmi" button (bottom-right on desktop) → Quality Studio or test call

---

## 3. Voice Studio (Highest Priority)

### 3.1 Page Goal
> "Hear exactly how Esmi will greet your callers — change voice, speed, or greeting, and preview before anything goes live."

Smith.ai wins on transparency. Esmi should win on bilingual personality + scenario testing + calendar-aware quality checks.

*Fidelity note: this "hear exactly" promise is achievable because the live voice pipeline already runs ElevenLabs (via VAPI). It only holds if the preview player calls the same ElevenLabs voice ID that the live VAPI assistant uses — see Section 12.2 for the mechanism.*

### 3.2 Desktop Layout
```
┌──────────────────────────────────────────────────────────────────┐
│ Voice & Personality                    [EN|ES]          [Save]   │
│ "How Esmi sounds on the phone"                                   │
├────────────────────────────┬─────────────────────────────────────┤
│ LEFT (40%)                 │ RIGHT (60%) — sticky preview        │
│                            │                                     │
│ Voice library grid         │ ┌─ Esmi Preview Player ───────────┐ │
│ • Avatar + name            │ │ [▶ Preview with current greeting]│ │
│ • Personality chips        │ │ ~~~~ waveform ~~~~  0:04 / 0:12 │ │
│ • EN / ES native badge     │ │ Voice: Sofia · 1.0× · English   │ │
│                            │ └─────────────────────────────────┘ │
│ Speed slider 0.85×–1.15×   │                                     │
│                            │ Greeting editor (per language)      │
│ Language default           │ [EN greeting] [ES greeting]         │
│ ○ Detect automatically     │ char count · "sounds natural" tip   │
│ ○ Prefer English first     │                                     │
│ ○ Prefer Spanish first     │ Sample scripts (quick-load chips)   │
│                            │                                     │
│ Transfer number            │ [Run Quality scenario ▾]            │
│ After-hours behavior       │ [Call my Esmi number]               │
└────────────────────────────┴─────────────────────────────────────┘
```

**Mobile**: Stack preview player first (sticky mini-player), then voice cards, then greeting.

### 3.3 Recommended Voice Library (8 voices)
| ID     | Name   | Languages feel          | Personality label      | Best for                        | Tagline                          |
|--------|--------|--------------------------|-------------------------|----------------------------------|------------------------------------|
| ava    | Ava    | EN-native, clear ES     | Calm & Professional    | Law, accounting, consulting     | Steady, precise, never rushed    |
| mateo  | Mateo  | Bilingual warm          | Warm & Friendly        | Home services, barbershops      | Like a great front-desk person   |
| sofia  | Sofia  | ES-native + fluent EN   | Confident & Efficient  | High-volume clinics, dental     | Gets to the point, still kind    |
| elena  | Elena  | Soft EN/ES              | Soft & Caring          | Medspa, dental, wellness        | Gentle, reassuring, patient      |
| lucas  | Lucas  | EN-native               | Clear & Direct         | Real estate, B2B services       | Crisp, modern, low fluff         |
| camila | Camila | ES-native (LatAm)       | Bright & Welcoming     | South Florida bilingual shops   | Energetic without being salesy   |
| noah   | Noah   | EN-native               | Neutral & Trustworthy  | Multi-location, franchises      | Default "safe" professional      |
| isabel | Isabel | ES-native + EN          | Polished & Warm        | Premium real estate, clinics    | Boutique-hotel receptionist energy |

*Each `EsmiVoice.providerVoiceId` (Section 4) must map to a real ElevenLabs voice ID already validated against Deepgram's EN/ES flux transcriber in VAPI — don't add a library entry until that pairing has been test-called once (Quality Studio "Spanish caller" scenario is the sanity check).*

**UI badges on each card**
- English / Spanish / Bilingual
- Personality chip
- "Popular" on Ava + Sofia
- Selected ring: 2px teal + soft teal glow

### 3.4 Voice Studio UI Copy

**Page header**
- EN: Voice & Personality
  Sub: Choose how Esmi sounds, then preview before you go live.
- ES: Voz y personalidad
  Sub: Elige cómo suena Esmi y escúchala antes de activarla.

**Voice library**
- Title: Esmi's voice / La voz de Esmi
- Helper: Pick a voice that matches your brand. You can change this anytime.

**Speed**
- Label: Speech speed / Velocidad al hablar
- Marks: Slower · Natural · Faster
- Range: 0.85× – 1.15× (default 1.0×)
- Helper: Most businesses sound best at Natural (1.0×).

**Primary CTA**
- Button: Preview with current greeting
  ES: Escuchar con el saludo actual
- Loading: Preparing preview… / Preparando vista previa…
- Playing: Playing preview / Reproduciendo
- Secondary: Play sample script / Reproducir guion de ejemplo

**Greeting editor**
- Label EN: Phone greeting (English)
- Label ES: Saludo telefónico (español)
- Placeholder EN: Thanks for calling {Business Name}, this is Esmi. How can I help you today?
- Placeholder ES: Gracias por llamar a {Business Name}, habla Esmi. ¿En qué le puedo ayudar?
- Helper: Keep it under ~12 seconds spoken (~30–40 words). Callers hang up on long intros.
- Live tip when long: This greeting may feel long on the phone. Try tightening it, then re-preview.
- Insert chips: `{Business Name}` `{Hours}` `{Location}`

**Sample script chips**
1. Standard greeting
2. After-hours greeting
3. Bilingual open ("I can help in English or Spanish")
4. Booking flow sample
5. Escalation sample

**Save / publish**
- Primary: Save voice settings / Guardar configuración de voz
- Success toast: Saved. New callers will hear this voice.
- Unsaved banner: You have unsaved voice changes · Preview reflects the draft
- Caution (live change): You're changing how live callers hear Esmi. Preview first, then save.

**Empty / first-time**
- You haven't chosen a voice yet. Start with Sofia or Ava — most bilingual teams love one of these.

### 3.5 Premium Voice Preview Player

**States**: `idle` | `loading` | `playing` | `paused` | `error`

**Visual anatomy**
1. Large teal play/pause circular button (48–56px)
2. Animated waveform (12–24 bars)
3. Progress bar + elapsed / duration
4. Meta line: `{Voice name} · {speed}× · {language}`
5. Caption: "Preview with current greeting"
6. Optional "Draft" pill when greeting/voice differs from last saved

**Behavior**
- Click Preview → POST TTS preview → stream or signed URL
- Changing voice, speed, or greeting invalidates current audio and shows "Outdated — re-preview"
- Keyboard: Space to play/pause when focused
- Mobile: larger hit targets; waveform can simplify to 8 bars

**Do not**
- Autoplay on page load
- Preview without showing which greeting language is playing
- Hide the re-preview requirement after edits

### 3.6 Quality Studio

**Tab**: Quality Studio / Estudio de calidad

| Scenario                    | What happens                                      | Success signal                          |
|-------------------------------|------------------------------------------------------|--------------------------------------------|
| New lead books appointment  | Asks for availability → offers slots → books      | Disposition `booked`                    |
| FAQ only                    | Pricing / hours / services questions              | Correct KB answer, no false book        |
| Spanish caller              | Full conversation in Spanish                      | Language detected, Spanish greeting path|
| After hours                 | Outside business hours                            | Correct after-hours behavior            |
| Angry / urgent              | Escalation language                               | `escalate_to_human` / transfer path     |
| Existing client reschedule  | Needs booking lookup + security flow              | Correct security steps                  |

**UI**
- Left: scenario picker + "Custom script"
- Center: live transcript (caller vs Esmi, timestamps)
- Right / bottom: audio of AI turns or TTS playback
- Footer: Run scenario · Replay last run · Call my live number

**Copy**
- Header: Practice calls without risking a real customer
- Helper: These use your current draft voice, greeting, and knowledge base.
- Result banner example: Esmi booked an appointment · 2 min 14 sec · Spanish
- Soft fail: Esmi didn't escalate when the caller asked for a human. Review escalation rules.

*Scenario transcripts should be produced by driving the same `/voice/tools` webhook path a real VAPI call hits (Section 12.2), not a separate simulated agent — otherwise a scenario can pass in Quality Studio while the live agent behaves differently.*

### 3.7 Real Test Call
- Button: Call my Esmi number / Llamar a mi número de Esmi
- Shows large phone number + copy button + `tel:` link on mobile
- Note: This is a real call to your Esmi line. It counts toward usage.
- After call: deep-link to that call in `/calls/{id}` with toast "Your test call is ready to review"

---

## 4. Component Structure (Voice Studio)

```
app/(dashboard)/voice/page.tsx
components/voice/
  VoiceStudio.tsx
  VoiceLibrary.tsx
  VoiceCard.tsx
  SpeechSpeedSlider.tsx
  GreetingEditor.tsx
  LanguagePreference.tsx
  VoicePreviewPlayer.tsx      ← THE premium player
  Waveform.tsx
  SampleScriptChips.tsx
  QualityStudio.tsx
  ScenarioRunner.tsx
  LiveTranscript.tsx
  TestCallPanel.tsx
  UnsavedVoiceBanner.tsx
lib/voice/
  voices.ts
  useVoicePreview.ts
  types.ts
```

### Voice metadata shape
```ts
export type EsmiVoice = {
  id: string;
  name: string;
  personality: string;       // "Calm & Professional"
  personalityEs: string;
  tagline: string;
  taglineEs: string;
  languages: ("en" | "es")[];
  bilingual: boolean;
  popular?: boolean;
  gender: "feminine" | "masculine" | "neutral";
  providerVoiceId: string;   // ElevenLabs voice id — never show raw to user
};
```

### Preview API Contract
```
POST /api/platform/voice/preview
{
  "tenant_id": "otro-nivel",
  "voice_id": "sofia",
  "speed": 1.0,
  "language": "es",
  "text": "Gracias por llamar a..."
}
→ {
  "url": "https://...signed...",
  "duration_sec": 8.4,
  "cache_key": "..."
}
```
`tenant_id` is required (added to the original contract) — see Section 12.3 on why preview and save must always be tenant-scoped, never global. Cache previews by hash of `(tenant_id, voice_id, speed, language, text)` for ~24h.

---

## 5. Rest of the Dashboard

### 5.1 Overview / Home
- KPI strip: Calls answered · Appts booked · Leads captured · After-hours calls
- After-hours spotlight: "Esmi answered 34 calls after hours this month"
- Language mix donut (EN vs ES)
- Recent activity (last 5 calls/chats with disposition badges)
- Setup checklist (until complete)
- Usage meter
- Disposition badge colors: Booked = teal · Info = slate · Escalated = amber · Missed = red · Voicemail = violet

### 5.2 Calls
- Table + detail drawer
- Columns: Time, Caller, Duration, Language, Outcome, Actions (Play)
- Detail: AI summary (3 bullets), full transcript, waveform recording player, tags, "Create playbook from this call"
- Filters: date, outcome, language, has recording

### 5.3 Knowledge Base & Playbooks
- FAQ list (question / answer / language)
- Services & intake questions
- Escalation triggers
- Routing rules
- Test box: "Ask Esmi a question" against sandbox

*Pricing is explicitly out of scope for this page's free-text FAQ editor. Prices must only ever be edited through a dedicated Pricing panel that writes to the tenant's `pricing` field (or the `default` tenant's `_PRICING` constant) — never into the general KB. See Section 12.4.*

### 5.4 Scheduling
- Connection status (Google Calendar / Orchelix-managed)
- Booking windows, buffers, service durations
- Confirmation SMS/email toggles

### 5.5 Analytics
- Call volume trend
- Peak hours heatmap
- Booking conversion rate
- Language mix over time
- Lead quality score

### 5.6 Integrations
Cards for Google Calendar, Calendly, HubSpot, Clio, Zapier, webhooks — each with Connected / Connect state and one-line benefit.

### 5.7 Settings
Business profile, hours, after-hours script, notifications, phone numbers + forwarding instructions, team, billing.

---

## 6. Public try-esmi Page (orhelix.com/try-esmi)

### Goals
- Hear Esmi without signup (voice preview = primary conversion lever)
- Offer live demo lines by industry
- Capture trial signup
- Keep existing chat demo as secondary path

### Page Structure
1. Nav + EN|ES
2. Hero — "Hear Esmi before you hire her"
3. Interactive Voice Preview (public Voice Studio lite)
4. Live demo phone numbers (by industry)
5. Recorded demo conversations
6. How it works (3 steps)
7. Trust strip
8. Final CTA
9. Optional chat widget
10. Footer

### Hero Copy
**EN**
- H1: Hear Esmi before you hire her
- Sub: A bilingual AI receptionist that answers every call, books the calendar, and hands off when a human should take over — in English or Spanish.
- Primary CTA: Preview Esmi's voice
- Secondary CTA: Start free trial
- Micro: Live in as little as 48 hours · No long contracts

**ES**
- H1: Escucha a Esmi antes de contratarla
- Sub: Una recepcionista con IA bilingüe que contesta cada llamada, agenda citas y te pasa la llamada cuando debe atender un humano — en inglés o español.

### Public Interactive Voice Preview
- Same player component as dashboard
- Fixed sample greetings per industry + language (control cost/abuse)
- 4–6 public voices
- Rate-limit by IP
- Watermark: Sample only — your Esmi will use your business name and services

*This preview must call a public, unauthenticated variant of the Section 4 preview endpoint with its own stricter rate limit — do not reuse the tenant-scoped dashboard endpoint, since it has no `tenant_id` to bill usage against and must never touch a real tenant's ElevenLabs quota.*

### Live Demo Lines
- Title: Call a live Esmi right now
- Sub: Real conversations. No sales pitch on the line — just the product.

### Trust Strip
- Bilingual by design — English & Spanish
- Books directly into your calendar
- Live in 48 hours
- Logos + 1–2 testimonials

### Final CTA
- Create your free Esmi
- Sub: Pick a voice, preview your greeting, and test a real call — before you forward a single customer.

---

## 7. Onboarding

Align with existing platform wizard. Inject voice delight at the moment of highest doubt.

### Wizard Steps
1. Business basics — name, industry template, timezone
2. Knowledge — website scrape + 3 FAQs
3. Voice & greeting ← cannot skip without preview
4. Calendar
5. Test drive — call your number + optional chat
6. Go live — forwarding instructions

### Step 3 Microcopy (Critical)
- Title: Make Esmi sound like your front desk
- Body: Choose a voice and write a short greeting. Then hit Preview — you'll hear exactly what callers hear.
- Checklist gate:
  - [ ] Voice selected
  - [ ] Greeting previewed at least once
  - [ ] (Optional) Spanish greeting added
- Primary button disabled until first successful preview.
- Tooltip: Preview your greeting once so you know how Esmi sounds

### Step 5
- Your Esmi number is ready
- Call it now — we'll open the recording when you hang up.

---

## 8. Key Microcopy Bank (EN)

| Context | Copy |
|---|---|
| Play (studio) | Preview with current greeting |
| Play (recording) | Play call recording |
| Stale audio | Re-preview to hear your latest changes |
| Save | Save voice settings |
| Saved | Saved. New callers will hear this voice. |
| Test call | Call my Esmi number |
| Quality run | Run scenario |
| Bilingual tip | Add a Spanish greeting so Spanish callers feel at home from the first second. |
| After-hours | When you're closed, Esmi can take a message, book for next open slot, or transfer emergencies. |
| Trust (public) | Hear Esmi before you hire her |
| CTA | Start free trial |
| Speed helper | Natural (1.0×) works best for most businesses |

Spanish mirrors should be written by a LatAm-native copy pass (prefer tú/usted consistency per vertical).

---

## 9. Component Inventory

**Shared**: AppShell, Sidebar, StatusPill, LangToggle, KpiCard, DispositionBadge, AudioPlayer base, Waveform, TranscriptView, EmptyState, Coachmark

**Voice**: Full set listed in Section 4

**Calls / Overview**: CallsTable, CallDetailDrawer, AfterHoursSpotlight, SetupChecklist

**Marketing try-esmi**: TryHero, PublicVoicePreview, DemoLineCards, RecordedDemoGrid, TrustStrip, FinalCta

---

## 10. Implementation Priority

| Priority | Ship | Why |
|---|---|---|
| P0 | Backend: `voice_id`/`speed`/`language_pref` fields + VAPI assistant sync (Section 12.1–12.2) | Nothing in Section 3 can go live without this |
| P0 | VoicePreviewPlayer + preview API + voice library + greeting editor | Product differentiator |
| P0 | try-esmi public preview (lite) | Sales conversion |
| P1 | Quality Studio scenarios | Trust / reduce support |
| P1 | Calls list + recording playback | Retention |
| P2 | Full dashboard IA pages | Platform completeness |
| P2 | Onboarding gate: must preview once | Behavior design |
| P3 | Advanced integrations marketplace | Expansion |

*The backend field/sync work was not in the original priority table. It is listed first because every other P0 item in Voice Studio assumes it already exists. **Status: done for `otro-nivel` and `coastline-condos` as of 2026-08-06** (Section 12.1) — `default`/Orchelix and wiring "Save" to trigger the sync automatically remain open.*

---

## 11. Design Principles

1. Hear before commit — every voice or greeting change is one click from audio.
2. Draft vs live is visible — never surprise a business with a silent config change.
3. Bilingual is first-class — not a toggle buried in settings.
4. Warm professional — sand backgrounds, teal play actions, human names on voices.
5. Transparent usage — test calls and previews labeled honestly.
6. Same player DNA — marketing, onboarding, studio, and call recordings share one audio UX language.

---

## 12. Integration Notes — Backend Reality Check

This section exists because the sections above describe a UI that assumes backend surfaces that partially don't exist yet in the `ai-receptionist` repo. Read this before scoping any Voice Studio sprint.

### 12.1 Voice/speed persist and sync to VAPI — for two tenants so far

**Status as of 2026-08-06:** the three backend pieces below are all built and live. `greeting` is the one field from the original version of this section still unwired — see the note at the end.

`tenants.py`'s `TenantConfig` now has `voice_id`, `speed`, and `language_pref` fields alongside `greeting` (whose own code comment is unchanged and still accurate: *"Not yet wired into the live prompt — `prompts/esmi_system.md` and `tenants/<id>/prompts/` remain authoritative until a later change compiles this into the prompt template. Stored now so the settings UI has somewhere durable to write."*).

What exists:
1. **Fields** — `TenantConfig.voice_id` / `.speed` / `.language_pref` (`tenants.py`), following the same config.json → DB-row → in-process-cache pattern `greeting` already used.
2. **Write path** — `PUT /platform/config` (`platform_api/config.py`) validates (`speed` clamped 0.85–1.15, `language_pref` restricted to `auto`/`en`/`es`) and saves these fields into the `tenant_configs` Postgres table as a new published version, same mechanism as every other self-serve field.
3. **VAPI sync** — `scripts/sync_vapi_voice.py` resolves a tenant's saved voice via the *live* `GET /platform/config` endpoint (not local `tenants.load_tenant()` — that path is forced file-only when run off-Railway and would silently miss a value only saved to Postgres), maps the short id through `voice_library.VOICE_LIBRARY` (single source of truth, shared with the preview endpoint in 12.2) to a real ElevenLabs voiceId, and PATCHes only `voice.voiceId`/`voice.speed` onto the tenant's VAPI assistant — every other key already on the assistant's `voice` object is read back and preserved untouched. Dry-run by default (prints the exact PATCH payload, calls no mutating endpoint); `--apply` PATCHes and re-GETs to verify.

**Remaining gaps — read before building the Section 3 UI:**
- **Hard allow-list, not every tenant.** `SYNC_ALLOWED_TENANTS = {otro-nivel, coastline-condos}` in `scripts/sync_vapi_voice.py`. `default`/Orchelix (the live production number) is deliberately excluded until the sync path earns more track record — any "Save voice settings" UI must not assume it works for every tenant yet.
- **Manual, not automatic.** The dashboard's "Save voice settings" action (step 2 above) only writes config — it does not itself trigger step 3. Today a human runs `railway run python scripts/sync_vapi_voice.py --tenant <id> --apply` after saving. Wiring "Save" to call this automatically (or an admin-only "Push to VAPI" button, gated by the same allow-list) is unbuilt — Section 3's "Save" copy should not promise callers hear the change immediately until that exists.
- **`greeting` is still unwired** into the live prompt — unchanged from the original note above.

**Live-verified:** `otro-nivel` and `coastline-condos` both confirmed via `--show-current` after `--apply` — live VAPI `voice.voiceId` is `hpp4J3VqNfWAUOO0d1Us`, `voice.speed` is `1.0`, matching their saved `voice_id: "esmi-default"`.

### 12.2 Preview fidelity: build it on ElevenLabs directly, not through VAPI

The "hear exactly how Esmi will greet your callers" promise (3.1) is achievable, because the live pipeline is already ElevenLabs + Deepgram behind VAPI — but VAPI itself has no simple text-to-audio preview endpoint. The preview API (Section 4) should call ElevenLabs' TTS API directly with the same `providerVoiceId` the live VAPI assistant is configured with, so the same voice ID is the single source of truth for both preview and production. Quality Studio scenario runs (3.6) should instead go through the real `/voice/tools` webhook path so a scenario that passes in Quality Studio reflects the live agent, not a simulated one.

### 12.3 Multi-tenant, not single-tenant

Three tenants are live today (`default`/Orchelix, `otro-nivel`, `coastline-condos`), each with its own VAPI assistant + phone number, resolved via `resolve_vapi_tenant()` matching `assistantId`/`phoneNumberId` in the webhook payload. The dashboard IA (Section 2) reads as single-tenant. Every write in Voice Studio — save, preview, publish — must carry `tenant_id` explicitly (added to the preview API contract in Section 4) and the dashboard needs a tenant switcher or tenant-scoped login, not assumed in the current spec. Also relevant: `tenant_is_active()` is the single traffic gate (onboarding approval + billing status) — Voice Studio changes for a tenant that isn't `active` should save as draft but the "You're changing how live callers hear Esmi" warning copy (3.4) should only fire for tenants actually serving traffic.

### 12.4 Pricing must not be editable from the Knowledge Base page

`tools.py` is explicit: pricing is answered from the structured `_PRICING` constant (or a tenant's `pricing` override in `TenantConfig`), never from the KB/vector store, specifically because RAG chunking can split a price table and surface a wrong figure — CLAUDE.md hard rule 2 requires `_PRICING` and `orchelix_knowledge_base/13_pricing_tiers.md` to stay in sync by hand. Section 5.3's generic FAQ editor must not become a place to edit prices. If/when the dashboard gets a pricing editor, it needs its own panel that writes to the same `pricing` field the agent's `get_pricing` tool actually reads — not a KB doc.

---

*End of Spec*
