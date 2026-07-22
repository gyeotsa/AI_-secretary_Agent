
import os
import sys

# 프로젝트 루트를 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.tools import ToolExecutor
from core.harness import SafetyLayer
from config import Config

print("=== Step 4 테스트 시작 ===\n")

# 1. SafetyLayer 테스트
print("1. SafetyLayer 테스트")
print("   - ALLOWED_PATHS:", Config.ALLOWED_PATHS)
print("   - ALLOWED_COMMANDS:", Config.ALLOWED_COMMANDS)

test_path = os.path.join(os.path.dirname(__file__), "test.txt")
is_valid, error_msg = SafetyLayer.validate_path(test_path)
print(f"   - validate_path({test_path}): {is_valid}, {error_msg}")

test_cmd = "echo hello"
is_valid, error_msg, args = SafetyLayer.validate_command(test_cmd)
print(f"   - validate_command({test_cmd}): {is_valid}, {error_msg}, args={args}")

blocked_cmd = "del test.txt"
is_valid, error_msg, args = SafetyLayer.validate_command(blocked_cmd)
print(f"   - validate_command({blocked_cmd}): {is_valid}, {error_msg}, args={args}")

print()

# 2. ToolExecutor 테스트
print("2. ToolExecutor 테스트")
tool_exec = ToolExecutor()

# write_file 테스트
print("   - write_file 테스트...")
result = tool_exec.write_file(test_path, "테스트 내용입니다.\n")
print(f"     결과: {result}")

# read_file 테스트
print("   - read_file 테스트...")
result = tool_exec.read_file(test_path)
print(f"     결과: {result}")

# run_command 테스트 (shell=False에서도 동작하는 실행 파일)
print("   - run_command 테스트 (git --version)...")
result = tool_exec.run_command("git --version")
print(f"     결과:\n{result}")

# 테스트 파일 정리
if os.path.exists(test_path):
    os.remove(test_path)
    print(f"\n   - 테스트 파일 {test_path} 삭제 완료")

print("\n=== Step 4 테스트 완료 ===")
