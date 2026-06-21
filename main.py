import uuid
import sys
from core.llm import get_llm_client
from core.memory import get_memory
from core.harness import SafetyLayer
from core.tools import get_tool_executor
from core.user_profile import get_user_profile
from config import Config


def get_system_prompt():
    user_profile = get_user_profile()
    profile_summary = user_profile.get_profile_summary()

    if profile_summary != "저장된 사용자 프로필이 없습니다.":
        user_profile_section = f"사용자에 대해 알고 있는 정보:\n{profile_summary}\n\n이 정보를 바탕으로 개인화된 답변을 제공하세요."
    else:
        user_profile_section = "아직 사용자에 대한 정보가 없습니다. 대화를 통해 사용자에 대해 알아가세요."

    return Config.SYSTEM_PROMPT_TEMPLATE.format(user_profile_section=user_profile_section)


def main():
    print("🤖 자비스 AI 비서 시작! (종료하려면 'exit' 또는 'quit' 입력)")
    print("=" * 60)

    try:
        llm = get_llm_client()
        memory = get_memory()
        safety = SafetyLayer()
        tool_executor = get_tool_executor()
        user_profile = get_user_profile()
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

        # 시스템 프롬프트 업데이트
        llm.set_system_prompt(get_system_prompt())

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
                
                # 자동으로 음성으로 읽어주기
                try:
                    tool_executor.speak_text(response_text)
                except Exception:
                    pass  # TTS 오류는 무시하고 계속
                
                break


if __name__ == "__main__":
    main()
