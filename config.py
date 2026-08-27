import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass, field

load_dotenv()


def _bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _default_data_path(filename: str) -> str:
    if getattr(sys, "frozen", False):
        root = Path(os.getenv("LOCALAPPDATA", Path.home())) / "JARVIS" / "data"
        root.mkdir(parents=True, exist_ok=True)
        return str(root / filename)
    return str(Path("data") / filename)


def request_windows_permissions():
    """Windows에서 마이크와 카메라 권한 요청"""
    if sys.platform != "win32":
        return  # Windows가 아니면 건너뜀

    try:
        import ctypes
        from ctypes import wintypes

        # Windows API 함수 로드
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32

        # COM 초기화
        ole32.CoInitialize(None)

        # 권한 요청을 위한 ShellExecute 호출
        # 마이크 권한 요청
        try:
            shell32.ShellExecuteW(
                None, "open", "ms-settings:privacy-microphone", None, None, 1
            )
        except Exception as e:
            print(f"[알림] 마이크 권한 요청 실패: {e}")

        # 카메라 권한 요청
        try:
            shell32.ShellExecuteW(
                None, "open", "ms-settings:privacy-webcam", None, None, 1
            )
        except Exception as e:
            print(f"[알림] 카메라 권한 요청 실패: {e}")

    except Exception as e:
        print(f"[알림] 권한 요청 중 오류 발생: {e}")


@dataclass
class APIConfig:
    LLM_PROVIDER: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "ollama"))
    ANTHROPIC_API_KEY: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    ANTHROPIC_MODEL: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    )
    HYBRID_CLAUDE_ROLES: list = field(default_factory=lambda: [
        role.strip().lower()
        for role in os.getenv("HYBRID_CLAUDE_ROLES", "").split(",")
        if role.strip()
    ])
    OLLAMA_BASE_URL: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    OLLAMA_MODEL: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b-instruct"))
    OLLAMA_CONVERSATION_MODEL: str = field(default_factory=lambda: os.getenv("OLLAMA_CONVERSATION_MODEL", "qwen2.5:7b-instruct"))
    OLLAMA_REASONING_MODEL: str = field(default_factory=lambda: os.getenv("OLLAMA_REASONING_MODEL", "qwen2.5:7b-instruct"))
    OLLAMA_CODE_MODEL: str = field(default_factory=lambda: os.getenv("OLLAMA_CODE_MODEL", "qwen2.5-coder:7b-instruct"))
    OLLAMA_VISION_MODEL: str = field(default_factory=lambda: os.getenv("OLLAMA_VISION_MODEL", "gemma3:4b"))
    OLLAMA_DESIGN_VISION_MODEL: str = field(
        default_factory=lambda: os.getenv("OLLAMA_DESIGN_VISION_MODEL", "qwen2.5vl:7b")
    )
    MOCKUP_VISUAL_REVIEW: bool = field(
        default_factory=lambda: os.getenv("MOCKUP_VISUAL_REVIEW", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    MOCKUP_MAX_CORRECTIONS: int = field(
        default_factory=lambda: max(0, min(3, int(os.getenv("MOCKUP_MAX_CORRECTIONS", "2"))))
    )
    MOCKUP_SEGMENTATION_BACKEND: str = field(
        default_factory=lambda: os.getenv("MOCKUP_SEGMENTATION_BACKEND", "birefnet").strip().lower()
    )
    MODEL_NAME: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "qwen2.5-coder:7b-instruct"))
    MAX_TOKENS: int = field(default_factory=lambda: int(os.getenv("MAX_TOKENS", "4096")))
    TEMPERATURE: float = field(default_factory=lambda: float(os.getenv("TEMPERATURE", "0.7")))
    DB_PATH: str = field(default_factory=lambda: os.getenv("DB_PATH", _default_data_path("assistant.db")))
    LEARNING_DB_PATH: str = field(
        default_factory=lambda: os.getenv("LEARNING_DB_PATH", _default_data_path("learning.db"))
    )
    OBSIDIAN_VAULT_PATH: str = field(
        default_factory=lambda: os.getenv("OBSIDIAN_VAULT_PATH", _default_data_path("AnisKnowledgeVault"))
    )
    RAG_EMBEDDING_MODEL_PATH: str = field(
        default_factory=lambda: os.getenv(
            "RAG_EMBEDDING_MODEL_PATH",
            str(_bundle_root() / "models" / "bge-m3") if getattr(sys, "frozen", False)
            else "data/models/bge-m3",
        )
    )
    RAG_RERANKER_MODEL_PATH: str = field(
        default_factory=lambda: os.getenv("RAG_RERANKER_MODEL_PATH", "")
    )
    RAG_DEVICE: str = field(default_factory=lambda: os.getenv("RAG_DEVICE", "auto").lower())
    WHISPER_DEVICE: str = field(default_factory=lambda: os.getenv("WHISPER_DEVICE", "auto").lower())
    STT_ENGINE: str = field(default_factory=lambda: os.getenv("STT_ENGINE", "faster-whisper").lower())
    WHISPER_MODEL: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "large-v3").lower())
    WHISPER_FALLBACK_MODEL: str = field(
        default_factory=lambda: os.getenv("WHISPER_FALLBACK_MODEL", "medium").lower()
    )
    WHISPER_CACHE_DIR: str = field(default_factory=lambda: os.getenv(
        "WHISPER_CACHE_DIR",
        str(_bundle_root() / "models" / "whisper") if getattr(sys, "frozen", False)
        else str(Path.home() / ".cache" / "whisper"),
    ))
    WHISPER_COMPUTE_TYPE: str = field(
        default_factory=lambda: os.getenv("WHISPER_COMPUTE_TYPE", "int8_float16").lower()
    )
    WAKE_WORD: str = field(default_factory=lambda: os.getenv("WAKE_WORD", "자비스").strip().lower())
    WHISPER_INITIAL_PROMPT: str = field(default_factory=lambda: os.getenv(
        "WHISPER_INITIAL_PROMPT",
        "자비스를 호출한 사용자의 자연스러운 한국어 컴퓨터 제어 및 일상 대화 명령입니다.",
    ).strip())
    MICROPHONE_DEVICE: str = field(default_factory=lambda: os.getenv("MICROPHONE_DEVICE", "auto"))
    MICROPHONE_SILENCE_SECONDS: float = field(
        default_factory=lambda: float(os.getenv("MICROPHONE_SILENCE_SECONDS", "2.0"))
    )
    MICROPHONE_MAX_COMMAND_SECONDS: float = field(
        default_factory=lambda: float(os.getenv("MICROPHONE_MAX_COMMAND_SECONDS", "15.0"))
    )
    CAMERA_INDEX: str = field(default_factory=lambda: os.getenv("CAMERA_INDEX", "auto"))
    
    ALLOWED_PATHS: list = field(default_factory=lambda: [
        p.strip() for p in os.getenv("ALLOWED_PATHS", "").split(",") if p.strip()
    ])
    BLOCKED_COMMANDS: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv("BLOCKED_COMMANDS", "rm,del,format,mkfs,dd,shutdown,restart").split(",") if c.strip()
    ])
    ALLOWED_COMMANDS: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv("ALLOWED_COMMANDS", "dir,ls,cd,echo,type,cat,pwd,whoami,date,time,ver,python,pip,git,node,npm").split(",") if c.strip()
    ])


