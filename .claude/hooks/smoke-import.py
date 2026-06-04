#!/usr/bin/env python
"""PostToolUse smoke-import hook.

After an edit to the agent's core Python modules, verify the import chain still
loads — catches syntax errors, bad imports, and broken tool definitions before
they reach Railway. Equivalent to: python -c "from agents import receptionist_agent"
(which pulls in tools.py + agents.py).

Contract (Claude Code PostToolUse hook):
  - stdin: {"tool_name": ..., "tool_input": {"file_path": ...}, ...}
  - exit 0 -> silent OK.
  - exit 2 -> stderr surfaced to Claude as feedback (tool already ran).
Fails open on timeout or internal error so a flaky import never bricks editing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# Editing any of these can break `from agents import receptionist_agent`.
RELEVANT = {"agents.py", "tools.py", "state.py"}
TIMEOUT_S = 90


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    path = (payload.get("tool_input", {}) or {}).get("file_path", "") or ""
    if Path(path).name not in RELEVANT:
        return 0

    # Force UTF-8 in the child so module-level emoji prints (e.g. tools.py's
    # "✅ Tools loaded") don't crash on Windows' cp1252 console — that would be a
    # false positive. Railway runs UTF-8 already.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    try:
        proc = subprocess.run(
            [sys.executable, "-c", "from agents import receptionist_agent"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired:
        # Fail open: a slow import (model/embeddings init) is not an error.
        return 0
    except Exception:
        return 0

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
        sys.stderr.write(
            "SMOKE-IMPORT FAILED: `from agents import receptionist_agent` did not "
            "load after this edit. The agent will not start on Railway until fixed:\n"
            + "\n".join(tail)
            + "\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
