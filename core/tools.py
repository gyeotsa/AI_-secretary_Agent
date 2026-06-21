import os
import subprocess
from typing import Optional
from config import Config
from core.harness import SafetyLayer
from core.user_profile import get_user_profile

try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None


class ToolExecutor:
    def __init__(self):
        self.safety = SafetyLayer()
        self.user_profile = get_user_profile()

    def read_file(self, path: str) -> str:
        is_valid, error_msg = self.safety.validate_path(path)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"파일 읽기 오류: {str(e)}"

    def write_file(self, path: str, content: str) -> str:
        is_valid, error_msg = self.safety.validate_path(path)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"파일이 성공적으로 저장되었습니다: {path}"
        except Exception as e:
            return f"파일 쓰기 오류: {str(e)}"

    def list_directory(self, path: str) -> str:
        is_valid, error_msg = self.safety.validate_path(path)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            items = os.listdir(path)
            result = []
            for item in items:
                item_path = os.path.join(path, item)
                is_dir = os.path.isdir(item_path)
                prefix = "[폴더] " if is_dir else "[파일] "
                result.append(prefix + item)
            return "\n".join(result)
        except Exception as e:
            return f"디렉토리 목록 오류: {str(e)}"

    def run_command(self, command: str) -> str:
        is_valid, error_msg = self.safety.validate_command(command)
        if not is_valid:
            return f"오류: {error_msg}"

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = []
            if result.stdout:
                output.append(f"출력:\n{result.stdout}")
            if result.stderr:
                output.append(f"에러:\n{result.stderr}")
            output.append(f"종료 코드: {result.returncode}")
            return "\n".join(output)
        except Exception as e:
            return f"명령어 실행 오류: {str(e)}"

    def web_search(self, query: str, num_results: int = 5) -> str:
        if DDGS is None:
            return "오류: duckduckgo-search가 설치되지 않았습니다. requirements.txt를 확인하세요."

        try:
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=num_results):
                    results.append(f"제목: {r.get('title', '')}")
                    results.append(f"링크: {r.get('href', '')}")
                    results.append(f"요약: {r.get('body', '')}")
                    results.append("-" * 50)
            return "\n".join(results)
        except Exception as e:
            return f"웹 검색 오류: {str(e)}"

    def set_profile(self, key: str, value: str) -> str:
        try:
            self.user_profile.set(key, value)
            return f"프로필이 저장되었습니다: {key} = {value}"
        except Exception as e:
            return f"프로필 저장 오류: {str(e)}"

    def get_profile(self, key: str = "") -> str:
        try:
            if key:
                value = self.user_profile.get(key)
                return f"{key}: {value}" if value else f"{key}가 프로필에 없습니다."
            else:
                return self.user_profile.get_profile_summary()
        except Exception as e:
            return f"프로필 조회 오류: {str(e)}"

    def set_preference(self, pref_key: str, pref_value: str) -> str:
        try:
            self.user_profile.set_preference(pref_key, pref_value)
            return f"환경설정이 저장되었습니다: {pref_key} = {pref_value}"
        except Exception as e:
            return f"환경설정 저장 오류: {str(e)}"

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        tool_functions = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "list_directory": self.list_directory,
            "run_command": self.run_command,
            "web_search": self.web_search,
            "set_profile": self.set_profile,
            "get_profile": self.get_profile,
            "set_preference": self.set_preference,
        }

        if tool_name not in tool_functions:
            return f"오류: 알 수 없는 툴 '{tool_name}'"

        try:
            return tool_functions[tool_name](**tool_input)
        except TypeError as e:
            return f"툴 파라미터 오류: {str(e)}"


def get_tools_schema() -> list[dict]:
    return [
        {
            "name": "read_file",
            "description": "파일의 내용을 읽어옵니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "읽을 파일의 경로",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "파일에 내용을 씁니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "쓸 파일의 경로",
                    },
                    "content": {
                        "type": "string",
                        "description": "파일에 쓸 내용",
                    },
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "list_directory",
            "description": "디렉토리의 파일과 폴더 목록을 보여줍니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "목록을 볼 디렉토리 경로",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "run_command",
            "description": "시스템 명령어를 실행합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "실행할 명령어",
                    }
                },
                "required": ["command"],
            },
        },
        {
            "name": "web_search",
            "description": "웹에서 정보를 검색합니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색 쿼리",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "검색 결과 수 (기본 5개)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "set_profile",
            "description": "사용자 프로필에 정보를 저장합니다 (예: 이름, 취미, 선호도 등)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "프로필 키 (예: '이름', '취미', '선호_언어')",
                    },
                    "value": {
                        "type": "string",
                        "description": "프로필 값",
                    },
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "get_profile",
            "description": "사용자 프로필 정보를 조회합니다. 키를 지정하지 않으면 전체 프로필을 보여줍니다",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "조회할 프로필 키 (선택사항)",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "set_preference",
            "description": "사용자 환경설정을 저장합니다 (예: 응답_스타일, 언어 등)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pref_key": {
                        "type": "string",
                        "description": "환경설정 키",
                    },
                    "pref_value": {
                        "type": "string",
                        "description": "환경설정 값",
                    },
                },
                "required": ["pref_key", "pref_value"],
            },
        },
    ]


_tools_executor = None


def get_tool_executor() -> ToolExecutor:
    global _tools_executor
    if _tools_executor is None:
        _tools_executor = ToolExecutor()
    return _tools_executor
