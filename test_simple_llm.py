import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.llm import get_llm_client
from config import Config

print("[TEST] Testing Ollama directly...")
print(f"[TEST] LLM Provider: {Config.LLM_PROVIDER}")
print(f"[TEST] Ollama Model: {Config.OLLAMA_MODEL}")

try:
    llm = get_llm_client()
    print("[TEST] LLM client initialized")

    messages = [{"role": "user", "content": "안녕하세요!"}]
    print(f"[TEST] Sending messages: {messages}")

    response_text, tool_uses = llm.chat_with_tools(messages)

    print(f"\n[TEST] Response received!")
    print(f"[TEST] Response text: {response_text}")
    print(f"[TEST] Tool uses: {tool_uses}")

except Exception as e:
    print(f"\n[ERROR] Exception occurred: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
