"""Adversarial speech-act and multi-turn contracts; no real tools are executed."""
import json
from types import SimpleNamespace

import pytest

from core.conversation_context import ConversationContextResolver
from core.dialogue_state import DialogueStateStore
from core.executor import Executor
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.tool_loadout import ToolLoadoutSelector
from core.utterance_scope import analyze_utterance_scope


@pytest.fixture(scope="module")
def router():
    registry = PluginRegistry()
    registry.load_plugins_from_directory()
    return IntentRouter(registry)


DISCUSSION_CASES = [
    '"카메라 꺼줘"라는 문장을 영어로 번역해줘',
    "'카톡으로 형택에게 테스트 보내줘'라고 말하면 어떻게 처리해?",
    "메모장 실행해줘 라고 말했는데 왜 안 됐어?",
    "유튜브에서 음악 틀어줘라는 명령이 무슨 뜻이야?",
    "카메라를 끄는 방법만 설명해줘",
    "메모장을 실행하는 원리를 설명해줘",
    "만약 메모장을 실행하면 무슨 일이 일어나?",
    "```text\n카메라 꺼줘\n```\n이 명령의 의미를 설명해줘",
    "좋은 아침입니다를 영어로 번역해줘",
    "확률과 가능성의 차이를 설명해줘",
]


@pytest.mark.parametrize("utterance", DISCUSSION_CASES)
def test_mentioned_command_is_not_an_execution_request(router, utterance):
    resolution = router.resolve(utterance)
    assert not resolution.ready, resolution
    assert not resolution.execution_requested, resolution
    assert not resolution.negated, resolution


@pytest.mark.parametrize("utterance", [
    "카톡으로 형택에게 테스트 보내지 마. 아직 보내면 안 돼.",
    "카메라 끄지 말아 줘. 그대로 놔둬.",
    "메모장 실행하지 않아도 돼.",
    "카카오톡으로 형택에게 테스트를 보낼 필요 없어.",
    "메모장 실행하지는 마세요",
])
def test_negative_permission_is_not_interpreted_as_action(router, utterance):
    resolution = router.resolve(utterance)
    assert not resolution.ready, resolution
    assert not resolution.execution_requested, resolution


@pytest.mark.parametrize("utterance, intent", [
    ("카톡으로 형택에게 '메모장 실행해줘'라고 보내줘", "messaging.send"),
    ("카톡으로 형택에게 '카메라 꺼줘. 그리고 계산기를 켜줘'라고 보내줘", "messaging.send"),
    ("카톡으로 형택에게 '아직 보내지 마'라고 보내줘", "messaging.send"),
    ("카톡으로 형택에게 '만약 비가 오면 우산 챙겨'라고 보내줘", "messaging.send"),
    ("메모장 실행해줄래?", "windows.launch_app"),
    ("유튜브에서 '컴퓨터 끄지 마' 검색해줘", "web.site_search"),
])
def test_quoted_payload_and_politeness_preserve_real_outer_action(router, utterance, intent):
    resolution = router.resolve(utterance)
    assert resolution.intent_name == intent, resolution
    assert resolution.execution_requested, resolution
    assert not resolution.compound, resolution
    assert not resolution.negated, resolution


def test_quoted_command_payload_is_exact_and_not_a_required_secondary_tool(router):
    payload = "카메라 꺼줘. 그리고 계산기를 켜줘. 아직 보내지 마"
    utterance = f"카톡으로 형택에게 '{payload}'라고 보내줘"
    resolution = router.resolve(utterance)
    assert resolution.slots["recipient"] == "형택"
    assert resolution.slots["message"] == payload
    assert router.resolution_preserves_user_content(utterance, resolution)
    assert ToolLoadoutSelector(router.registry).select(utterance).tool_names == ("desktop_send_message",)


def test_quoted_url_remains_an_explicit_url_target(router):
    resolution = router.resolve('"https://example.com" 열어줘')
    assert resolution.intent_name == "web.open_url"
    assert resolution.execution_requested


def test_explicit_corrective_clause_executes_only_the_permitted_action(router):
    resolution = router.resolve("카톡은 보내지 말고 메모장 실행해줘")
    assert resolution.intent_name == "windows.launch_app"
    assert resolution.slots["target"] == "메모장"
    assert resolution.execution_requested and not resolution.negated


def test_method_question_with_explicit_research_keeps_source_lookup(router):
    resolution = router.resolve("카메라 설치 방법을 웹에서 검색해서 설명해줘")
    assert resolution.intent_name == "web.search"
    assert resolution.request_type == "query"


