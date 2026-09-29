"""Supplied conditions and failed model contracts are not pending user actions."""
from types import SimpleNamespace

import pytest

from core.agent_services import ConversationService
from core.answer_verification import AnswerReview
from test_semantic_executor_flow import make_executor, _pending_send, _decision, SESSION
from test_executor_semantic_conversation import prepare_conversation


def answer(**overrides):
    return dict(relation="conversation", operation="conversation", grounded=True,
                confidence=.99, source="response_mode", **overrides)


@pytest.mark.parametrize("utterance", [
    "두 값을 비교하면 큰 값을 반환합니다. solution 함수를 완성해 주세요.",
    "문제 설명\n잔액이 없으면 -1을 반환합니다.\n빈 입력이면 0입니다.\n함수를 완성해 주세요.",
    "비가 오면 우산을 가져간다는 문장을 영어로 바꿔줄 수 있어?",
    "코드를 실행하지 마. 여기서 답만 보여줄 수 있어?",
    "If a list is empty return zero. Write the function in this chat.",
])
def test_supplied_conditions_do_not_block_validated_answer(utterance, make_executor):
    executor = make_executor(answer())
    result = executor.execute_turn(utterance, "answer-boundary")
    assert result.status == "completed" and result.grounded_conversation
    assert executor.conversation_calls[0][0] == utterance
    assert not executor.plan_calls and not executor.test_surface.calls
    assert executor.dialogue_state.list("answer-boundary", executor._workspace_scope()) == []


def test_problem_followup_capability_and_feedback_do_not_accumulate_pending(make_executor):
    requests = ["문제 설명\n두 수를 비교하면 큰 수를 반환합니다. 함수를 완성해 주세요.",
                "이 문제를 풀어봐", "코딩테스트 문제 풀어줄 수 있어?", "말을 전혀 이해 못하는구나?"]
    executor = make_executor(*(answer() for _ in requests))
    history = []
    for request in requests:
        result = executor.execute_turn(request, "replay", history)
        assert result.status == "completed"
        history.extend([{"role": "user", "content": request},
                        {"role": "assistant", "content": result.response}])
    assert executor.conversation_calls[-1][1] == history[:-2]
    assert executor.dialogue_state.list("replay", executor._workspace_scope()) == []
    assert not executor.plan_calls and not executor.test_surface.calls


@pytest.mark.parametrize("utterance", [
    "내가 승인하면 note.txt 파일을 수정해줘",
    "note.txt 파일을 수정하지 마",
])
def test_action_still_cannot_cross_conditional_or_negative_authority(utterance, make_executor):
    executor = make_executor(_decision("test.write", slots={"filename": "note.txt", "instruction": "교체"}))
    result = executor.execute_turn(utterance, "authority")
    assert result.status in {"awaiting_user", "cancelled"}
    assert not executor.plan_calls and not executor.test_surface.calls


@pytest.mark.parametrize("reason, expected_message", [
    *[(reason, reason) for reason in (
        "semantic_response_mode_invalid", "semantic_discovery_invalid", "invalid_slots",
        "semantic_schema_or_confidence_invalid", "conversation_cannot_execute",
        "unknown_or_out_of_scope_tool", "ungrounded_literal:recipient",
    )],
    ("semantic_interpretation_failed:TransportError:connection", "연결하지 못해"),
    ("semantic_interpretation_failed:TransportError:timeout", "응답 시간이 초과"),
    ("semantic_interpretation_failed:TransportError:context_saturated", "길이 한도"),
    ("semantic_interpretation_failed:TransportError:truncated_output", "길이 한도"),
    ("semantic_model_unavailable", "연결하지 못해"),
    ("semantic_interpretation_failed:RuntimeError", "유효한 실행 명세"),
])
@pytest.mark.parametrize("utterance", [
    "대화가 자꾸 엉뚱하게 이어지네",
    "두 값을 비교하면 큰 값을 반환합니다. solution 함수를 완성해 주세요.",
    "내가 승인하면 note.txt 파일을 수정해줘",
    "note.txt 파일을 수정하지 마",
])
def test_model_failure_never_adds_a_pending_user_question(reason, expected_message, utterance, make_executor):
    executor = make_executor({"reason": reason})
    pending = _pending_send(executor)
    before = executor.dialogue_state.list(SESSION, executor._workspace_scope())
    outcome = executor.execute_turn(utterance, SESSION)
    assert outcome.status == "failed"
    assert expected_message in outcome.response
    assert executor.dialogue_state.list(SESSION, executor._workspace_scope()) == before
    assert executor.dialogue_state.get_task(SESSION, pending.task_id).status == "awaiting_user"
    assert not executor.plan_calls and not executor.test_surface.calls


