"""Answer acceptance metadata stays separate from execution evidence."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from core.agent_services import (
    ConversationResponse, ConversationService, guard_conversation_response,
    has_unsupported_activity_claim,
)
from core.evaluation_runtime import ApplicationEvaluator
from test_semantic_executor_flow import make_executor
from test_executor_semantic_conversation import prepare_conversation


@dataclass(frozen=True)
class Review:
    status: str

    def to_dict(self):
        return {"status": self.status, "code_executed": False,
                "requested_count": 3, "observed_count": 3 if self.status == "passed" else 2}


@pytest.mark.parametrize("text", [
    "현재 이유를 확인 중이라 정확히 말씀드리기 어렵습니다.",
    "원인을 조사 중입니다.", "잠시만요. 지금 테스트하고 있어요.",
    "검색을 진행할게요. 현재 검색 중입니다.", "파일을 다운로드하고 있어요.",
    "I'm currently checking the server.", "I am running the tests.",
])
def test_unsupported_ongoing_claim_is_not_a_completed_conversation(text):
    assert has_unsupported_activity_claim(text)
    response = ConversationResponse(text, answer_review=Review("passed"))
    guarded = guard_conversation_response(response)
    assert guarded.unsupported_activity
    assert guarded.answer_review is response.answer_review
    assert not guarded.unverified_completion
    assert "아직 실제 조사나 도구 실행을 시작하지 않았습니다" in guarded
    assert guard_conversation_response(guarded) is guarded


@pytest.mark.parametrize("text", [
    '"원인을 확인 중입니다"라는 말은 근거가 없어.',
    '```text\nI am running tests.\n```',
    "원인을 확인 중이라고 말하면 안 돼.", "원인을 확인 중이라면 기다려 보자.",
    "지금 확인 중이 아니야.", "지금 확인하고 있지 않아.",
    "지금 검색 중인가요?", "서버가 설치 중입니다.",
    "사용자가 원인을 확인 중입니다.", "제공된 코드를 보면 종료 조건이 잘못됐어.",
    "실행하지 않고 검토하면 빈 입력에서 0을 반환해야 해.",
])
def test_quoted_negative_hypothetical_third_party_and_static_reasoning_remain(text):
    assert not has_unsupported_activity_claim(text)
    assert guard_conversation_response(text) == text


@pytest.mark.parametrize("status,terminal", [
    ("passed", "completed"), ("not_required", "completed"),
    ("incomplete", "partial"), ("failed", "unverified"), ("unverified", "unverified"),
])
@pytest.mark.parametrize("queued", [False, True])
def test_answer_review_controls_terminal_persistence_and_metrics(
        status, terminal, queued, make_executor, monkeypatch):
    executor = make_executor(dict(relation="conversation", operation="conversation", grounded=True,
                                  confidence=1.0, source="model"))
    _, metrics = prepare_conversation(executor, monkeypatch)
    review = Review(status)
    executor.conversation_service.respond = lambda *a, **k: ConversationResponse(
        "설명할 내용입니다.", answer_review=review,
    )
    goal, session = "실행하지 말고 예시 3개를 설명해줘.", "answer-review"
    task = executor.dialogue_state.create_task(session, goal, workspace_path=executor._workspace_scope()) if queued else None
    outcome = executor.execute_turn(goal, session, existing_task_id=task.task_id if task else None)
    assert outcome.status == terminal
    assert outcome.answer_review == review.to_dict()
    assert not executor.test_surface.calls and not executor.plan_calls
    assert any(name == "runtime_completion" and value == float(terminal == "completed")
               for name, value, _ in metrics)
    if status != "not_required":
        assert any(name == "answer_review" and value == float(status == "passed")
                   for name, value, _ in metrics)
    if task:
        assert executor.dialogue_state.get_task(session, task.task_id, executor._workspace_scope()).status == terminal


def test_activity_claim_is_measured_separately(make_executor, monkeypatch):
    executor = make_executor(dict(relation="conversation", operation="conversation", grounded=True,
                                  confidence=1.0, source="model"))
    _, metrics = prepare_conversation(executor, monkeypatch)
    executor.conversation_service.respond = lambda *a, **k: "원인을 확인 중입니다."
    outcome = executor.execute_turn("왜 느린지 아직 모르면 모른다고 말해줘.", "activity")
    assert outcome.status == "failed" and outcome.unsupported_activity_claim
    assert any(name == "unsupported_activity_claim" and value == 1 for name, value, _ in metrics)
    assert not any(name == "false_completion" for name, _, _ in metrics)


def test_answer_evaluator_does_not_demand_tool_receipts_for_explanations():
    outcome = {"status": "completed", "answer_review": Review("passed").to_dict()}
    expected = {"answer_contract": {"requested_count": 3}}
    assert ApplicationEvaluator.check(outcome, expected)[0]
    outcome["answer_review"] = Review("unverified").to_dict()
    assert not ApplicationEvaluator.check(outcome, expected)[0]
    assert not ApplicationEvaluator.check({"status": "completed"}, expected)[0]


@pytest.mark.parametrize("question,draft", [
    ("안녕?", "안녕!"), ("안녕하세요", "안녕하세요."),
    ("좋은 아침!", "좋은 아침."), ("고마워", "고마워!"),
    ("네", "네."), ("Hello?", "hello!"),
])
def test_simple_greeting_does_not_instantiate_long_answer_role(question, draft):
    calls = []
    llm = SimpleNamespace(chat=lambda messages: calls.append(messages) or draft)
    service = ConversationService(llm, generation_client_factory=lambda _: pytest.fail("long role for greeting"))
    assert service.respond(question, []) == draft
    assert len(calls) == 1


@pytest.mark.parametrize("question", ["웃어 봐", "왜?", "안녕, 오늘은 뭐 할까?", "잘 자는 법 알려줘"])
def test_echoed_question_or_command_still_requires_an_answer(question):
    answers = iter([question, "질문에 대한 답은 이거야."])
    calls = []
    llm = SimpleNamespace(chat=lambda messages: calls.append(messages) or next(answers))
    result = ConversationService(llm).respond(question, [])
    assert result == "질문에 대한 답은 이거야."
    assert len(calls) == 2


def test_unsupported_progress_gets_one_correction_before_final_guard():
    answers = iter(["이유를 확인 중이라 정확히 말하기 어려워.", "아직 원인을 확인하지 않았어. 지연 원인은 알 수 없어."])
    calls = []
    llm = SimpleNamespace(chat=lambda messages: calls.append(messages) or next(answers))
    response = ConversationService(llm).respond("느린 원인을 아직 확인하지 않았다면 모른다고 말해줘.", [])
    assert len(calls) == 2
    assert not response.unsupported_activity
    assert response == "아직 원인을 확인하지 않았어. 지연 원인은 알 수 없어."


def test_substantive_review_receives_full_heading_and_explanation():
    draft = "1. **字符串 반전**\n설명을 잘라내면 안 돼.\n```python\nx = 1\n```"
    captured = []
    def verify(contract, response, **kwargs):
        captured.append((response, kwargs))
        return response, Review("unverified")
    llm = SimpleNamespace(chat=lambda _: draft)
    result = ConversationService(llm, answer_verifier=SimpleNamespace(verify=verify)).respond(
        "예시 1개를 코드와 설명해줘", [],
    )
    assert result == draft
    assert captured[0][0] == draft and captured[0][1]["repair_hint"]
