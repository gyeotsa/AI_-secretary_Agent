import uuid
import sys
import json
from core.llm import get_llm_client
from core.memory import get_memory
from core.harness import SafetyLayer
from core.tools import get_tool_executor
from core.user_profile import get_user_profile
from core.react import get_react_agent
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
    voice_mode = "--voice" in sys.argv
    react_mode = "--react" in sys.argv
    
    print("자비스 AI 비서 시작! (종료하려면 'exit' 또는 'quit' 입력)")
    if voice_mode:
        print("음성 모드가 활성화되었습니다!")
    if react_mode:
        print("ReAct 자율 에이전트 모드가 활성화되었습니다!")
    print("=" * 60)

    try:
        llm = get_llm_client()
        memory = get_memory()
        safety = SafetyLayer()
        tool_executor = get_tool_executor()
        user_profile = get_user_profile()
        if react_mode:
            react_agent = get_react_agent()
    except Exception as e:
        print(f"초기화 오류: {e}")
        print(".env 파일에 API 키를 설정했는지 확인하세요.")
        return

    # 세션 선택
    sessions = memory.list_sessions()
    session_id = None
    messages = []
    
    if sessions:
        print("\n저장된 대화 세션:")
        for i, (sid, start_time) in enumerate(sessions, 1):
            print(f"{i}. {start_time}")
        
        choice = input("\n세션을 선택하세요 (번호 입력, 새로 시작하려면 그냥 Enter): ").strip()
        
        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            session_id = sessions[int(choice)-1][0]
            messages = memory.load_session(session_id)
            print(f"\n{len(messages)}개의 메시지를 불러왔습니다!")
        else:
            session_id = str(uuid.uuid4())
            print("\n새로운 세션을 시작합니다!")
    else:
        session_id = str(uuid.uuid4())
        print("\n새로운 세션을 시작합니다!")

    while True:
        try:
            if voice_mode:
                # 음성 모드: listen 툴 사용
                print("\n음성 입력을 기다리는 중...")
                listen_result = tool_executor.listen()
                print(f"나: {listen_result}")
                if "음성 인식 결과: " in listen_result:
                    user_input = listen_result.replace("음성 인식 결과: ", "")
                else:
                    user_input = ""
            else:
                # 텍스트 모드: 입력 받기
                user_input = input("\n나: ").strip()
            
            user_input = safety.sanitize_input(user_input)
        except (KeyboardInterrupt, EOFError):
            print("\n\n안녕히 가세요!")
            break

        if user_input.lower() in ("exit", "quit", "종료"):
            print("안녕히 가세요!")
            break

        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})
        memory.save_message(session_id, "user", user_input)

        if react_mode:
            # ReAct 모드: 자율 에이전트 사용
            print("\n🧠 ReAct 에이전트 처리 중...")
            final_answer = react_agent.run(user_input)
            print("\n비서:", final_answer)
            
            messages.append({"role": "assistant", "content": final_answer})
            memory.save_message(session_id, "assistant", final_answer)
            
            # 자동으로 음성으로 읽어주기
            try:
                tool_executor.speak_text(final_answer)
            except Exception:
                pass  # TTS 오류는 무시하고 계속
        else:
            # 일반 모드: Tool Use 루프
            # 시스템 프롬프트 업데이트
            llm.set_system_prompt(get_system_prompt())

            while True:
                print("비서: ", end="", flush=True)
                response_text, tool_uses = llm.chat_with_tools(messages)

                if tool_uses:
                    # 툴 실행
                    assistant_content = []
                    tool_results = []
                    for tool_use in tool_uses:
                        # Anthropic은 객체, Ollama는 dict로 반환됨
                        if hasattr(tool_use, 'id'):
                            tool_id = tool_use.id
                            tool_name = tool_use.name
                            tool_input = tool_use.input
                        else:
                            tool_id = tool_use.get('id')
                            tool_name = tool_use.get('name')
                            tool_input = tool_use.get('input')
                        
                        print(f"[툴 실행 중: {tool_name}]")
                        
                        # 툴 실행
                        tool_result = tool_executor.execute_tool(tool_name, tool_input)
                        print(f"[툴 결과: {tool_name} 완료]")
                        
                        # Ollama용 메시지 추가
                        assistant_content.append(f"사용자: {tool_name} 툴을 실행했습니다. 결과: {tool_result}")
                        tool_results.append(tool_result)

                    # Ollama는 content에 툴 결과를 텍스트로 넣어줌
                    if Config.LLM_PROVIDER == "ollama":
                        messages.append({
                            "role": "user",
                            "content": f"다음 툴을 실행했습니다: {json.dumps([{'tool': t[0], 'result': t[1]} for t in zip([tu.get('name', '') for tu in tool_uses], tool_results)], ensure_ascii=False)}"
                        })
                    else:
                        # Anthropic용
                        assistant_content_objects = []
                        tool_use_ids = []
                        for tool_use in tool_uses:
                            if hasattr(tool_use, 'id'):
                                tool_id = tool_use.id
                                tool_name = tool_use.name
                                tool_input = tool_use.input
                            else:
                                tool_id = tool_use.get('id')
                                tool_name = tool_use.get('name')
                                tool_input = tool_use.get('input')
                            
                            assistant_content_objects.append({
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": tool_input,
                            })
                            tool_use_ids.append(tool_id)
                        
                        messages.append({"role": "assistant", "content": assistant_content_objects})
                        
                        for i, tool_use in enumerate(tool_uses):
                            if hasattr(tool_use, 'id'):
                                tool_id = tool_use.id
                            else:
                                tool_id = tool_use.get('id')
                            
                            messages.append({
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": tool_results[i],
                                    }
                                ],
                            })
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
