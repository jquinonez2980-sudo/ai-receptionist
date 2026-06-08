"""Builds test agents / graphs with the REAL Esmi prompts + stubbed tools, and
runs scripted multi-turn conversations, returning the recorded tool calls + text.

Two test modes:
  run_conversation()            — Phase 1 single-agent (harness default).
  run_multi_agent_conversation() — Phase 4 three-specialist graph topology.
Both use stub tools so no real Calendar / SendGrid / Twilio calls are made.
"""

import time

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, END
from langchain.agents import create_agent

from agents import make_prompt_middleware, _make_middleware, _load_prompt
from graph import _route
from state import AgentState

from . import stub_tools

# Inter-test throttle: the OpenAI free/starter tier has a 30K TPM cap. Running
# 12 tests back-to-back exhausts it near the tail. A short pause between calls
# keeps us inside the window without needing rate-limit retry logic.
_INTER_CALL_DELAY_S = 5.0


def build_test_agent():
    """Same model + prompt as production, but with recording stub tools."""
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    return create_agent(
        llm,
        tools=stub_tools.ALL_STUBS,
        middleware=[make_prompt_middleware()],
        checkpointer=MemorySaver(),
    )


def run_conversation(
    turns: list[str], thread_id: str = "eval", kb_empty: bool = False
) -> tuple[list, str]:
    """Run a conversation turn-by-turn on one thread.

    Args:
        turns: user messages, sent in order on the same thread (memory persists).
        thread_id: checkpointer thread id.
        kb_empty: if True, the stub KB returns "no results" (drives escalation evals).

    Returns:
        (calls, final_text) where calls is the ordered list of (tool_name, kwargs)
        recorded across ALL turns, and final_text is the last assistant message.
    """
    stub_tools.reset()
    stub_tools.KB_EMPTY = kb_empty
    agent = build_test_agent()
    config = {"configurable": {"thread_id": thread_id}}
    final_text = ""
    for user_msg in turns:
        time.sleep(_INTER_CALL_DELAY_S)
        result = agent.invoke({"messages": [("user", user_msg)]}, config)
        final_text = result["messages"][-1].content
    return list(stub_tools.CALLS), final_text


def tool_names(calls: list) -> list[str]:
    return [name for name, _ in calls]


# ── Phase 4: multi-agent test graph ──────────────────────────────────────────

def build_multi_agent_test_graph():
    """Phase 4 routing topology with stub tools — no real external calls.

    Uses the SAME production node-builders (_compress_node, _make_informer_node,
    _make_booker_node, _make_closer_node) and _route() from graph.py — only the
    underlying tools are swapped for recording stubs. This guarantees the test
    graph exercises the real state-writing logic (lead_score, appointment_details,
    and crucially `next` for booking stickiness), so harness and prod can't drift.
    """
    from graph import (
        _compress_node, _make_informer_node, _make_booker_node, _make_closer_node,
    )

    llm = ChatOpenAI(model="gpt-4o", temperature=0)

    informer = create_agent(
        llm,
        tools=stub_tools.INFORMER_STUBS,
        middleware=[_make_middleware(_load_prompt("informer.md"))],
    )
    booker = create_agent(
        llm,
        tools=stub_tools.BOOKER_STUBS,
        middleware=[_make_middleware(_load_prompt("booker.md"))],
    )
    closer = create_agent(
        llm,
        tools=stub_tools.CLOSER_STUBS,
        middleware=[_make_middleware(_load_prompt("closer.md"))],
    )

    workflow = StateGraph(AgentState)
    workflow.add_node("compress", _compress_node)
    workflow.add_node("informer", _make_informer_node(informer))
    workflow.add_node("booker",   _make_booker_node(booker))
    workflow.add_node("closer",   _make_closer_node(closer))

    workflow.set_entry_point("compress")
    workflow.add_conditional_edges(
        "compress",
        _route,
        {"informer": "informer", "booker": "booker", "closer": "closer"},
    )
    for node in ("informer", "booker", "closer"):
        workflow.add_edge(node, END)

    return workflow.compile(checkpointer=MemorySaver())


def run_multi_agent_conversation(
    turns: list[str], thread_id: str = "eval-ma", kb_empty: bool = False
) -> tuple[list, str]:
    """Run a conversation through the Phase 4 multi-agent graph with stub tools.

    Same call signature as run_conversation() so tests can be written identically
    and switched between Phase 1 / Phase 4 by changing one function name.
    """
    stub_tools.reset()
    stub_tools.KB_EMPTY = kb_empty
    g = build_multi_agent_test_graph()
    config = {"configurable": {"thread_id": thread_id}}
    final_text = ""
    for user_msg in turns:
        time.sleep(_INTER_CALL_DELAY_S)
        result = g.invoke({"messages": [("user", user_msg)]}, config)
        final_text = result["messages"][-1].content
    return list(stub_tools.CALLS), final_text
