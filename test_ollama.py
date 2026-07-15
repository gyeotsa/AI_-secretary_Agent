
import requests
import json
from config import Config

print(f"Ollama Base URL: {Config.OLLAMA_BASE_URL}")
print(f"Ollama Model: {Config.OLLAMA_MODEL}")

try:
    # Ollama 상태 확인
    response = requests.get(f"{Config.OLLAMA_BASE_URL}/api/tags", timeout=10)
    print(f"\nOllama 연결 성공! 상태 코드: {response.status_code}")
    print(f"사용 가능한 모델: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
    # 간단한 채팅 테스트
    print("\n간단한 채팅 테스트...")
    payload = {
        "model": Config.OLLAMA_MODEL,
        "messages": [{"role": "user", "content": "안녕하세요!"}],
        "stream": False
    }
    response = requests.post(f"{Config.OLLAMA_BASE_URL}/api/chat", json=payload, timeout=30)
    print(f"채팅 테스트 응답: {response.status_code}")
    print(f"응답 내용: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    
except Exception as e:
    print(f"\n오류 발생: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
