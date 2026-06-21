import os
from config import Config


class SafetyLayer:
    """
    Harness Engineering: 5중 안전장치 중 기본 layer
    - 프롬프트 가드레일
    - 경로/명령어 검증
    """

    @staticmethod
    def validate_path(path: str) -> tuple[bool, str]:
        """파일 경로가 허용된 범위 내에 있는지 확인"""
        if not Config.ALLOWED_PATHS:
            return True, ""

        abs_path = os.path.abspath(os.path.expanduser(path))
        for allowed in Config.ALLOWED_PATHS:
            allowed_abs = os.path.abspath(os.path.expanduser(allowed))
            if abs_path.startswith(allowed_abs):
                return True, ""
        return False, f"경로 '{path}'는 허용되지 않습니다."

    @staticmethod
    def validate_command(command: str) -> tuple[bool, str]:
        """명령어에 위험한 키워드가 없는지 확인"""
        cmd_lower = command.lower()
        for blocked in Config.BLOCKED_COMMANDS:
            if blocked.lower() in cmd_lower:
                return False, f"명령어 '{command}'는 차단되었습니다."
        return True, ""

    @staticmethod
    def sanitize_input(text: str) -> str:
        """기본적인 입력 정화"""
        return text.strip()