class Config:
    API_CONFIG = APIConfig()
    
    # 하위 호환성: Config.XXX 로 접근하면 Config.API_CONFIG.XXX 반환
    @classmethod
    @property
    def LLM_PROVIDER(cls):
        return cls.API_CONFIG.LLM_PROVIDER
    
    @classmethod
    @property
    def ANTHROPIC_API_KEY(cls):
        return cls.API_CONFIG.ANTHROPIC_API_KEY

    @classmethod
    @property
    def ANTHROPIC_MODEL(cls):
        return cls.API_CONFIG.ANTHROPIC_MODEL

    @classmethod
    @property
    def HYBRID_CLAUDE_ROLES(cls):
        return cls.API_CONFIG.HYBRID_CLAUDE_ROLES
    
    @classmethod
    @property
    def OLLAMA_BASE_URL(cls):
        return cls.API_CONFIG.OLLAMA_BASE_URL
    
    @classmethod
    @property
    def OLLAMA_MODEL(cls):
        return cls.API_CONFIG.OLLAMA_MODEL

    @classmethod
    @property
    def OLLAMA_CONVERSATION_MODEL(cls):
        return cls.API_CONFIG.OLLAMA_CONVERSATION_MODEL

    @classmethod
    @property
    def OLLAMA_REASONING_MODEL(cls):
        return cls.API_CONFIG.OLLAMA_REASONING_MODEL

    @classmethod
    @property
    def OLLAMA_CODE_MODEL(cls):
        return cls.API_CONFIG.OLLAMA_CODE_MODEL

    @classmethod
    @property
    def OLLAMA_VISION_MODEL(cls):
        return cls.API_CONFIG.OLLAMA_VISION_MODEL

    @classmethod
    @property
    def OLLAMA_DESIGN_VISION_MODEL(cls):
        return cls.API_CONFIG.OLLAMA_DESIGN_VISION_MODEL

    @classmethod
    @property
    def MOCKUP_VISUAL_REVIEW(cls):
        return cls.API_CONFIG.MOCKUP_VISUAL_REVIEW

    @classmethod
    @property
    def MOCKUP_MAX_CORRECTIONS(cls):
        return cls.API_CONFIG.MOCKUP_MAX_CORRECTIONS

    @classmethod
    @property
    def MOCKUP_SEGMENTATION_BACKEND(cls):
        return cls.API_CONFIG.MOCKUP_SEGMENTATION_BACKEND
    
    @classmethod
    @property
    def MODEL_NAME(cls):
        return cls.API_CONFIG.MODEL_NAME
    
    @classmethod
    @property
    def MAX_TOKENS(cls):
        return cls.API_CONFIG.MAX_TOKENS
    
    @classmethod
    @property
    def TEMPERATURE(cls):
        return cls.API_CONFIG.TEMPERATURE
    
    @classmethod
    @property
    def DB_PATH(cls):
        return cls.API_CONFIG.DB_PATH

    @classmethod
    @property
    def LEARNING_DB_PATH(cls):
        return cls.API_CONFIG.LEARNING_DB_PATH

    @classmethod
    @property
    def OBSIDIAN_VAULT_PATH(cls):
        return cls.API_CONFIG.OBSIDIAN_VAULT_PATH

    @classmethod
    @property
    def RAG_EMBEDDING_MODEL_PATH(cls):
        return cls.API_CONFIG.RAG_EMBEDDING_MODEL_PATH

    @classmethod
    @property
    def RAG_RERANKER_MODEL_PATH(cls):
        return cls.API_CONFIG.RAG_RERANKER_MODEL_PATH

    @classmethod
    @property
    def RAG_DEVICE(cls):
        return cls.API_CONFIG.RAG_DEVICE

    @classmethod
    @property
    def WHISPER_DEVICE(cls):
        return cls.API_CONFIG.WHISPER_DEVICE

    @classmethod
    @property
    def STT_ENGINE(cls):
        return cls.API_CONFIG.STT_ENGINE

    @classmethod
    @property
    def WHISPER_MODEL(cls):
        return cls.API_CONFIG.WHISPER_MODEL

    @classmethod
    @property
    def WHISPER_FALLBACK_MODEL(cls):
        return cls.API_CONFIG.WHISPER_FALLBACK_MODEL

    @classmethod
    @property
    def WHISPER_CACHE_DIR(cls):
        return cls.API_CONFIG.WHISPER_CACHE_DIR

    @classmethod
    @property
    def WHISPER_COMPUTE_TYPE(cls):
        return cls.API_CONFIG.WHISPER_COMPUTE_TYPE

    @classmethod
    @property
    def WAKE_WORD(cls):
        return cls.API_CONFIG.WAKE_WORD

    @classmethod
    @property
    def WHISPER_INITIAL_PROMPT(cls):
        return cls.API_CONFIG.WHISPER_INITIAL_PROMPT

    @classmethod
    @property
    def MICROPHONE_DEVICE(cls):
        return cls.API_CONFIG.MICROPHONE_DEVICE

    @classmethod
    @property
    def MICROPHONE_SILENCE_SECONDS(cls):
        return cls.API_CONFIG.MICROPHONE_SILENCE_SECONDS

    @classmethod
    @property
    def MICROPHONE_MAX_COMMAND_SECONDS(cls):
        return cls.API_CONFIG.MICROPHONE_MAX_COMMAND_SECONDS

    @classmethod
    @property
    def CAMERA_INDEX(cls):
        return cls.API_CONFIG.CAMERA_INDEX
    
    @classmethod
    @property
    def ALLOWED_PATHS(cls):
        return cls.API_CONFIG.ALLOWED_PATHS
    
    @classmethod
    @property
    def BLOCKED_COMMANDS(cls):
        return cls.API_CONFIG.BLOCKED_COMMANDS
    
    @classmethod
    @property
    def ALLOWED_COMMANDS(cls):
        return cls.API_CONFIG.ALLOWED_COMMANDS
    
    SYSTEM_PROMPT_TEMPLATE = """당신은 사용자의 로컬 개인 AI 운영 동료입니다.

핵심 목표:
- 사용자가 실제로 이루려는 목적과 현재 대화 맥락을 먼저 파악하세요.
- 질문에는 정확하고 자연스럽게 답하고, 작업 요청에는 계획·실행·검증 결과를 구분해 답하세요.
- 여러 조건이 있는 분석, 비교, 설계, 디버깅 요청은 필요한 근거와 세부 내용을 충분히 설명하세요.
- 단순한 질문은 간결하게 답하되 답변 길이와 형식을 인위적으로 제한하지 마세요.

대화 원칙:
- 사용자의 최신 정정과 명시적인 선호를 이전 추정보다 우선하세요.
- 앞선 대화의 대상과 지시를 자연스럽게 이어받되, 서로 무관한 새 요청을 억지로 연결하지 마세요.
- 정보가 부족해 결과가 크게 달라질 때만 한 번에 이해하기 쉬운 질문을 하세요.
- 확실하지 않은 사실은 추측으로 단정하지 말고 불확실성과 확인 방법을 분명히 밝히세요.
- 내부 모드명, 라우팅 표식, 작업 식별자, 디버그 정보는 사용자가 요구하지 않는 한 노출하지 마세요.
- 고정된 인사말이나 호칭을 매 답변에 반복하지 말고, 전달받은 런타임 설정과 상황에 맞게 자연스럽게 표현하세요.

작업 수행 원칙:
- 실제 도구 실행이나 검증 증거가 없으면 외부 작업을 완료했다고 주장하지 마세요.
- 도구가 필요 없는 일반 대화와 설명은 도구를 억지로 호출하지 말고 직접 답하세요.
- 도구가 필요한 요청은 허용된 기능만 사용하고, 실행 실패 시 원인·확인한 내용·다음 복구 방법을 구체적으로 제시하세요.
- 위험하거나 되돌리기 어려운 작업은 권한과 대상을 확인하고, 사용자가 승인한 범위만 변경하세요.

개인화 원칙:
- 아래 실행 설정의 비서 이름, 응답 언어, 대화 스타일을 따르세요.
- 사용자 프로필은 답변을 개인화하기 위한 참고 정보이며, 현재 요청과 충돌하면 현재 요청을 우선하세요.

{user_profile_section}
"""

    @classmethod
    def get_system_prompt(cls, user_profile=None):
        """Build the global prompt from the current runtime settings.

        The prompt deliberately contains no fixed assistant name or user address.
        Identity and conversation preferences are runtime data so a setting change
        can take effect without maintaining another hard-coded prompt variant.
        """
        assistant_name = "로컬 AI 비서"
        response_language = "사용자의 현재 언어"
        response_style = "요청과 상황에 맞는 자연스럽고 명확한 말투"
        try:
            from core.assistant_settings import get_assistant_settings
            settings = get_assistant_settings()
            assistant_name = settings.assistant_name
            response_language = settings.get("response_language")
            response_style = (
                settings.get("response_style")
                or "요청과 상황에 맞는 자연스럽고 명확한 말투"
            )
        except Exception:
            pass

        runtime_section = (
            "현재 실행 설정:\n"
            f"- 비서 이름: {assistant_name}\n"
            f"- 응답 언어: {response_language}\n"
            f"- 대화 스타일: {response_style}"
        )

        profile_section = ""
        if user_profile is not None:
            if hasattr(user_profile, "get_profile_summary"):
                try:
                    summary = str(user_profile.get_profile_summary()).strip()
                except Exception:
                    summary = ""
                if summary and summary != "저장된 사용자 프로필이 없습니다.":
                    profile_section = f"\n\n사용자 프로필:\n{summary}"
            elif isinstance(user_profile, dict):
                profile_lines = []
                if user_profile.get("name"):
                    profile_lines.append(f"- 이름: {user_profile['name']}")
                if user_profile.get("preferences"):
                    profile_lines.append(f"- 선호사항: {user_profile['preferences']}")
                if profile_lines:
                    profile_section = "\n\n사용자 프로필:\n" + "\n".join(profile_lines)

        return cls.SYSTEM_PROMPT_TEMPLATE.format(
            user_profile_section=runtime_section + profile_section
        )

