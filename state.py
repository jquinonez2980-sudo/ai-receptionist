# state.py
# This is the "memory" for our AI receptionist
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """Shared state that every agent can see and update"""
    messages: Annotated[list[BaseMessage], add_messages]  # keeps the full conversation history
    lead_score: int | None          # 0-100 score for how good the lead is
    qualified: bool | None          # True if the lead is ready to book
    appointment_details: dict | None  # stores date/time when we book
    next: str | None                # tells the supervisor which agent to call next
    conversation_summary: str | None  # older-turns summary (see graph._compress_node);
                                       # injected into the system prompt by agents._make_middleware,
                                       # NOT the message list — add_messages always appends new
                                       # messages after existing ones, so a summary living in
                                       # `messages` would land after the live turns it summarizes.
