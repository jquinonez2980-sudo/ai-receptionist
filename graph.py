# graph.py - SIMPLE VERSION FOR SINGLE AGENT

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from state import AgentState
from agents import receptionist_agent   # <-- uses the single agent

def build_graph():
    workflow = StateGraph(AgentState)
    
    workflow.add_node("receptionist", receptionist_agent)
    
    workflow.set_entry_point("receptionist")
    workflow.add_edge("receptionist", END)
    
    memory = MemorySaver()
    graph = workflow.compile(checkpointer=memory)
    return graph

graph = build_graph()

print("✅ Simple graph built successfully!")