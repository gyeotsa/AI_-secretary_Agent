import json
from typing import List, Dict, Any
from core.llm import get_llm_client
from core.tools import get_tool_executor, get_tools_description_text


class ReActAgent:
    def __init__(self):
        self.llm = get_llm_client("reasoning")
        self.tool_executor = get_tool_executor()
        self.max_steps = 10

    def run(self, query: str) -> str:
        """
        ReAct 패턴으로 쿼리를 처리합니다:
        1. Think: 다음 행동을 생각합니다
        2. Act: 툴을 실행합니다
        3. Observe: 결과를 관찰합니다
        4. Repeat: 필요한 만큼 반복합니다
        """
        print(f"🤖 ReAct 에이전트 시작: {query}")
        print("-" * 50)

        messages = [
            {
                "role": "system",
                "content": """당신은 유용한 AI 비서입니다. 사용자의 요청을 처리하기 위해 ReAct 패턴을 사용하세요.

[중요 규칙]
1. 도구를 실행한 후에는 **반드시 "Final Answer:"로 시작하는 최종 답변을 일반 텍스트로 출력**해야 합니다.
2. 도구 실행 결과를 받은 후에는 절대 다시 Tool을 호출하지 마세요.
3. 최종 답변은 사용자에게 직접 전달될 내용이므로, Tool-call이 아닌 자연어로 작성하세요.

응답 형식:
- 질문을 이해했다면: "Final Answer: [답변]"
- 툴을 사용해야 한다면: "Tool: [툴이름]\nInput: [툴입력 (JSON 형식)]"

사용 가능한 툴 (이름(파라미터*: 필수): 설명, 이 목록에 없는 이름은 사용하지 마세요):
__TOOLS_TEXT__

예시:
사용자: 오늘 날씨 어때?
당신: Tool: web_search\nInput: {"query": "오늘 서울 날씨"}
(Observation: 오늘 서울은 맑고 기온은 25도입니다)
당신: Final Answer: 오늘 서울은 맑고 기온은 25도입니다!

사용자: 내 프로필 보여줘
당신: Tool: get_profile\nInput: {}
(Observation: 이름: 보스, 나이: 30)
당신: Final Answer: 보스님의 프로필은 이름: 보스, 나이: 30입니다!

사용자: 간단한 인사해줘
당신: Final Answer: 안녕하세요! 어떤 도움이 필요하신가요?"""
            },
            {"role": "user", "content": query}
        ]
        # get_tools_schema()가 유일한 진실 공급원입니다 (Planner, Executor와 동일한 목록 사용).
        # 이 문자열에는 리터럴 중괄호가 없으므로 단순 치환이면 충분합니다.
        messages[0]["content"] = messages[0]["content"].replace(
            "__TOOLS_TEXT__", get_tools_description_text()
        )

        for step in range(self.max_steps):
            print(f"\n📝 Step {step + 1}/{self.max_steps}")

            # Think + Act
            response = self.llm.chat(messages)
            print(f"💭 AI 응답:\n{response}")

            if "Final Answer:" in response:
                final_answer = response.split("Final Answer:")[-1].strip()
                print(f"\n✅ 최종 답변: {final_answer}")
                return final_answer

            if "Tool:" in response and "Input:" in response:
                # 툴 호출 파싱
                try:
                    tool_part = response.split("Tool:")[-1].split("Input:")[0].strip()
                    input_part = response.split("Input:")[-1].strip()

                    tool_name = tool_part
                    tool_input = json.loads(input_part)

                    print(f"🔧 툴 실행: {tool_name}")
                    print(f"📥 입력: {tool_input}")

                    # Observe
                    tool_result = self.tool_executor.execute_tool(tool_name, tool_input)
                    print(f"📤 결과:\n{tool_result}")

                    # 다음 단계를 위해 메시지에 추가
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"Observation: {tool_result}"})

                except Exception as e:
                    print(f"❌ 툴 실행 오류: {e}")
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"Error: {str(e)}"})
            else:
                # 툴을 사용하지 않고 바로 답변
                print(f"\n✅ 답변: {response}")
                return response

        print("\n⚠️ 최대 스텝을 초과했습니다")
        return "죄송합니다, 문제를 해결하는 데 시간이 너무 오래 걸렸어요. 조금 더 구체적으로 질문해주세요!"


# Singleton instance
_react_agent = None


def get_react_agent() -> ReActAgent:
    global _react_agent
    if _react_agent is None:
        _react_agent = ReActAgent()
    return _react_agent
