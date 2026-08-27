import json
from types import SimpleNamespace

from core.conversation_context import ConversationContextResolver, ResolvedRequest
from core.dialogue_state import DialogueStateStore
from core.executor import Executor
from core.scratchpad import Observation
from core.intent_router import IntentRouter, IntentResolution
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
    assert result.resolved_request == "오늘 서울시 구로구 항동의 현재 온도를 조회해줘"
    assert result.entities["location"] == "서울시 구로구 항동"
    assert result.relation == "follow_up"
    assert result.context_used
    assert not result.needs_clarification


def test_independent_message_request_never_receives_previous_stock_context():
    class NeverCalledLLM:
        def chat(self, _messages):
            raise AssertionError("독립 요청에는 문맥 재작성 LLM을 호출하면 안 됩니다")

    resolver = ConversationContextResolver(NeverCalledLLM())
    request = "카카오 톡으로 형택이에게 테스트 라고 보내줄래?"
    result = resolver.resolve(request, [
        {"role": "user", "content": "삼성전자 주식 분석해줄래?"},
        {"role": "assistant", "content": "삼성전자 검색 결과를 열었습니다."},
    ], "session")

    assert result.resolved_request == request
    assert result.relation == "independent"
    assert not result.context_used


def test_elliptical_visual_edit_uses_recent_context_without_domain_keywords():
    class EditLLM:
        def chat(self, _messages):
            return json.dumps({
                "resolved_request": "현재 시안의 문구 색상을 빨간색으로 변경해줘",
                "topic": "mockup_edit",
                "entities": {"target": "현재 시안의 문구", "color": "빨간색"},
                "confidence": 0.94,
                "needs_clarification": False,
                "clarification_question": "",
                "relation": "follow_up",
                "context_used": True,
            }, ensure_ascii=False)

    resolver = ConversationContextResolver(EditLLM())
    result = resolver.resolve("빨간색으로 바꿔줘", [
        {"role": "user", "content": "현재 시안의 문구를 조금 아래로 내려줘"},
        {"role": "assistant", "content": "문구를 아래로 이동했습니다."},
    ], "design-session")

    assert result.context_used
    assert result.resolved_request == "현재 시안의 문구 색상을 빨간색으로 변경해줘"
    assert result.entities["target"] == "현재 시안의 문구"


def test_context_is_resolved_before_intent_routing(tmp_path):
    class ContextResolver:
        def resolve(self, request, _history, _session_id):
            return ResolvedRequest(
                request,
                "현재 시안의 문구 색상을 빨간색으로 변경해줘",
                topic="mockup_edit",
                confidence=0.95,
                relation="follow_up",
                context_used=True,
            )

    class RecordingRouter:
        def __init__(self):
            self.registry = PluginRegistry()
            self.seen = []

        def resolve(self, text, *_args):
            self.seen.append(text)
            if text == "빨간색으로 바꿔줘":
                return IntentResolution()
            return IntentResolution(
                matched=True,
                capability_response="문맥이 복원된 요청입니다.",
                request_type="capability",
            )

        def is_contextual_follow_up(self, *_args):
            return False

    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor.intent_router = RecordingRouter()
    executor.context_resolver = ContextResolver()
    executor.tool_executor = SimpleNamespace()
    executor.llm = SimpleNamespace()
    executor._progress_callback = None
    executor.current_agent_task_id = ""
    executor._task_controls = {}

    outcome = executor.execute_turn(
        "빨간색으로 바꿔줘",
        "design-session",
        [{"role": "assistant", "content": "문구를 아래로 이동했습니다."}],
    )

    assert outcome.response == "문맥이 복원된 요청입니다."
    assert executor.intent_router.seen[-1] == "현재 시안의 문구 색상을 빨간색으로 변경해줘"


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
