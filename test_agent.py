from agents import receptionist_agent

result = receptionist_agent.invoke({
    'messages': [{'role': 'user', 'content': 'Can you show me available slots next Tuesday?'}]
})

for msg in result['messages']:
    print(msg.type, ':', getattr(msg, 'content', '')[:200])