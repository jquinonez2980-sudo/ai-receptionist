---
description: Ask the live Esmi backend a pricing question and confirm it returns the canonical exact numbers
allowed-tools: Bash(curl *), Bash(python *)
---

Verify the **live** Esmi backend quotes correct, canonical pricing. (This replaces the old hand-escaped one-off curl in settings.local.json.)

Canonical amounts — these come from `_PRICING` in `tools.py` and ALL must appear in the reply:
- Esmi — AI Virtual Receptionist: setup **$8,500** / **$1,099**/mo
- Revenue Operations Agents: setup **$9,500** / **$1,299**/mo
- Firm OS: setup **$24,000** / **$2,499**/mo

Steps:
1. POST a pricing question to the live SSE endpoint with a UNIQUE thread_id (append today's date so you don't hit cached conversation state):

```bash
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 curl -sN -X POST \
  https://ai-receptionist-production-5375.up.railway.app/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"How much does each package cost? Give exact setup and monthly prices.","thread_id":"verify-pricing-REPLACE_WITH_DATE"}' \
  --max-time 90 > /tmp/esmi_pricing.sse
```

2. Reconstruct the reply by concatenating the `content` of every `token` SSE event, then check all six amounts are present:

```bash
python - <<'PY'
import json
text = ""
for line in open("/tmp/esmi_pricing.sse", encoding="utf-8"):
    if line.startswith("data: "):
        try:
            ev = json.loads(line[6:])
        except Exception:
            continue
        if ev.get("type") == "token":
            text += ev.get("content", "")
expected = ["$8,500", "$1,099", "$9,500", "$1,299", "$24,000", "$2,499"]
for amt in expected:
    print(("PASS" if amt in text else "FAIL"), amt)
PY
```

3. Report PASS/FAIL per amount. Any FAIL most likely means `_PRICING` (tools.py) drifted from the deployed build, or Railway is serving a stale deploy — flag it and suggest re-checking the latest deploy on `-5375`.
