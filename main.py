import uuid
import sys
from core.llm import get_llm_client
from core.memory import get_memory
from core.harness import SafetyLayer
from core.tools import get_tool_executor


def main():
    print("🤖 자비스 AI 비서 시작! (종료하려면 'exit' 또는 'quit' 입력)")
    print("=" * 60)

    try:
        llm = get_llm_client()
        memory = get_memory()
        safety = SafetyLayer()
        tool_executor = get_tool_executor()
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

        # Tool Use 루프
        while True:
            print("비서: ", end="", flush=True)
            response_text, tool_uses = llm.chat_with_tools(messages)

            if tool_uses:
                # 툴 실행
                assistant_content = []
                for tool_use in tool_uses:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tool_use.id,
                        "name": tool_use.name,
                        "input": tool_use.input,
                    })
                    print(f"[툴 실행 중: {tool_use.name}]")

                messages.append({"role": "assistant", "content": assistant_content})

                # 툴 결과 수집
                for tool_use in tool_uses:
                    tool_result = tool_executor.execute_tool(tool_use.name, tool_use.input)
                    messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": tool_result,
                            }
                        ],
                    })
                    print(f"[툴 결과: {tool_use.name} 완료]")
            else:
                # 최종 응답
                print(response_text)
                messages.append({"role": "assistant", "content": response_text})
                memory.save_message(session_id, "assistant", response_text)
                break


if __name__ == "__main__":
    main()
