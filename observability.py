# observability.py — Phase 1
# Wires LangSmith tracing if LANGSMITH_API_KEY is set. Safe no-op otherwise.
#
# This module is imported for its side effects at startup (from graph.py).
# It only sets env vars LangChain reads — it does not import langsmith itself,
# so it never crashes on missing optional deps.

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def init_observability() -> None:
    """Enable LangSmith tracing when an API key is present.

    Reads:
      LANGSMITH_API_KEY  (preferred) or LANGCHAIN_API_KEY  (legacy)
      LANGSMITH_PROJECT  (default: "esmi-receptionist")
      LANGSMITH_ENDPOINT (optional; default: https://api.smith.langchain.com)

    Sets the LANGCHAIN_* env vars LangChain/LangGraph read at runtime.
    """
    api_key = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
    if not api_key:
        log.info("LangSmith disabled (no LANGSMITH_API_KEY).")
        return

    project = os.getenv("LANGSMITH_PROJECT", "esmi-receptionist")
    endpoint = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")

    # setdefault so any explicit override the user already set wins
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", api_key)
    os.environ.setdefault("LANGCHAIN_PROJECT", project)
    os.environ.setdefault("LANGCHAIN_ENDPOINT", endpoint)

    log.info(f"LangSmith tracing enabled (project={project}).")
