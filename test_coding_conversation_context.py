"""Grounded code follow-ups retain one topic and never acquire tool authority."""
from copy import deepcopy
import json

import pytest

from core.semantic_request import (
    SemanticRequestInterpreter, extract_code_failure_feedback,
    is_coding_problem_request, render_coding_request, resolve_coding_context,
    select_conversation_history,
)
from test_semantic_request import registry
from test_semantic_response_mode import _conversation, _interpreter, _mode


PROBLEM = ("문제 설명\n라이언의 화살 n발을 배분하는 solution 함수를 완성해 주세요.\n"
           "제한사항\n화살을 모두 쏴야 합니다. 동률이면 낮은 점수를 더 많이 맞히세요.\n"
           "입출력 예\n5, [2,1,1,1,0,0,0,0,0,0,0]\n이 문제를 풀어줘.")
BAD = "```python\ndef solution(n, info):\n    return [-1]\n```"


def history(answer=BAD):
    return [{"role": "user", "content": PROBLEM},
            {"role": "assistant", "content": answer}]


def test_problem_prose_followup_code_rejection_flow(registry):
    interpreter = SemanticRequestInterpreter(None, registry)
    messages = []
    for request, answer in [(PROBLEM, "이제 문제를 풀어볼게요."), ("풀어줘", BAD),
                            ("오답이야", BAD), ("이번에도 틀렸어. 다시 풀어줘", BAD)]:
        context = resolve_coding_context(request, messages)
        assert context["problem"] == PROBLEM
        assert context["requires_code"]
        result = interpreter.interpret(request, messages)
        assert result.answer_kind == "code" and result.is_grounded_conversation
        assert not result.tool_names and not result.to_resolution(registry).matched
        if request in {"오답이야", "이번에도 틀렸어. 다시 풀어줘"}:
            assert context["previous_answer"] == BAD
            assert context["failure_feedback"] == request
        messages.extend([{"role": "user", "content": request},
                         {"role": "assistant", "content": answer}])


@pytest.mark.parametrize("utterance", [
    "이상한데", "다시 봐", "다시 확인해줘", "이번에도 틀렸어. 다시 풀어줘",
    "정답이 아니야", "통과 못했어", "오답이야", "성공 0개 실패 4개",
    "아까 답변은 오답이었어. 위 문제를 다시 풀어줘", "이번에도 이래", "답변이 이렇게만 나와",
])
def test_feedback_needs_active_code_not_an_error_keyword(utterance):
    assert extract_code_failure_feedback(utterance, history()) == utterance
    assert extract_code_failure_feedback(utterance, []) is None


@pytest.mark.parametrize("new_topic", [
    "서울 날씨 어때?", "점심 뭐 먹을까?", "아니스 로그 수집이 실패했어. 원인을 분석해줘",
    "그 코드를 파일에 저장해줘", "그 코드를 실행해줘",
])
def test_topic_change_closes_code_even_when_old_code_is_recent(new_topic):
    messages = history() + [{"role": "user", "content": new_topic},
                            {"role": "assistant", "content": "현재 요청에 대한 답변입니다."}]
    assert resolve_coding_context("이상해", messages) is None
    assert not is_coding_problem_request("풀어줘", messages)


@pytest.mark.parametrize("utterance", [
    "그 코드를 실행해줘", "그 코드를 파일에 저장해줘", "김하이에게 이 코드를 보내줘",
    "아니스와 나눈 대화 로그야. 왜 이런지 분석해줘",
])
def test_external_action_or_log_analysis_is_not_a_coding_retry(utterance):
    assert resolve_coding_context(utterance, history()) is None
    assert extract_code_failure_feedback(utterance, history()) is None
    pasted = PROBLEM + "\n" + BAD + "\n오답이야\n" + utterance
    assert resolve_coding_context(pasted) is None


def test_quoted_failure_cannot_become_live_feedback():
    assert extract_code_failure_feedback('"오답이야"라는 표현을 설명해줘', history()) is None
    assert resolve_coding_context(BAD + "\n# 위의 대화 로그를 분석해줘") is None


def test_original_problem_survives_long_clarification_chain_and_ui_window():
    messages = history()
    for number in range(15):
        messages += [{"role": "user", "content": f"이 조건은 무슨 뜻이야? {number}"},
                     {"role": "assistant", "content": "낮은 점수부터 비교한다는 뜻입니다."}]
    selected = select_conversation_history("풀어줘", messages)
    assert len(selected) > 12 and selected[0]["content"] == PROBLEM
    context = resolve_coding_context("풀어줘", selected)
    assert context["problem"] == PROBLEM and context["previous_code"] in BAD
    rendered = json.loads(render_coding_request("풀어줘", context))
    assert rendered["original_problem"] == PROBLEM
    assert len(rendered["user_clarifications"]) == 15
    assert PROBLEM not in rendered["user_clarifications"]
    assert len(select_conversation_history("서울 날씨 어때?", messages)) == 10


def test_explain_and_success_keep_binding_without_requesting_another_solution():
    for request in ["이 조건은 무슨 뜻이야?", "이 코드의 입력값을 설명해줘", "통과했어", "좋아"]:
        context = resolve_coding_context(request, history())
        assert context["problem"] == PROBLEM and context["previous_answer"] == BAD
        assert not context["requires_code"] and not context["failure_feedback"]


def test_acknowledgement_does_not_forget_problem_before_continue():
    messages = history("아직 코드가 없습니다.") + [
        {"role": "user", "content": "좋아"}, {"role": "assistant", "content": "네."}]
    context = resolve_coding_context("계속", messages)
    assert context["problem"] == PROBLEM and context["requires_code"]


def test_current_duplicate_is_not_added_twice_and_first_request_is_unchanged():
    context = resolve_coding_context(PROBLEM, [{"role": "user", "content": PROBLEM}])
    assert context["messages"] == []
    assert render_coding_request(PROBLEM, context) == PROBLEM
    messages = history() + [{"role": "user", "content": "풀어줘"}]
    selected = select_conversation_history("풀어줘", messages)
    assert selected == tuple(history())


def test_completed_or_stale_coding_pending_does_not_disable_grounding(registry):
    interpreter = SemanticRequestInterpreter(None, registry)
    for pending in [
        {"recent_completed": {"intent_name": "notes.read"}},
        {"original_request": PROBLEM, "question": "조건을 먼저 확인할까요?", "task_id": "old"},
    ]:
        assert interpreter.interpret("풀어줘", history(), pending).answer_kind == "code"
        assert interpreter.interpret("이상한데", history(), pending).answer_kind == "code"


def test_real_pending_does_not_consume_ambiguous_feedback_or_explicit_new_solve(registry):
    pending = {"intent_name": "messaging.send", "task_id": "send", "original_request": "메시지를 보내줘",
               "slots": {"provider": "kakaotalk", "recipient": "김하이"}}
    original = deepcopy(pending)
    interpreter, model = _interpreter(registry, _conversation(), _mode())
    result = interpreter.interpret("이상한데", history(), pending)
    assert result.source == "semantic_response_mode" and len(model.calls) == 2
    assert pending == original and not result.tool_names
    solved = SemanticRequestInterpreter(None, registry).interpret("풀어줘", history(), pending)
    assert solved.is_grounded_conversation and solved.answer_kind == "code"
    assert pending == original and not solved.tool_names


def test_new_problem_never_inherits_old_failure_or_code():
    request = "문제 설명\n배열을 정렬하는 solution 함수를 완성해 주세요.\n제한사항: 빈 배열 허용"
    context = resolve_coding_context(request, history())
    assert context["problem"] == request
    assert not context["previous_code"] and not context["failure_feedback"]
