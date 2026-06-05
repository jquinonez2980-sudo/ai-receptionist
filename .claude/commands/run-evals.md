---
description: Run the Esmi behavioral + streaming eval suite against the live prompt + gpt-4o
allowed-tools: Bash(python -m pytest *), Bash(pytest *), Bash(pip install *)
---

Run the eval suite in `evals/` — it asserts Esmi's customer-facing invariants against
the REAL prompt (`prompts/esmi_system.md`) + gpt-4o, with stubbed tools (no real
bookings, emails, or calendar calls). See `evals/README.md`.

Invariants covered:
- pricing answered via get_pricing, never the KB (canonical number present)
- no book_appointment before the Step-4 read-back confirmation
- book_appointment fires after explicit confirmation
- reschedule / cancel both look the booking up first (find_booking) before acting
- escalate_to_human actually fires on budget/urgency AND on a KB miss (no
  "someone will follow up" without calling the tool)
- Spanish answered in Latin-American register (no "vosotros")
- SSE token streaming surfaces (the path api.py /chat depends on)

Steps:
1. Ensure pytest is installed: `pip install -r requirements-dev.txt` (it's dev-only,
   not in requirements.txt). Skip if `python -c "import pytest"` already works.
2. Run:
   `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python -m pytest evals/ -v`
   (UTF-8 env avoids Windows cp1252 choking on the agent's emoji prints.)
3. Report PASS/FAIL per invariant. A failure is usually a real behavior change in
   `prompts/esmi_system.md` or tool routing — investigate before dismissing it.

These call gpt-4o for real (needs OPENAI_API_KEY from .env) — about a minute and a
few cents per run. Run after any change to the prompt or tool routing rules.
