
import sys
from core.tools import get_tool_executor

executor = get_tool_executor()
result = executor.speak_text("안녕하세요, 사장님. 테스트 음성입니다.")
print(result)
