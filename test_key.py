# test_key.py - Simple test to check if your OpenAI key is loading
from dotenv import load_dotenv
import os

load_dotenv()   # This reads your .env file

key = os.getenv('OPENAI_API_KEY')

if key and key.startswith('sk-'):
    print("✅ SUCCESS! Your OpenAI key is loaded correctly!")
    print("Key starts with:", key[:10] + "...")
else:
    print("❌ Key is NOT loaded.")
    print("Please double-check your .env file.")
    print("Current value found:", key)