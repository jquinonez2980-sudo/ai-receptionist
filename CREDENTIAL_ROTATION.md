# Credential Rotation — REQUIRED

> **Why:** `credentials.json`, `token.json`, and `.env` are present in the
> working tree of the v0 repo. Treat them as compromised. Even if the repo
> is private today, anyone with historical access (past collaborators, CI
> systems, lost laptops, cached forks) has them. Rotate **now**, before any
> Phase 1 change ships.

> **⚠️ URGENT (added):** The `Dockerfile` previously baked **live** secrets into
> the image as base64 `ENV` lines — `GOOGLE_TOKEN_B64` (OAuth refresh token +
> client secret), `SENDGRID_API_KEY_B64`, and `VAPI_API_KEY_B64`. Base64 is
> encoding, not encryption. These values are in git history and in every built
> image layer. **All three must be rotated** (sections 2, 3, and the new
> section 4b below), and the history must be scrubbed (section 5). The secrets
> have been removed from the Dockerfile; they are now supplied as Railway
> environment variables at runtime.

This is a one-time operational task. Phase 1 code assumes it's done.

---

## 1. OpenAI

- [ ] Sign in to https://platform.openai.com/api-keys
- [ ] **Revoke** the key currently used by Esmi.
- [ ] **Generate a new key**. Set a usage limit ($X/day) appropriate to the workload.
- [ ] Add to:
  - Streamlit Cloud → app → Settings → Secrets → `OPENAI_API_KEY`
  - Local `.env` (gitignored)

## 2. Google Calendar (OAuth)

- [ ] Sign in to https://console.cloud.google.com → APIs & Services → Credentials.
- [ ] Find the OAuth client whose `client_secret` is in `credentials.json`.
- [ ] **Delete that OAuth client** (or rotate the client secret).
- [ ] Create a **new OAuth client** (Desktop or Web — same type as before).
- [ ] Re-run the local OAuth flow to obtain a fresh `token.json`.
- [ ] **Do NOT commit either file.** Verify `git status` shows them as ignored.
- [ ] For Streamlit Cloud: paste the new `token.json` contents as a single-line JSON string into `GOOGLE_TOKEN_JSON`.

## 3. SendGrid

- [ ] https://app.sendgrid.com → Settings → API Keys.
- [ ] **Delete** the existing key used by Esmi.
- [ ] **Create a new key** with the minimum scope needed (Mail Send only).
- [ ] Add to Streamlit secrets and `.env` as `SENDGRID_API_KEY`.

## 4. Twilio

- [ ] https://console.twilio.com → Account → API keys & tokens.
- [ ] **Rotate the Auth Token** for the project.
- [ ] If using API keys: delete the old key, generate a new one.
- [ ] Update webhook hosting env: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.

## 4b. VAPI

- [ ] https://dashboard.vapi.ai → Org settings → API Keys.
- [ ] **Delete/regenerate** the private API key (the one previously baked into the Dockerfile).
- [ ] Set the new value as a Railway env var `VAPI_API_KEY` (or `VAPI_API_KEY_B64`).
- [ ] While here, set a **Server URL Secret** on the assistant and the matching
      `VAPI_SERVER_SECRET` Railway var (gates `/voice/tools` and the `/health/*` diagnostics — see `api.py`).

## 5. Scrub git history

The above protects future leaks. To remove the secrets that already exist
in git history. Note the Dockerfile must be kept (with secrets stripped), so
use `--replace-text` for it and `--invert-paths` for the standalone secret files:

```bash
# Recommended: git-filter-repo (faster than BFG, official replacement)
pip install git-filter-repo

# In a fresh clone (not your working repo):
git clone <repo-url> esmi-clean
cd esmi-clean

# 1) Strip the standalone secret files from every commit in every branch
git filter-repo \
  --invert-paths \
  --path credentials.json \
  --path token.json \
  --path .env

# 2) Redact the base64 secrets that were committed inside the Dockerfile.
#    Put each leaked base64 blob on its own line in replacements.txt:
#      eyJ1bml2ZXJzZV9kb21haW4...==>REDACTED
#      U0cuYjdGTzdRSDd...==>REDACTED
#      M2YwNDQ5ZDUtZmVj...==>REDACTED
git filter-repo --replace-text replacements.txt

# Force push (coordinate with anyone who has a local clone)
git push --force --all
git push --force --tags
```

Rotation (sections 2–4b) is what actually neutralizes the leak — history
scrubbing only limits further spread. Rotate first.

After force-push, **everyone with a local clone must re-clone**. Old clones
still contain the secrets in their reflog.

## 6. Verify .gitignore

After applying the new `.gitignore`:

```bash
git rm --cached credentials.json token.json .env 2>/dev/null
git status                       # should show no secret files
git check-ignore -v credentials.json token.json .env  # should report rule hits
```

## 7. Postgres for the checkpointer

If you don't already have a Postgres instance:

- [ ] Provision one (Supabase free tier, Neon, Railway, or self-hosted).
- [ ] Create a database `esmi` and a user `esmi`.
- [ ] Put the connection string into `DATABASE_URL`.
- [ ] First app start will create the checkpoint tables automatically (`PostgresSaver.setup()`).

## 8. LangSmith

- [ ] Sign in to https://smith.langchain.com.
- [ ] Create a new project: **esmi-receptionist** (or whatever you prefer).
- [ ] Generate an API key.
- [ ] Set `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` in env.

---

## Done?

Run the smoke checklist in `PHASE1_PR_NOTES.md` → "Acceptance tests".
