
import sys
import os

# 프로젝트 루트 경로 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.tools import get_tool_executor

print("통합 TTS 테스트 시작...")

tool_executor = get_tool_executor()

test_text = "일상적으로는 맑음으로 예보되는군요, 보스."
print(f"테스트 텍스트: {test_text}")

result = tool_executor.speak_text(test_text)
print(f"결과: {result}")

print("테스트 완료!")

