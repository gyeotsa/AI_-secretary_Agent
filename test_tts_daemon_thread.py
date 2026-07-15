
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.tools import get_tool_executor

print("데몬 스레드 TTS 테스트 시작...")

tool_executor = get_tool_executor()
test_text = "데몬 스레드에서 테스트하는 메시지입니다, 보스."

def speak_in_thread():
    print("[DEBUG] 데몬 스레드에서 speak_text 호출")
    result = tool_executor.speak_text(test_text)
    print(f"[DEBUG] 데몬 스레드에서 결과: {result}")

# 데몬 스레드 생성
thread = threading.Thread(target=speak_in_thread, daemon=True)
thread.start()

# 스레드가 완료될 때까지 기다림
thread.join()

print("테스트 완료!")

