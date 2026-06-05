"""Builds a test agent with the REAL Esmi prompt + stubbed tools, and runs
scripted multi-turn conversations, returning the recorded tool calls + final text.
"""

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langchain.agents import create_agent

from agents import make_prompt_middleware  # the REAL prompt (prompts/esmi_system.md)

from . import stub_tools


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
        result = agent.invoke({"messages": [("user", user_msg)]}, config)
        final_text = result["messages"][-1].content
    return list(stub_tools.CALLS), final_text


def tool_names(calls: list) -> list[str]:
    return [name for name, _ in calls]
