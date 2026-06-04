#!/usr/bin/env python
"""PreToolUse secret-scan hook for the Esmi AI-receptionist repo.

Blocks Write/Edit/Bash calls that would introduce a live credential into the
working tree, or re-bake a secret into the Dockerfile as an `ENV` line. This
exists because secrets were previously baked into the Dockerfile and leaked
into git history (see PROJECT_STATUS.md "Security — Action Required").

Contract (Claude Code hooks):
  - Reads the tool call as JSON on stdin: {"tool_name": ..., "tool_input": {...}}.
  - exit 0  -> allow the tool call.
  - exit 2  -> BLOCK the tool call; stderr is shown to Claude.
  - other   -> non-blocking; tool proceeds (fail-open on our own bugs).

This is a developer guardrail, not a hard security boundary: it fails open on
parse/internal errors so a hook bug never bricks the workflow.
"""

import json
import re
import sys

# Live-credential signatures. Tuned to match REAL keys, not the placeholders in
# .env.example (e.g. "sk-...", "your-key-here") which lack the long charset tail.
SECRET_PATTERNS = [
    (r"sk-proj-[A-Za-z0-9_-]{20,}", "OpenAI project API key (sk-proj-...)"),
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "Anthropic API key (sk-ant-...)"),
    (r"sk-[A-Za-z0-9]{32,}", "OpenAI API key (sk-...)"),
    (r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", "SendGrid API key (SG....)"),
    (r"re_[A-Za-z0-9_]{20,}", "Resend API key (re_...)"),
    (r"GOCSPX-[A-Za-z0-9_-]{20,}", "Google OAuth client secret (GOCSPX-...)"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "PEM private key block"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id (AKIA...)"),
]

# Dockerfile re-baking: ENV <NAME_WITH_KEY/SECRET/TOKEN/PASSWORD> = <value>.
DOCKERFILE_ENV_SECRET = re.compile(
    r"^\s*ENV\s+\w*(?:KEY|SECRET|TOKEN|PASSWORD)\w*\s*=?\s*\S",
    re.IGNORECASE | re.MULTILINE,
)


def collect_text(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Return (text_to_scan, target_path) for the given tool call."""
    if tool_name in ("Write",):
        return tool_input.get("content", ""), tool_input.get("file_path", "")
    if tool_name in ("Edit",):
        return tool_input.get("new_string", ""), tool_input.get("file_path", "")
    if tool_name in ("MultiEdit",):
        edits = tool_input.get("edits", []) or []
        joined = "\n".join(e.get("new_string", "") for e in edits)
        return joined, tool_input.get("file_path", "")
    if tool_name in ("NotebookEdit",):
        return tool_input.get("new_source", ""), tool_input.get("notebook_path", "")
    if tool_name in ("Bash", "PowerShell"):
        return tool_input.get("command", ""), ""
    return "", ""


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Fail open: never block on our own parse failure.
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    text, path = collect_text(tool_name, tool_input)
    if not text:
        return 0

    findings = []

    for pattern, label in SECRET_PATTERNS:
        if re.search(pattern, text):
            findings.append(f"  - {label}")

    if path.replace("\\", "/").rstrip("/").endswith("Dockerfile"):
        if DOCKERFILE_ENV_SECRET.search(text):
            findings.append(
                "  - Dockerfile ENV line assigning a *_KEY/_SECRET/_TOKEN value "
                "— secrets must be Railway runtime env vars, never baked in."
            )

    if findings:
        sys.stderr.write(
            "BLOCKED by secret-scan hook: this change appears to embed a live "
            "credential in the repo.\n"
            + "\n".join(findings)
            + "\n\nFix: keep secrets in Railway env vars (or local .env, which is "
            "git-ignored). Never write keys into tracked files or the Dockerfile. "
            "If this is a false positive (e.g. a placeholder), rephrase so it does "
            "not look like a live key.\n"
        )
        return 2

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open on any unexpected internal error.
        sys.exit(0)
