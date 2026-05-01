# app.py
# This is the main file that runs your AI Virtual Receptionist

from dotenv import load_dotenv
from graph import graph
import uuid

load_dotenv()

print("🤖 AI Virtual Receptionist is starting...")
print("Type 'exit' or 'quit' to stop.\n")

# Unique thread ID so the AI remembers the conversation
thread_id = str(uuid.uuid4())
config = {"configurable": {"thread_id": thread_id}}

while True:
    user_input = input("You: ")
    if user_input.lower() in ["exit", "quit", "bye"]:
        print("👋 Goodbye! Your AI receptionist is ready for real customers.")
        break
    
    if not user_input.strip():
        continue
    
    # Send message to the full AI graph
    print("🤖 Thinking...")
    result = graph.invoke(
        {"messages": [{"role": "user", "content": user_input}]},
        config
    )
    
    # Print the AI's final response
    last_message = result["messages"][-1].content
    print(f"Receptionist: {last_message}\n")