"""Streaming smoke test — guards the SSE token path that api.py depends on.

api.py's /chat streams by iterating graph.astream_events(version="v2",
include_subgraphs=True) and forwarding `on_chat_model_stream` chunks as `token`
events. If the agent implementation ever stops surfacing those (e.g. a library
migration), live chat would silently fall back to a single dump or break. This
test asserts real token events surface from the production graph.

Calls gpt-4o for real — needs OPENAI_API_KEY (loaded from .env). Run with the
rest of the suite:  pytest evals/ -v
"""

import asyncio
import os

import pytest
from dotenv import load_dotenv

load_dotenv()  # so the skipif below sees OPENAI_API_KEY when run standalone

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — streaming test calls the real model.",
)


def _collect_streamed_text(message: str, thread_id: str) -> str:
    """Reproduce api.py's on_chat_model_stream token extraction against the real graph."""

    async def go() -> str:
        from graph import build_graph
        graph = build_graph()  # MemorySaver (tests don't need persistence)

        tokens: list[str] = []
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id}},
            version="v2",
            include_subgraphs=True,
        ):
            if event["event"] == "on_chat_model_stream":
                # Mirror api.py: drop the internal router classifier's tokens.
                if "esmi-router-internal" in (event.get("tags") or []):
                    continue
                chunk = event["data"].get("chunk")
                if chunk is None:
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, str) and content:
                    tokens.append(content)
        return "".join(tokens)

    return asyncio.run(go())


def _collect_streamed_text_and_tools(message: str, thread_id: str) -> tuple[str, list[str]]:
    """Like _collect_streamed_text, but also returns which tools fired —
    needed when the triggering question's tool call isn't guaranteed by
    fixed text content (e.g. slot availability, whose times vary by day)."""

    async def go() -> tuple[str, list[str]]:
        from graph import build_graph
        graph = build_graph()

        tokens: list[str] = []
        tools: list[str] = []
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id}},
            version="v2",
            include_subgraphs=True,
        ):
            if event["event"] == "on_tool_start":
                tools.append(event.get("name", ""))
            elif event["event"] == "on_chat_model_stream":
                if "esmi-router-internal" in (event.get("tags") or []):
                    continue
                chunk = event["data"].get("chunk")
                if chunk is None:
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, str) and content:
                    tokens.append(content)
        return "".join(tokens), tools

    return asyncio.run(go())


def _collect_multi_agent_stream(message: str, thread_id: str) -> str:
    """Stream through the REAL multi-agent graph, applying api.py's router-tag
    filter. Used to prove the router classifier's one-word answer never leaks."""
    from langgraph.checkpoint.memory import MemorySaver

    from graph import _build_multi_agent_graph

    async def go() -> str:
        graph = _build_multi_agent_graph(MemorySaver())
        tokens: list[str] = []
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id, "tenant_id": "default"}},
            context={"tenant_id": "default"},
            version="v2",
            include_subgraphs=True,
        ):
            if event["event"] == "on_chat_model_stream":
                if "esmi-router-internal" in (event.get("tags") or []):
                    continue
                chunk = event["data"].get("chunk")
                if chunk is None:
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, str) and content:
                    tokens.append(content)
        return "".join(tokens)

    return asyncio.run(go())


def test_chat_model_tokens_surface_for_a_greeting():
    # A plain greeting takes no tool path, so the reply is pure streamed tokens.
    text = _collect_streamed_text("Hi there!", thread_id="eval-stream-greet")
    assert text.strip(), (
        "no tokens surfaced via on_chat_model_stream — the SSE streaming path "
        "api.py relies on has regressed (live chat would lose token streaming)"
    )


def test_chat_model_tokens_surface_after_tool_call():
    """Tokens must stream on tool-calling turns, not just plain greetings.

    This guards the on_chain_end fallback path in api.py — if on_chat_model_stream
    tokens stop surfacing after a tool call, live chat degrades to a single dump
    at the end of the response (or worse, silence).
    """
    # A specific-day availability question always triggers list_available_slots
    # (booking flow STEP 2), so this is a real tool-call turn. (Used to be a
    # pricing question — that stopped being a tool call once "how much does
    # Esmi cost" got a prompt-injected answer instead of a get_pricing call;
    # see test_evals.test_esmi_own_pricing_is_stated_clearly.)
    text, called = _collect_streamed_text_and_tools(
        "What times are available this Thursday?",
        thread_id="eval-stream-tool-call",
    )
    assert "list_available_slots" in called, (
        f"expected a real tool-calling turn to test SSE-after-tool-call, got: {called}"
    )
    assert text.strip(), (
        "no tokens surfaced after a tool call — the SSE fallback path may have "
        "regressed; check on_chain_end fallback in api.py _stream_chat()"
    )


def test_router_classification_does_not_leak_into_stream():
    """The LLM router's one-word answer must never reach the user.

    A keyword-free booking phrase triggers the router (→ 'booker'). That token is
    tagged 'esmi-router-internal' and filtered by api.py. This guards against the
    classifier's output bleeding into the reply ('bookerWhat day works?').
    """
    text = _collect_multi_agent_stream(
        "hey could you get me on your calendar sometime?",
        thread_id="eval-stream-router-leak",
    )
    assert text.strip(), "no user-facing tokens surfaced"
    low = text.lower()
    # The reply should be the booker's question, not the classifier's word.
    assert not low.startswith(("booker", "informer", "closer")), (
        f"router classification leaked into the user stream: {text[:80]!r}"
    )
