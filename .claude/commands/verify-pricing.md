---
description: Verify the live Esmi backend's two pricing behaviors — Orchelix's own pricing stays gated behind lead capture, a client tenant's service pricing comes back exact via get_pricing
allowed-tools: Bash(curl *), Bash(python *), Bash(railway *)
---

Verify pricing behavior on the **live** Esmi backend (`ai-receptionist-production-5375`).
Two DIFFERENT, both-correct behaviors must hold — don't conflate them:

- **Case 1 — "How much does Esmi cost?" (no tenant / the default Orchelix tenant).**
  Per `prompts/esmi_system.md`, this question must NEVER be answered with a number from
  `get_pricing` or memory — it always gets the canned lead-capture deflection (ask for
  name + best way to reach them, promise Jorge will follow up). A reply containing a
  dollar amount here is a REGRESSION, not a pass.
- **Case 2 — a client tenant's own customer asking about THEIR service prices**
  (e.g. otro-nivel's barbershop menu). `get_pricing` reads `tenant.services` and the
  agent must answer with the exact canonical number — no dollar amount here is the
  regression.

`/chat` requires `X-Chat-Secret` (`CHAT_PROXY_SECRET` in Railway) since the proxy-only
hardening landed — pull it from Railway rather than hardcoding it, and never print it.

## Setup — get the chat secret without echoing it

```bash
export CHAT_SECRET=$(railway variables --service ai-receptionist --kv 2>/dev/null | grep '^CHAT_PROXY_SECRET=' | cut -d= -f2-)
```

## Case 1 — Orchelix's own pricing must stay gated

```bash
curl -sN -X POST https://ai-receptionist-production-5375.up.railway.app/chat \
  -H "Content-Type: application/json" \
  -H "X-Chat-Secret: $CHAT_SECRET" \
  -d '{"message":"How much does each package cost? Give exact setup and monthly prices.","thread_id":"verify-pricing-case1-REPLACE_WITH_DATE"}' \
  --max-time 90 -o /tmp/esmi_pricing_case1.sse
```

Reconstruct the reply from `token` events and check it:

```bash
python - <<'PY'
import json, re
text = ""
for line in open("/tmp/esmi_pricing_case1.sse", encoding="utf-8"):
    if line.startswith("data: "):
        try:
            ev = json.loads(line[6:])
        except Exception:
            continue
        if ev.get("type") == "token":
            text += ev.get("content", "")
has_dollar_amount = re.search(r"\$\d", text) is not None
asks_for_contact = any(w in text.lower() for w in ("name", "contact", "reach", "email", "phone"))
print("FAIL — dollar amount leaked (regression: get_pricing/memory used for Esmi's own price)"
      if has_dollar_amount else "PASS — no dollar amount disclosed")
print("PASS — deflects to lead capture" if asks_for_contact else
      "FAIL — no lead-capture ask found (reply may have just refused with no next step)")
PY
```

## Case 2 — a real client tenant's service pricing must be exact

Uses otro-nivel, which has two locations (Weston, Keele) with different Fade prices in
its live tenant.services — checked structurally, NOT against literal dollar amounts:
those prices are self-serve editable from the dashboard Settings page, so a client
changing their own menu must never make this check "fail". What must hold is that
per-location price data is actually reaching the reply — at least two distinct dollar
amounts, one per location.

```bash
curl -sN -X POST https://ai-receptionist-production-5375.up.railway.app/chat \
  -H "Content-Type: application/json" \
  -H "X-Chat-Secret: $CHAT_SECRET" \
  -H "X-Tenant-Id: otro-nivel" \
  -d '{"message":"How much is a fade at each location?","thread_id":"verify-pricing-case2-REPLACE_WITH_DATE"}' \
  --max-time 90 -o /tmp/esmi_pricing_case2.sse
```

```bash
python - <<'PY'
import json, re
text = ""
for line in open("/tmp/esmi_pricing_case2.sse", encoding="utf-8"):
    if line.startswith("data: "):
        try:
            ev = json.loads(line[6:])
        except Exception:
            continue
        if ev.get("type") == "token":
            text += ev.get("content", "")
amounts = set(re.findall(r"\$\d[\d,]*", text))
print(f"amounts found: {sorted(amounts)}")
print("PASS — at least 2 distinct location prices present" if len(amounts) >= 2
      else "FAIL — expected per-location pricing (Weston != Keele), got fewer than 2 distinct amounts")
PY
```

If you want to pin exact current figures instead, fetch them live first —
`GET /platform/config` with `X-Platform-Secret` + `X-Tenant-Id: otro-nivel` returns the
tenant's current `services` map — rather than trusting the checked-in
`tenants/otro-nivel/config.json`, which is a fallback seed, not the source of truth
once a tenant has DB-backed config.

## Reporting

Report PASS/FAIL per check across both cases, and the reconstructed reply text for
whichever case has any FAIL. A Case 1 dollar-amount leak or a Case 2 missing amount
both point at the same two possible causes: `prompts/esmi_system.md` /
`tools.py` (`get_pricing`, `tenant.services`) drifted from the deployed build, or
Railway is serving a stale deploy on `-5375` — re-check the latest deploy before
assuming a prompt bug.

If pricing tiers or a tenant's services change, update the example numbers above to
match (Case 1 checks a *pattern*, not amounts, so it shouldn't need touching; Case 2
should be kept in sync with whichever tenant/service it probes).
