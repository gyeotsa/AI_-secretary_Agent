import sys
import os

# 프로젝트 루트 경로 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from core.llm import get_llm_client

print("[Ollama 연결 테스트 시작...]")
print(f"LLM 제공자: {Config.LLM_PROVIDER}")
print(f"Ollama 모델: {Config.OLLAMA_MODEL}")
print(f"Ollama URL: {Config.OLLAMA_BASE_URL}")
print()

try:
    llm = get_llm_client()
    print("[LLM 클라이언트 초기화 성공!]")
    
    # 간단한 대화 테스트
    print()
    print("[간단한 대화 테스트...]")
    response = llm.chat([{"role": "user", "content": "안녕! 너는 누구야?"}])
    print(f"응답: {response}")
    
    print()
    print("[모든 테스트 성공! main.py를 실행하세요.]")
    
except Exception as e:
    print(f"[오류 발생: {e}]")
    print()
    print("[해결 방법:]")
    print("1. Ollama가 실행 중인지 확인: 'ollama list' 명령 실행")
    print("2. Llama 3 모델이 다운로드된지 확인: 'ollama list'로 확인")
    print("3. 모델이 없다면: 'ollama pull llama3' 실행")
