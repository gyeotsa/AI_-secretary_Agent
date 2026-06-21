import uuid
import sys
from core.llm import get_llm_client
from core.memory import get_memory
from core.harness import SafetyLayer


def main():
    print("🤖 자비스 AI 비서 시작! (종료하려면 'exit' 또는 'quit' 입력)")
    print("=" * 60)

    try:
        llm = get_llm_client()
        memory = get_memory()
        safety = SafetyLayer()
    except Exception as e:
        print(f"❌ 초기화 오류: {e}")
        print("💡 .env 파일에 API 키를 설정했는지 확인하세요.")
        return

    session_id = str(uuid.uuid4())
    messages = []

    while True:
        try:
            user_input = input("\n나: ").strip()
            user_input = safety.sanitize_input(user_input)
        except (KeyboardInterrupt, EOFError):
            print("\n\n👋 안녕히 가세요!")
            break

        if user_input.lower() in ("exit", "quit", "종료"):
            print("👋 안녕히 가세요!")
            break

        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        memory.save_message(session_id, "user", user_input)

        print("비서: ", end="", flush=True)
        response = llm.chat(messages)
        print(response)

        messages.append({"role": "assistant", "content": response})
        memory.save_message(session_id, "assistant", response)


if __name__ == "__main__":
    main()
