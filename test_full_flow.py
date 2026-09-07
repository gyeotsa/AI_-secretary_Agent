"""Opt-in end-to-end LLM diagnostic.

The old version executed an LLM call, wrote conversation memory, and played
audio merely by being imported. Those operations are now explicit command
line options so pytest collection cannot mutate user state.
"""

from __future__ import annotations

import argparse
import re
import threading
import uuid


def run_full_flow_diagnostic(
    user_input: str,
    *,
    persist_memory: bool = False,
    speak: bool = False,
    initialize_support_services: bool = False,
) -> dict:
    """Run a live LLM turn; persistence and audio require explicit opt-in."""
    from config import Config
    from core.llm import get_llm_client
    from core.memory import get_memory
    from core.tools import get_tool_executor
    from core.user_profile import get_user_profile

    print("전체 흐름 수동 진단 시작...")
    user_profile = get_user_profile()
    llm = get_llm_client()
    llm.set_system_prompt(Config.get_system_prompt(user_profile))

    if initialize_support_services:
        from core.rag import get_rag_manager

        get_rag_manager()

    messages = [{"role": "user", "content": user_input}]
    print(f"사용자 입력: {user_input}")
    print("LLM 호출 중...")
    response_text, tool_uses = llm.chat_with_tools(messages)
    response_text = re.sub(r"[^\w\s가-힣.,!?]", "", response_text)
    print(f"LLM 응답: {response_text}")
    print(f"Tool uses: {tool_uses}")

    session_id = f"manual_diagnostic_{uuid.uuid4().hex}"
    if persist_memory:
        memory = get_memory()
        memory.save_message(session_id, "user", user_input)
        memory.save_message(session_id, "assistant", response_text)
        print(f"진단 대화를 저장했습니다: {session_id}")
    else:
        print("대화 저장 생략 (--persist-memory로 명시적으로 활성화)")

    speech_result = None
    if speak:
        tool_executor = get_tool_executor()
        state: dict[str, object] = {}

        def speak_in_thread() -> None:
            try:
                state["result"] = tool_executor.speak_text(response_text)
            except BaseException as exc:
                state["error"] = exc

        thread = threading.Thread(target=speak_in_thread, daemon=True)
        thread.start()
        thread.join()
        if "error" in state:
            raise RuntimeError("TTS 수동 진단에 실패했습니다") from state["error"]
        speech_result = state.get("result")
        print(f"TTS 결과: {speech_result}")
    else:
        print("음성 재생 생략 (--speak로 명시적으로 활성화)")

    print("전체 흐름 수동 진단 완료!")
    return {
        "response_text": response_text,
        "tool_uses": tool_uses,
        "session_id": session_id if persist_memory else None,
        "speech_result": speech_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 LLM 전체 흐름 수동 진단")
    parser.add_argument("--prompt", default="자비스 날씨")
    parser.add_argument("--persist-memory", action="store_true")
    parser.add_argument("--speak", action="store_true")
    parser.add_argument("--initialize-support-services", action="store_true")
    args = parser.parse_args()
    run_full_flow_diagnostic(
        args.prompt,
        persist_memory=args.persist_memory,
        speak=args.speak,
        initialize_support_services=args.initialize_support_services,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
