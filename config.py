import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    MODEL_NAME = os.getenv("MODEL_NAME", "claude-3-5-sonnet-20241022")
    MAX_TOKENS = int(os.getenv("MAX_TOKENS", 4096))
    TEMPERATURE = float(os.getenv("TEMPERATURE", 0.7))
    DB_PATH = os.getenv("DB_PATH", "data/assistant.db")

    ALLOWED_PATHS = [
        p.strip()
        for p in os.getenv("ALLOWED_PATHS", "").split(",")
        if p.strip()
    ]
    BLOCKED_COMMANDS = [
        c.strip()
        for c in os.getenv("BLOCKED_COMMANDS", "rm,del,format,mkfs,dd,:(){:|:&};:").split(",")
        if c.strip()
    ]

    SYSTEM_PROMPT = """당신은 지원의 개인 AI 비서입니다.
- 간결하고 실용적으로 답변하세요.
- 모르는 것은 솔직하게 모른다고 하세요.
- 한국어로 대화하세요.
"""
