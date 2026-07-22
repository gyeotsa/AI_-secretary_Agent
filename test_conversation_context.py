import json
from types import SimpleNamespace

from core.conversation_context import ConversationContextResolver
from core.executor import Executor
from core.scratchpad import Observation
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from plugins.calendar import CalendarPlugin


def _calendar_router():
    registry = PluginRegistry()
    registry.register_plugin(CalendarPlugin())
    return IntentRouter(registry)


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


def test_calendar_capability_question_does_not_enter_tool_pipeline():
    resolution = _calendar_router().resolve("너 .ics 일정 파일 생성은 가능한 거 아니었어?")
    assert "가능합니다" in resolution.capability_response


def test_calendar_goal_blocks_unrelated_mail_and_weather_tools():
    executor = Executor.__new__(Executor)
    executor.goal = "바탕화면에 .ics 일정 파일을 생성해줘"
    executor.intent_router = _calendar_router()
    assert "관련 없는 도구" in executor._tool_domain_error({
        "action_type": "use_tool", "tool_name": "mail_create_draft", "tool_input": {},
    })
    assert "관련 없는 도구" in executor._tool_domain_error({
        "action_type": "use_tool", "tool_name": "get_weather", "tool_input": {},
    })
    assert executor._tool_domain_error({
        "action_type": "use_tool", "tool_name": "calendar_create_event", "tool_input": {},
    }) is None


def test_calendar_creation_without_times_asks_before_planning():
    resolution = _calendar_router().resolve(
        "바탕화면에 123이라는 이름으로 캘린더 파일 하나 생성해줘"
    )
    assert resolution.question == "일정은 언제 시작하나요, 보스?"
    assert resolution.slots["title"] == "123"
    assert resolution.slots["path"].endswith("Desktop\\123.ics")


def test_calendar_goal_restricts_llm_to_calendar_tools():
    executor = Executor.__new__(Executor)
    executor.intent_router = _calendar_router()
    assert executor._allowed_tools_for_goal("바탕화면에 123.ics를 만들어줘") == [
        "calendar_create_event",
    ]


def test_llm_timeout_text_is_not_treated_as_simple_success():
    class TimeoutLLM:
        system_prompt = "original"
        def set_system_prompt(self, value): self.system_prompt = value
        def chat_with_tools(self, _messages, _allowed=None):
            return "오류가 발생했습니다: Read timed out. (read timeout=120)", []

    executor = Executor.__new__(Executor)
    executor.reasoning_llm = TimeoutLLM()
    executor._default_reasoning_prompt = "original"
    executor.goal = "캘린더 파일 생성"
    executor.intent_router = _calendar_router()
    action = executor.decide_next_action(SimpleNamespace(description="캘린더 생성"), "")
    assert action["action_type"] == "error"
    assert "timed out" in action["simple_result"]