@pytest.mark.parametrize("reason", [
    "semantic_response_mode_invalid", "semantic_interpretation_failed:TransportError:timeout",
])
def test_conditional_model_failure_closes_only_its_queued_task(reason, make_executor):
    executor = make_executor({"reason": reason})
    goal = "두 값을 비교하면 큰 값을 반환합니다. solution 함수를 완성해 주세요."
    task = executor.dialogue_state.create_task(SESSION, goal, workspace_path=executor._workspace_scope())
    outcome = executor.execute_turn(goal, SESSION, existing_task_id=task.task_id)
    stored = executor.dialogue_state.get_task(SESSION, task.task_id, executor._workspace_scope())
    assert outcome.status == stored.status == "failed"
    assert outcome.task_id == task.task_id and stored.result == outcome.response
    assert executor.dialogue_state.list(SESSION, executor._workspace_scope()) == []
    assert len(executor.dialogue_state.list_tasks(SESSION)) == 1
    assert not executor.plan_calls and not executor.test_surface.calls


@pytest.mark.parametrize("kind", ["code", "reasoning"])
def test_semantic_answer_kind_reaches_specialist_and_review_without_tools(kind):
    captured, drafts = [], []
    request = "두 수 중 큰 값을 반환하는 solution 함수를 완성해 주세요."
    def factory(contract):
        captured.append(contract)
        return SimpleNamespace(chat=lambda messages: drafts.append(messages) or "경계 조건까지 고려한 풀이입니다.")
    verifier = SimpleNamespace(verify=lambda contract, draft, **kw: (draft, AnswerReview(status="unverified")))
    base = SimpleNamespace(chat=lambda _: pytest.fail("substantive answer used chat-only role"))
    result = ConversationService(base, answer_verifier=verifier, generation_client_factory=factory).respond(
        request, [], answer_kind=kind,
    )
    assert captured[0].requires_review
    # An explicit request to complete a function is a code deliverable even
    # when the classifier labels it reasoning.
    assert captured[0].requires_code
    assert drafts[0][-1]["content"] == request
    assert result.answer_review.status == "unverified"


def test_capability_question_keeps_light_conversation_role():
    model = SimpleNamespace(chat=lambda _: "응, 문제를 보내주면 풀이와 코드를 설명해 줄 수 있어.")
    verifier = SimpleNamespace(verify=lambda contract, draft, **kw: (draft, AnswerReview()))
    service = ConversationService(model, answer_verifier=verifier,
                                  generation_client_factory=lambda _: pytest.fail("capability is not a coding task"))
    assert "문제를 보내주면" in service.respond("코딩테스트 문제 풀어줄 수 있어?", [], answer_kind="conversation")


def test_executor_propagates_only_validated_answer_kind(make_executor, monkeypatch):
    executor = make_executor(answer(answer_kind="code"))
    prepare_conversation(executor, monkeypatch)
    received = []
    executor.conversation_service.respond = lambda *a, **kw: received.append(kw) or "풀이입니다."
    result = executor.execute_turn("solution 함수를 완성해 주세요.", "kind")
    assert result.grounded_conversation and received[0]["answer_kind"] == "code"
