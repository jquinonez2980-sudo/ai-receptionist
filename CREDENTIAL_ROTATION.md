# Credential Rotation — COMPLETE ✅

> Rotation completed 2026-06-15. All keys below were revoked and replaced.
> New secrets live in Railway environment variables only — not in any tracked file.

---

## 1. OpenAI

- [x] Sign in to https://platform.openai.com/api-keys
- [x] **Revoke** the key currently used by Esmi.
- [x] **Generate a new key**. Set a usage limit ($X/day) appropriate to the workload.
- [x] Add to:
  - Local `.env` (gitignored)

## 2. Google Calendar (OAuth)

- [x] Sign in to https://console.cloud.google.com → APIs & Services → Credentials.
- [x] Find the OAuth client whose `client_secret` is in `credentials.json`.
- [x] **Delete that OAuth client** (or rotate the client secret).
- [x] Create a **new OAuth client** (Desktop or Web — same type as before).
- [x] Re-run the local OAuth flow to obtain a fresh `token.json`.
- [x] **Do NOT commit either file.** Verify `git status` shows them as ignored.

## 3. SendGrid

- [x] https://app.sendgrid.com → Settings → API Keys.
- [x] **Delete** the existing key used by Esmi.
- [x] **Create a new key** with the minimum scope needed (Mail Send only).
- [x] Add to `.env` as `SENDGRID_API_KEY`.

## 4. Twilio

- [x] https://console.twilio.com → Account → API keys & tokens.
- [x] **Rotate the Auth Token** for the project.
- [x] Update Railway env: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.

## 4b. VAPI

- [x] https://dashboard.vapi.ai → Org settings → API Keys.
- [x] **Delete/regenerate** the private API key.
- [x] Set the new value as a Railway env var `VAPI_API_KEY`.
- [x] `VAPI_SERVER_SECRET` Railway var set (gates `/voice/tools`).

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
