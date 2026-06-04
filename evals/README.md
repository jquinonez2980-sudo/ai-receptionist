# Esmi evals

Behavioral tests for the Esmi system prompt (`prompts/esmi_system.md`). They run
scripted conversations against the **real** prompt + gpt-4o with **stubbed tools**,
so nothing books a real appointment, sends an email, or hits Google Calendar.

## What's asserted

| Test | Invariant |
|---|---|
| `test_pricing_uses_get_pricing_not_kb` | Pricing answered via `get_pricing`, never the KB; canonical number appears |
| `test_no_booking_before_confirmation` | No `book_appointment` before the Step-4 read-back confirmation |
| `test_booking_after_explicit_confirmation` | `book_appointment` IS called once the user confirms |
| `test_escalation_on_budget_and_urgency` | `escalate_to_human` fires on budget/timeline/urgency |
| `test_spanish_is_latam_register` | Spanish in, Spanish out; no Castilian "vosotros" |

## Run

These call the real model, so they need `OPENAI_API_KEY` (already in `.env`) and
network. They're skipped automatically if the key is absent.

```bash
# from the repo root
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 pytest evals/ -v
```

Cost is a few cents per full run (~15 model calls). Run after any change to
`prompts/esmi_system.md` or the tool routing rules.

## Extending

Add cases to `test_evals.py`. To record a new tool's calls, it must exist in
`stub_tools.py` with a matching name + signature. The harness (`harness.py`)
reuses the production prompt loader, so you're always testing what ships.
