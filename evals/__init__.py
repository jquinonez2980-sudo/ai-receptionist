"""Esmi eval harness.

Runs scripted conversations against the REAL system prompt (prompts/esmi_system.md)
with stubbed tools, so we can assert routing/behavior invariants without making
real bookings, sending real emails, or hitting Google Calendar.

The model calls (gpt-4o) ARE real — that's the point: we test whether the live
prompt + model route correctly. Requires OPENAI_API_KEY (loaded from .env).

Run:  pytest evals/ -v
"""

import os
import sys

# Make the repo root importable (agents.py, tools.py live there) regardless of
# where pytest is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