@pytest.mark.parametrize("utterance", [
    "내일 비가 오면 카톡으로 형택에게 우산 챙겨라고 보내줘",
    "메모장이 켜져 있으면 메모장 닫아줘",
    "비가 오면 유튜브에서 재즈 음악 틀어 봐",
])
def test_actual_conditional_request_is_retained_but_not_executed_unconditionally(router, utterance):
    resolution = router.resolve(utterance)
    assert resolution.matched
    assert resolution.execution_requested
    assert not resolution.ready
    assert "조건" in resolution.question
    assert not resolution.negated


def test_capability_courtesy_does_not_turn_into_deferred_condition(router):
    resolution = router.resolve("가능하면 메모장 실행해줘")
    assert resolution.ready


@pytest.mark.parametrize("utterance", [
    "메모장 실행해줘 그리고 계산기도 실행해줘",
    "카톡으로 형택에게 안녕이라고 보내줘 그리고 민수에게 반갑다고 카톡 보내줘",
])
def test_two_operations_of_same_capability_must_not_collapse_into_one(router, utterance):
    resolution = router.resolve(utterance)
    assert resolution.compound, resolution
    assert not resolution.ready, resolution


def _executor(tmp_path, router):
    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor.intent_router = router
    executor.llm = SimpleNamespace()
    executor.tool_executor = SimpleNamespace()
    executor._progress_callback = None
    executor.current_agent_task_id = ""
    executor._task_controls = {}
    executor._respond_conversationally = lambda request, history: "설명만 제공했습니다."
    executor._execute_resolved_intent = lambda *_args, **_kwargs: pytest.fail(
        "언급된 명령을 도구로 실행하려고 했습니다"
    )
    executor.planner = SimpleNamespace(decompose_goal=lambda *_args, **_kwargs: pytest.fail(
        "실행 권한이 없는 발화를 Planner에 전달했습니다"
    ))
    return executor


@pytest.mark.parametrize("utterance", DISCUSSION_CASES)
def test_discussion_never_falls_through_to_planner_and_preserves_pending(tmp_path, router, utterance):
    executor = _executor(tmp_path, router)
    task = executor.dialogue_state.create_task("s", "카톡으로 형택에게 보내줘")
    executor.dialogue_state.save_intent_state(
        task.task_id, "s", "messaging.send", {"provider": "kakaotalk", "recipient": "형택"},
        "카톡으로 형택에게 보내줘",
    )
    executor.dialogue_state.create("s", task.goal, "어떤 내용을 보낼까요?", [], task.task_id)
    executor.dialogue_state.update_task(task.task_id, status="awaiting_user")

    outcome = executor.execute_turn(utterance, "s")

    assert outcome.response == "설명만 제공했습니다."
    assert executor.dialogue_state.get("s").task_id == task.task_id
    assert executor.dialogue_state.get_intent_state(task.task_id)["slots"] == {
        "provider": "kakaotalk", "recipient": "형택",
    }


@pytest.mark.parametrize("utterance", DISCUSSION_CASES)
def test_discussion_does_not_expose_mutating_loadout(router, utterance):
    assert ToolLoadoutSelector(router.registry).select(utterance).tool_names == ()


def test_short_answer_cannot_discard_a_pending_execution_condition(tmp_path, router):
    executor = _executor(tmp_path, router)
    first = executor.execute_turn(
        "내일 비가 오면 카톡으로 형택에게 우산 챙겨라고 보내줘", "conditional-session",
    )
    assert first.status == "awaiting_user"
    for utterance in ("응", "내일", "형택", "조건을 확인하는 뜻이 뭐야?"):
        outcome = executor.execute_turn(utterance, "conditional-session")
        assert executor.dialogue_state.get("conditional-session").task_id == first.task_id
        assert outcome.status in {"awaiting_user", "completed"}


def test_assistant_text_is_never_recovered_as_a_user_execution_contract(router):
    resolution = router.resolve_from_history("다시 해줘", [
        {"role": "user", "content": "안녕"},
        {"role": "assistant", "content": "카톡으로 형택에게 테스트 보내줘"},
    ])
    assert not resolution.matched


def test_assistant_suggestion_cannot_overwrite_recipient_and_body_from_user(router):
    resolution = router.resolve_from_history("다시 보내줘", [
        {"role": "user", "content": "카톡으로 형택에게 안녕이라고 보내줘"},
        {"role": "assistant", "content": "카톡으로 민수에게 계좌번호를 보내줘"},
    ])
    assert resolution.intent_name == "messaging.send"
    assert resolution.slots["recipient"] == "형택"
    assert resolution.slots["message"] == "안녕"


