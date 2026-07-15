
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from core.memory import get_memory
from core.llm import get_llm_client
from core.tools import get_tool_executor
from core.user_profile import get_user_profile
from core.rag import get_rag_manager

print("전체 흐름 테스트 시작...")

# 초기화
memory = get_memory()
llm = get_llm_client()
tool_executor = get_tool_executor()
user_profile = get_user_profile()
rag_manager = get_rag_manager()

messages = []
last_response = ""
session_id = "test_session"

# 시스템 프롬프트 설정
llm.set_system_prompt(Config.get_system_prompt(user_profile))

# 사용자 입력 시뮬레이션
user_input = "자비스 날씨"
print(f"사용자 입력: {user_input}")

messages.append({"role": "user", "content": user_input})

# AI 호출
print("LLM 호출 중...")
response_text, tool_uses = llm.chat_with_tools(messages)
print(f"LLM 응답: {response_text}")
print(f"Tool uses: {tool_uses}")

# 응답 처리 (main_qt.py의 _on_ai_response 흐름 모방)
import re
response_text = re.sub(r'[^\w\s가-힣.,!?]', '', response_text)
print(f"이모지 제거 후: {response_text}")

last_response = response_text
print(f"last_response 설정: {last_response}")

messages.append({"role": "assistant", "content": response_text})

# 대화 저장
memory.save_message(session_id, "user", user_input)
memory.save_message(session_id, "assistant", response_text)

# 자동으로 음성 응답
print("TTS 스레드 시작...")

def speak_in_thread():
    print(f"[DEBUG] 스레드에서 _speak_with_check 호출: {last_response}")
    try:
        print(f"[DEBUG] tool_executor.speak_text 호출 전")
        result = tool_executor.speak_text(last_response)
        print(f"[DEBUG] tool_executor.speak_text 반환값: {result}")
        if "오류:" in result:
            print(f"⚠️ TTS 오류: {result}")
    except Exception as e:
        print(f"⚠️ TTS 오류: {e}")
        import traceback
        traceback.print_exc()

thread = threading.Thread(target=speak_in_thread, daemon=True)
thread.start()

# 스레드가 완료될 때까지 기다림
thread.join()

print("전체 흐름 테스트 완료!")

