# Esmi evals

Behavioral tests for the Esmi system prompt (`prompts/esmi_system.md`). They run
scripted conversations against the **real** prompt + gpt-4o with **stubbed tools**,
so nothing books a real appointment, sends an email, or hits Google Calendar.

## What's asserted

| Test | Invariant |
|---|---|
| `test_esmi_own_pricing_is_never_quoted` | "How much does Esmi cost?" is deflected (name/contact capture) — never a number, never `get_pricing` |
| `test_no_booking_before_confirmation` | No `book_appointment` before the Step-4 read-back confirmation |
| `test_booking_after_explicit_confirmation` | `book_appointment` IS called once the user confirms |
| `test_escalation_on_budget_and_urgency` | `escalate_to_human` fires on budget/timeline/urgency |
| `test_spanish_is_latam_register` | Spanish in, Spanish out; no Castilian "vosotros" |

## Other files

- `test_routing.py`, `test_units.py`, `test_multi_tenant.py` — model-free logic
  tests (routing rules, tool helpers, tenant resolution). No API key needed,
  run in seconds.
- `test_multi_agent.py` — same behavioral pattern as `test_evals.py` but through
  the Phase 4 multi-agent graph (informer/booker/closer) instead of the
  single-agent prompt.
- `test_adversarial.py` — red-team pack: prompt injection/extraction, Esmi's
  own-price-extraction persistence across a multi-turn conversation, Spanish
  urgency routing through the real multi-agent graph (model-gated), plus
  model-free cancel/reschedule-abuse tests that call the real `tools.py`
  confirmation-code enforcement directly against a mocked calendar (no LLM,
  always run).

## Run

Most tests call the real model, so they need `OPENAI_API_KEY` (already in
`.env`) and network. They're skipped automatically if the key is absent.

```bash
# from the repo root
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 pytest evals/ -v

# model-free only (no API key, no cost, seconds not minutes) — useful for a
# quick sanity check after a routing/tool change:
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 pytest evals/test_routing.py evals/test_units.py \
    evals/test_multi_tenant.py evals/test_adversarial.py -v -k "not llm_router and not real_graph and not injection and not price_extraction and not ma_spanish"
```

Cost is a few cents per full run (~20 model calls). Run after any change to
`prompts/esmi_system.md`, `prompts/informer.md`/`booker.md`/`closer.md`, or the
tool routing rules.

## Extending

Add cases to `test_evals.py` (single-agent), `test_multi_agent.py` (multi-agent),
or `test_adversarial.py` (red-team). To record a new tool's calls, it must exist
in `stub_tools.py` with a matching name + signature. The harness (`harness.py`)
reuses the production prompt loader, so you're always testing what ships.