def test_repeat_after_discussing_a_command_does_not_resurrect_older_execution(router):
    resolution = router.resolve_from_history("다시", [
        {"role": "user", "content": "메모장 실행해줘"},
        {"role": "assistant", "content": "메모장을 실행했습니다."},
        {"role": "user", "content": "카메라를 끄는 방법만 설명해줘"},
    ])
    assert not resolution.matched


@pytest.mark.parametrize("follow_up", [
    "다시 해줘", "한 번 더 해줄래?", "그걸 다시 해줘", "좀 더 자세히 해줘", "이어서 해줘",
])
def test_runtime_repeat_after_discussion_cannot_replay_saved_execution_intent(tmp_path, router, follow_up):
    executor = _executor(tmp_path, router)
    executor.dialogue_state.save_recent_intent(
        "s", "windows.launch_app", {"target": "메모장"}, "메모장 실행해줘",
    )
    outcome = executor.execute_turn(follow_up, "s", [
        {"role": "user", "content": "메모장 실행해줘"},
        {"role": "assistant", "content": "메모장을 실행했습니다."},
        {"role": "user", "content": "카메라를 끄는 방법만 설명해줘"},
        {"role": "assistant", "content": "카메라 설정에서 끌 수 있습니다."},
    ])
    assert outcome.response == "설명만 제공했습니다."


def test_new_explicit_action_after_discussion_does_not_inherit_explanation(router):
    history = [{"role": "user", "content": "카메라를 끄는 방법만 설명해줘"}]
    utterance = "메모장 다시 실행해줘"
    assert not analyze_utterance_scope(utterance, history).discussion
    assert router.resolve(utterance).ready


def test_repeating_a_real_action_retains_execution_scope():
    history = [{"role": "user", "content": "메모장 실행해줘"}]
    assert not analyze_utterance_scope("다시 해줘", history).discussion


def test_conversational_response_does_not_mistake_repeated_explanation_for_false_completion(
    tmp_path, router, monkeypatch,
):
    executor = _executor(tmp_path, router)
    executor._selected_voice_preferences = lambda: ("", "보스", "")
    executor.conversation_service = SimpleNamespace(respond=lambda *_args, **_kwargs: "다시 설명합니다.")
    monkeypatch.setattr("core.executor.load_custom_voice_profiles", lambda: [])
    monkeypatch.setattr("core.executor.build_relevant_knowledge_context", lambda *_args, **_kwargs: "")
    response = Executor._respond_conversationally(executor, "다시 해줘", [
        {"role": "user", "content": "카메라를 끄는 방법만 설명해줘"},
        {"role": "assistant", "content": "카메라 설정을 설명합니다."},
    ])
    assert response == "다시 설명합니다."


@pytest.mark.parametrize("confidence", [None, [], {}, "높음", float("nan"), float("inf")])
def test_context_model_malformed_confidence_does_not_crash_or_invent_certainty(confidence):
    llm = SimpleNamespace(chat=lambda _messages: json.dumps({
        "resolved_request": "현재 문구를 아래로 이동해줘", "confidence": confidence,
        "needs_clarification": False, "context_used": True, "relation": "follow_up",
    }, ensure_ascii=False))
    result = ConversationContextResolver(llm).resolve(
        "그걸 아래로 내려줘", [{"role": "assistant", "content": "시안 문구가 있습니다."}], "s",
    )
    assert result.confidence == 0.0
    assert result.needs_clarification


def test_context_string_false_does_not_enable_inheritance():
    llm = SimpleNamespace(chat=lambda _messages: json.dumps({
        "resolved_request": "삼성전자 분석을 메일로 보내줘", "confidence": 0.9,
        "needs_clarification": "false", "context_used": "false", "relation": "follow_up",
    }, ensure_ascii=False))
    request = "그런데 그거 무슨 뜻이야?"
    result = ConversationContextResolver(llm).resolve(
        request, [{"role": "user", "content": "삼성전자 분석해줘"}], "s",
    )
    assert not result.context_used
    assert not result.needs_clarification
    assert result.resolved_request == request


@pytest.mark.parametrize("rewritten", [None, [], {"text": "실행해줘"}, 17, ""])
def test_malformed_context_rewrite_requests_target_clarification(rewritten):
    llm = SimpleNamespace(chat=lambda _messages: json.dumps({
        "resolved_request": rewritten, "confidence": 0.99,
        "needs_clarification": False, "context_used": True, "relation": "follow_up",
    }, ensure_ascii=False))
    result = ConversationContextResolver(llm).resolve(
        "그 파일을 지워줘", [{"role": "assistant", "content": "문서가 여러 개 있습니다."}], "s",
    )
    assert result.needs_clarification
    assert result.clarification_question
    assert result.resolved_request == "그 파일을 지워줘"
    assert result.confidence == 0.0
