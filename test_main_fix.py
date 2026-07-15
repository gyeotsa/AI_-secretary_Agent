import sys
from config import Config

print("Config.LLM_PROVIDER:", Config.LLM_PROVIDER)
print("Config.OLLAMA_MODEL:", Config.OLLAMA_MODEL)
print("Config.DB_PATH:", Config.DB_PATH)
print("Config.ALLOWED_PATHS:", Config.ALLOWED_PATHS)
print("모든 Config 접근 테스트 성공!")
