# webhook.py - Twilio SMS + WhatsApp for Orchelix AI Receptionist

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
import os
import threading
from dotenv import load_dotenv
from graph import graph
import uuid

load_dotenv()

app = Flask(__name__)

conversation_memory = {}

def send_ai_reply(from_number, to_number, incoming_msg, thread_id):
    try:
        response = graph.invoke(
            {"messages": [{"role": "user", "content": incoming_msg}]},
            {"configurable": {"thread_id": thread_id}}
        )
        ai_reply = response["messages"][-1].content
        print(f"🤖 AI Reply: {ai_reply}")

        client = Client(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )
        client.messages.create(
            body=ai_reply,
            from_=to_number,
            to=from_number
        )
        print(f"✅ Message sent to {from_number}")

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()

@app.route("/sms", methods=['GET', 'POST'])
def sms_webhook():
    incoming_msg = request.values.get('Body', '').strip()
    from_number = request.values.get('From', '')
    to_number = request.values.get('To', '')

    print(f"📩 Incoming from {from_number}: {incoming_msg}")

    if from_number not in conversation_memory:
        conversation_memory[from_number] = str(uuid.uuid4())
    thread_id = conversation_memory[from_number]

    thread = threading.Thread(
        target=send_ai_reply,
        args=(from_number, to_number, incoming_msg, thread_id)
    )
    thread.start()

    return ('', 204)

if __name__ == "__main__":
    print("🚀 Twilio webhook is running on port 5000...")
    print("Ready for SMS and WhatsApp!")
    app.run(port=5000, debug=True)