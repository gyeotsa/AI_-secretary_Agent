import json
from types import SimpleNamespace

from core.conversation_context import ConversationContextResolver
from core.executor import Executor
from core.scratchpad import Observation


class _LLM:
    def chat(self, _messages):
        return json.dumps({
            "resolved_request": "오늘 서울시 구로구 항동의 현재 온도를 조회해줘",
            "topic": "weather", "entities": {"location": "서울시 구로구 항동", "date": "today"},
            "confidence": 0.97, "needs_clarification": False, "clarification_question": "",
        }, ensure_ascii=False)


def test_follow_up_request_inherits_location_from_recent_dialogue():
    resolver = ConversationContextResolver(_LLM())
    result = resolver.resolve("온도는 어느 정도야?", [
        {"role": "user", "content": "오늘 서울시 구로구 항동 날씨를 알려줘"},
        {"role": "assistant", "content": "날씨를 확인했습니다."},
    ], "session")
    assert "오늘 서울시 구로구 항동의 현재 온도를 조회해줘" in result.resolved_request
    assert "후속 질문의 핵심 요구: 온도는 어느 정도야?" in result.resolved_request
    assert "해석 근거가 된 최근 사용자 대화: 오늘 서울시 구로구 항동 날씨를 알려줘" in result.resolved_request
    assert result.entities["location"] == "서울시 구로구 항동"
    assert not result.needs_clarification


def test_unknown_json_tool_request_is_not_marked_as_simple_success():
    class ToolLLM:
        system_prompt = "original"
        def set_system_prompt(self, value): self.system_prompt = value
        def chat_with_tools(self, _messages):
            return '{"name":"invented_tool","arguments":{}}', []

    executor = Executor.__new__(Executor)
    executor.reasoning_llm = ToolLLM()
    executor._default_reasoning_prompt = "original"
    action = executor.decide_next_action(SimpleNamespace(description="가상 도구 실행"), "")
    assert action["action_type"] == "error"
    assert "invented_tool" in action["simple_result"]


def test_verified_weather_result_is_rendered_without_llm_hallucination():
    class NeverCalledLLM:
        def chat(self, _messages):
            raise AssertionError("검증된 날씨 결과는 LLM으로 재작성하면 안 됩니다")

    payload = {
        "requested_location": "서울시 구로구 항동",
        "location_precision": "city",
        "temperature_c": 27.8,
        "apparent_temperature_c": 34.2,
        "today_min_c": 22.5,
        "today_max_c": 28.8,
        "humidity_percent": 86,
    }
    executor = Executor.__new__(Executor)
    executor.llm = NeverCalledLLM()
    executor.goal = "항동 날씨를 알려줘\n후속 질문의 핵심 요구: 온도는?"
    executor.scratchpad = SimpleNamespace(observations=[
        Observation("get_weather", {"location": "서울시 구로구 항동"}, json.dumps(payload), True)
    ])
    executor.build_context = lambda: ""

    response = executor.generate_response()
    assert "27.8°C" in response
    assert "체감온도는 34.2°C" in response
    assert "서울시 기준 근사값" in response
