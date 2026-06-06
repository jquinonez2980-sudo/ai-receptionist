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

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — streaming test calls the real model.",
)


def _collect_streamed_text(message: str, thread_id: str) -> str:
    """Reproduce api.py's on_chat_model_stream token extraction against the real graph."""

    async def go() -> str:
        from graph import graph  # the production outer graph (real agent + checkpointer)

        tokens: list[str] = []
        async for event in graph.astream_events(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id}},
            version="v2",
            include_subgraphs=True,
        ):
            if event["event"] == "on_chat_model_stream":
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
    # Pricing question always triggers get_pricing, so this is a real tool-call turn.
    text = _collect_streamed_text(
        "How much does Esmi cost?",
        thread_id="eval-stream-tool-call",
    )
    assert text.strip(), (
        "no tokens surfaced after a tool call — the SSE fallback path may have "
        "regressed; check on_chain_end fallback in api.py _stream_chat()"
    )
    # Pricing numbers must appear in the streamed tokens (not swallowed by fallback).
    assert "$8,500" in text or "8,500" in text, (
        f"canonical pricing not present in streamed text — tokens may be arriving "
        f"via fallback path rather than incremental stream. Got: {text[:300]!r}"
    )
