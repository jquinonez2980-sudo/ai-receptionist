#!/usr/bin/env python
"""PostToolUse pricing-sync hook.

Pricing lives in two places that MUST agree on the numbers:
  - _PRICING in tools.py  (canonical source returned by the get_pricing tool)
  - orchelix_knowledge_base/13_pricing_tiers.md  (what KB search / the website show)

If an edit to either file leaves a setup_from / monthly_from amount that is no
longer present in the KB markdown, warn Claude so the two don't drift. (Names
intentionally differ between the two — only the dollar amounts are checked.)

Contract (Claude Code PostToolUse hook):
  - stdin: {"tool_name": ..., "tool_input": {"file_path": ...}, ...}
  - exit 0 -> silent OK.
  - exit 2 -> stderr is surfaced to Claude as feedback (tool already ran).
Fails open on any internal/parse error.
"""

import json
import re
import sys
from pathlib import Path

TOOLS_PY = Path("tools.py")
PRICING_MD = Path("orchelix_knowledge_base/13_pricing_tiers.md")
RELEVANT = {"tools.py", "13_pricing_tiers.md"}


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

    if not TOOLS_PY.exists() or not PRICING_MD.exists():
        return 0

    tools_src = TOOLS_PY.read_text(encoding="utf-8", errors="ignore")
    kb_text = PRICING_MD.read_text(encoding="utf-8", errors="ignore")

    amounts = [
        int(m) for m in re.findall(r"\"(?:setup_from|monthly_from)\":\s*(\d+)", tools_src)
    ]
    if not amounts:
        return 0

    missing = []
    for amt in sorted(set(amounts)):
        formatted = f"${amt:,}"          # e.g. $8,500
        if formatted not in kb_text and str(amt) not in kb_text:
            missing.append(formatted)

    if missing:
        sys.stderr.write(
            "PRICING DRIFT WARNING: these amounts from _PRICING (tools.py) are NOT "
            "present in orchelix_knowledge_base/13_pricing_tiers.md:\n  "
            + ", ".join(missing)
            + "\nReconcile the two so the get_pricing tool and the KB/website agree. "
            "(See CLAUDE.md hard rule #2.)\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
