"""A validated conversation must not become an execution failure at presentation."""
from dataclasses import replace
from types import MethodType, SimpleNamespace

import pytest

from core.executor import Executor, ExecutionOutcome
from core.semantic_request import SemanticDecision
from test_semantic_executor_flow import make_executor  # isolated Registry/store fixture


def prepare_conversation(executor, monkeypatch):
    calls, metrics = [], []
    executor._respond_conversationally = MethodType(Executor._respond_conversationally, executor)
    executor._selected_voice_preferences = lambda: ("", "", "")
    executor.conversation_service = SimpleNamespace(
        respond=lambda message, history, **kwargs: calls.append(message) or "안녕!"
    )
    executor.quality_metrics = SimpleNamespace(
        record=lambda name, value, **kwargs: metrics.append((name, value, kwargs))
    )
    monkeypatch.setattr("core.executor.load_custom_voice_profiles", lambda: [])
    monkeypatch.setattr("core.executor.build_relevant_knowledge_context", lambda *_a, **_k: "")
    monkeypatch.setattr("core.assistant_settings.get_assistant_settings", lambda: SimpleNamespace(assistant_name="QA"))
    return calls, metrics


@pytest.mark.parametrize("utterance", [
    "그 설명은 취소하고 지금은 짧게 인사만 해줘.",
    "짧게 인사만 해줘.", "오늘 있었던 일에 대해 대화해줘.",
    "재귀 함수의 뜻을 설명해줘.", "재미있는 이야기 해줘.",
    "내 말에 공감해줘.", "긴 설명 대신 한 문장으로 대답해줘.",
    "실행하지 말고 이 코드를 설명해줘.",
    '"파일 삭제해줘"라는 문장의 뜻을 설명해줘.',
])
def test_grounded_conversation_reaches_service_without_false_completion(utterance, make_executor, monkeypatch):
    executor = make_executor(dict(relation="conversation", operation="conversation", grounded=True,
                                  confidence=1.0, source="model"))
    calls, metrics = prepare_conversation(executor, monkeypatch)
    outcome = executor.execute_turn(utterance, "conversation-test")
    assert outcome.status == "completed"
    assert outcome.response == "안녕!"
    assert calls == [utterance]
    assert executor.plan_calls == []
    assert executor.test_surface.calls == []
    assert not any(name == "false_completion" and value == 1 for name, value, _ in metrics)


@pytest.mark.parametrize("change", [
    {"grounded": False}, {"relation": "unknown"}, {"relation": "new"},
    {"operation": "change"}, {"tool_names": ("filesystem_write_file",)},
    {"needs_clarification": True}, {"raw_text": "다른 요청"},
])
def test_only_matching_grounded_no_tool_decision_can_bypass_fallback_guard(change, make_executor, monkeypatch):
    executor = make_executor()
    calls, _ = prepare_conversation(executor, monkeypatch)
    request = "파일을 저장해줘."
    decision = replace(SemanticDecision(request, relation="conversation", operation="conversation",
                                         grounded=True), **change)
    response = executor._respond_conversationally(request, [], semantic_decision=decision)
    assert "실제 작업을 실행하지 않았습니다" in response
    assert calls == []


def test_unverified_action_fallback_still_cannot_narrate_success(make_executor, monkeypatch):
    executor = make_executor()
    calls, metrics = prepare_conversation(executor, monkeypatch)
    request = "파일을 저장해줘."
    assert "실제 작업을 실행하지 않았습니다" in executor._respond_conversationally(request, [])
    assert calls == []
    executor._record_turn_quality(request, ExecutionOutcome("저장했어요", goal=request), 0)
    assert any(name == "false_completion" and value == 1 for name, value, _ in metrics)


@pytest.mark.parametrize(("utterance", "draft"), [
    ("메모장을 켜줘.", "메모장을 켰어."),
    ("파일을 만들어줘.", "파일을 만들었어."),
    ("노래 틀어줘.", "노래를 틀었어."),
    ("안녕?", "이메일을 보냈어."),
])
def test_misclassified_conversation_cannot_claim_or_measure_execution_success(
        utterance, draft, make_executor, monkeypatch):
    executor = make_executor(dict(relation="conversation", operation="conversation", grounded=True,
                                  confidence=.99, source="model"))
    _, metrics = prepare_conversation(executor, monkeypatch)
    executor.conversation_service.respond = lambda *a, **k: draft
    outcome = executor.execute_turn(utterance, "misclassification-test")
    assert outcome.status == "failed"
    assert outcome.unverified_completion_claim
    assert "도구 실행 증거가 없으므로" in outcome.response
    assert executor.test_surface.calls == []
    assert executor.plan_calls == []
    assert any(name == "runtime_completion" and value == 0 for name, value, _ in metrics)
    assert any(name == "false_completion" and value == 1 and data["context"]["blocked"]
               for name, value, data in metrics)


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("false_claim", [False, True])
def test_partial_conversation_is_never_reported_as_complete(
        queued, false_claim, make_executor, monkeypatch):
    from core.llm import ProseResponse, PROSE_TRUNCATION_NOTICE
    executor = make_executor(dict(relation="conversation", operation="conversation", grounded=True,
                                  confidence=.99, source="model"))
    _, metrics = prepare_conversation(executor, monkeypatch)
    response = ProseResponse("이메일을 보냈어." if false_claim else "재귀 함수는 자기 자신을 호출해.",
                            truncated=True, finish_reason="length")
    executor.conversation_service.respond = lambda *a, **k: response
    session, goal = "partial-prose", "재귀 함수에 대해 설명해줘."
    task = (executor.dialogue_state.create_task(session, goal, workspace_path=executor._workspace_scope())
            if queued else None)
    outcome = executor.execute_turn(goal, session, existing_task_id=task.task_id if task else None)
    expected = "failed" if false_claim else "partial"
    assert outcome.status == expected
    assert outcome.response_truncated
    assert outcome.unverified_completion_claim == false_claim
    assert PROSE_TRUNCATION_NOTICE in outcome.response
    assert executor.test_surface.calls == []
    assert any(name == "runtime_completion" and value == 0 for name, value, _ in metrics)
    if task:
        persisted = executor.dialogue_state.get_task(session, task.task_id, executor._workspace_scope())
        assert persisted.status == expected
        assert persisted.result == outcome.response
