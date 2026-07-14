# Calendar Setup — Orchelix-Managed (per new client)

Scope: this covers only the Google Calendar half of onboarding — creating the client's
calendar(s), sharing them, and wiring them into Esmi. For everything else (KB, pricing,
VAPI, website), see `sales/CLIENT_ONBOARDING_CHECKLIST.md`.

Policy: every client's calendar is created and owned under the **Orchelix Google account**
(the same one Esmi's own calendar already uses) — never the client's own account. See
`sales/CLIENT_ONBOARDING_CHECKLIST.md` Phase 2 for why. The client gets visibility via a
normal calendar-share invite, not OAuth.

## What you need before you start

- The client's business name + location name(s) (e.g. "Weston", "Keele")
- The client's business timezone
- (Optional) the Google email address they want the calendar shared to
- Logged in as the Orchelix Google account in your browser

## Steps

### 1. Create the calendar (one per location)

Google Calendar → left sidebar → **Other calendars** → **+** → **Create new calendar**.
Name it `<Client> — <Location>` (e.g. "Otro Nivel — Weston"). Set the timezone to the
client's. Click **Create calendar**. Repeat once per location.

### 2. Copy the Calendar ID

Hover the new calendar in the sidebar → **⋮ → Settings and sharing**. Scroll to
**Integrate calendar** → copy the **Calendar ID** (a long string ending
`@group.calendar.google.com`). This — not the display name — is what goes into config.

### 3. Share it with the client (optional but recommended)

Same Settings page → **Share with specific people or groups** → **Add people**. Enter
the client's existing Google email. Permission: "See all event details" (view-only) or
"Make changes to events" (if they'll add walk-ins themselves). Click **Send**. They get
an email invite — once accepted, it shows up automatically in their own Google Calendar
app next to their existing calendars. No OAuth, no password sharing.

### 4. Paste the Calendar ID into the tenant config

In `tenants/<slug>/config.json`:

```json
"locations": {
  "weston": {
    "calendar_id": "PASTE_THE_ID_FROM_STEP_2_HERE"
  }
}
```

### 5. Point the tenant at the existing Orchelix credential

`tenant_secret()` builds the Railway variable name as `TENANT_<SLUG>_<NAME>`, uppercased,
with hyphens turned into underscores — e.g. tenant `otro-nivel` → `TENANT_OTRO_NIVEL_*`.

Check which form the master Orchelix credential uses (`railway variable list` — names only,
values are sensitive) — as of 2026-07 it's three individual vars, not a single token blob:

```
TENANT_<SLUG>_GOOGLE_REFRESH_TOKEN  = <same value as master GOOGLE_REFRESH_TOKEN>
TENANT_<SLUG>_GOOGLE_CLIENT_ID      = <same value as master GOOGLE_CLIENT_ID>
TENANT_<SLUG>_GOOGLE_CLIENT_SECRET  = <same value as master GOOGLE_CLIENT_SECRET>
```

(If the master instead uses `GOOGLE_TOKEN_B64`, copy that single var as
`TENANT_<SLUG>_GOOGLE_TOKEN_B64` instead — same idea, whichever form is actually set.)

No new OAuth run is needed — the Orchelix account already has access to every calendar
created under it. **Never print secret values to a terminal you're sharing/logging** — copy
between Railway vars with a piped shell command (`railway run` + `variable set --stdin`) so
the value never appears in scrollback, not by reading and retyping it.

### 6. Verify

- Book one test appointment (web chat or phone) for the new tenant → confirm it lands
  on the correct calendar under the Orchelix account.
- If you sent a share invite, confirm the client accepted it and can see the calendar.

## What to tell the client

Send them this (all they need to do):

> We host and manage your booking calendar so it's always available — even if your own
> Google account ever changes — and you keep full visibility into every appointment.
> All we need is the Google email address you'd like it shared to. You'll get an email
> invite titled "[Client] — [Location]" — just accept it, and it'll appear automatically
> in your Google Calendar, right next to your other calendars. Nothing to install, no
> passwords to share.
