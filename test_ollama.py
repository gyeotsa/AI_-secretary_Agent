"""Manual live Ollama HTTP diagnostic.

No request is sent at import or pytest collection time.
"""

from __future__ import annotations

import argparse
import json


def run_ollama_diagnostic(*, timeout: float = 30.0) -> dict:
    import requests

    from config import Config

    base_url = Config.OLLAMA_BASE_URL.rstrip("/")
    print(f"Ollama Base URL: {base_url}")
    print(f"Ollama Model: {Config.OLLAMA_MODEL}")

    tags_response = requests.get(f"{base_url}/api/tags", timeout=min(timeout, 10.0))
    tags_response.raise_for_status()
    tags = tags_response.json()
    print(f"사용 가능한 모델: {json.dumps(tags, indent=2, ensure_ascii=False)}")

    payload = {
        "model": Config.OLLAMA_MODEL,
        "messages": [{"role": "user", "content": "안녕하세요!"}],
        "stream": False,
    }
    chat_response = requests.post(
        f"{base_url}/api/chat", json=payload, timeout=timeout
    )
    chat_response.raise_for_status()
    chat = chat_response.json()
    print(f"응답 내용: {json.dumps(chat, indent=2, ensure_ascii=False)}")
    return {"tags": tags, "chat": chat}


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 Ollama 서버 수동 진단")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    run_ollama_diagnostic(timeout=args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
