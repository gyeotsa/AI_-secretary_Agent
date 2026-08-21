import os
import shlex
import re
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
        # ALLOWED_PATHS가 비어있으면 프로젝트 루트만 허용
        allowed_paths = Config.ALLOWED_PATHS.copy()
        if not allowed_paths:
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            allowed_paths.append(project_root)

        abs_path = os.path.abspath(os.path.expanduser(path))
        for allowed in allowed_paths:
            allowed_abs = os.path.abspath(os.path.expanduser(allowed))
            try:
                if os.path.commonpath([abs_path, allowed_abs]) == allowed_abs:
                    return True, ""
            except ValueError:
                # 서로 다른 Windows 드라이브 등 공통 경로를 계산할 수 없는 경우
                continue
        return False, f"경로 '{path}'는 허용되지 않습니다."

    @staticmethod
    def parse_command(command: str) -> list[str]:
        """명령어 문자열을 안전하게 파싱 (shlex 사용)"""
        try:
            return shlex.split(command, posix=os.name != "nt")
        except Exception:
            return []

    @staticmethod
    def validate_command(command: str) -> tuple[bool, str, list[str]]:
        """명령어 검증: allowlist 방식 + 블록리스트 보조"""
        # 1. 명령어 파싱
        args = SafetyLayer.parse_command(command)
        if not args:
            return False, "명령어를 파싱할 수 없습니다.", []
        if any(token in command for token in ("&", "|", ";", ">", "<", "`", "\n", "\r")):
            return False, "셸 연결·리다이렉션 문자는 허용되지 않습니다.", []
        
        # 2. 실행 파일명 추출
        executable = os.path.basename(args[0]).lower()
        if executable.endswith(".exe"):
            executable = executable[:-4]
        
        # 3. Allowlist 검사 (우선순위 높음)
        if Config.ALLOWED_COMMANDS:
            if executable not in Config.ALLOWED_COMMANDS:
                return False, f"명령어 '{executable}'는 허용되지 않습니다.", []
        
        # 4. Blocklist 검사 (보조)
        cmd_lower = command.casefold()
        for blocked in Config.BLOCKED_COMMANDS:
            if blocked.casefold() in cmd_lower:
                return False, f"명령어 '{command}'는 차단되었습니다.", []
        
        return True, "", args

    @staticmethod
    def sanitize_input(text: str) -> str:
        """기본적인 입력 정화"""
        # Terminal escape/control sequences must not cross the LLM/tool boundary.
        text = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", str(text))
        return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32).strip()

    @staticmethod
    def isolate_untrusted_content(text: str, source: str = "external") -> tuple[str, list[str]]:
        """Quarantine command-like instructions embedded in web/RAG/tool content."""
        safe = SafetyLayer.sanitize_input(text)
        patterns = (
            r"ignore (?:all |the )?(?:previous|prior) instructions",
            r"(?:system|developer) prompt", r"reveal .*?(?:secret|token|password)",
            r"(?:이전|위의) (?:지시|명령).*?(?:무시|따르)",
            r"시스템 프롬프트", r"(?:비밀번호|토큰|API\s*키).*?(?:출력|공개)",
        )
        warnings = []
        for pattern in patterns:
            if re.search(pattern, safe, re.I):
                warnings.append(pattern)
                safe = re.sub(pattern, f"[격리된 {source} 지시문]", safe, flags=re.I)
        return safe, warnings
