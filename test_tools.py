import os
import sys

# 프로젝트 루트를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from config import Config
from core.tools import get_tool_executor

# 환경 변수 로드
load_dotenv()

print("=== 도구 테스트 시작 ===\n")

# 도구 실행기 초기화
tool_executor = get_tool_executor()

# 1. 폴더 생성 테스트
print("1. 폴더 생성 테스트")
desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
test_dir = os.path.join(desktop_path, "자비스 테스트용")
result = tool_executor.create_directory(test_dir)
print(result)

# 2. 엑셀 파일 생성 테스트
print("\n2. 엑셀 파일 생성 테스트")
excel_path = os.path.join(test_dir, "123.xlsx")
data = [["이름", "나이"], ["철수", 30], ["영희", 25]]
result = tool_executor.create_excel_file(excel_path, data)
print(result)

print("\n=== 테스트 완료 ===")
