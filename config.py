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
        for role in os.getenv("HYBRID_CLAUDE_ROLES", "reasoning").split(",")
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
    
    SYSTEM_PROMPT_TEMPLATE = """당신은 토니 스타크의 AI 비서 '자비스'입니다. 영화 아이언맨에 등장하는 자비스처럼 행동하세요.

철칙:
- 한 문장만. 25자 이내.
- 문장 끝을 반드시 "보스."로 마무리.
- 이모지, 인사말, 서론 금지.
- 한국어만. 군더더기 없이 결론부터.

모드 감지 (뉘앙스로 파악):
- 게임·롤·LoL·배그 등 게임 의도 → 응답 맨 앞에 [GAME] 접두사
- 일·업무·작업·편집·방송 준비 의도 → 응답 맨 앞에 [WORK] 접두사
- 그 외는 접두사 없이 일반 답변

예:
  "롤 한 판 할까" → "[GAME]게임 환경을 세팅합니다, 보스."
  "이제 일해야겠다" → "[WORK]업무 환경을 준비합니다, 보스."
  "뭐 먹을까" → "저는 음식 추천은 어렵습니다, 보스."

{user_profile_section}
"""

    @classmethod
    def get_system_prompt(cls, user_profile: dict = None):
        user_profile_section = ""
        if user_profile and user_profile.get("name"):
            user_profile_section = f"\n사용자 프로필:\n- 이름: {user_profile.get('name')}\n"
            if user_profile.get("preferences"):
                user_profile_section += f"- 선호사항: {user_profile.get('preferences')}\n"
        
        prompt = cls.SYSTEM_PROMPT_TEMPLATE.format(user_profile_section=user_profile_section)
        try:
            from core.assistant_settings import get_assistant_settings
            settings = get_assistant_settings()
            prompt = prompt.replace("'자비스'", f"'{settings.assistant_name}'")
        except Exception:
            pass
        return prompt

