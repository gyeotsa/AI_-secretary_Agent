"""A failed implementation must not be delivered again as a claimed correction."""
import json

import pytest

from core.agent_services import ConversationService
from core.answer_verification import AnswerContract, AnswerVerificationService, _blocks
from core.plugin import ToolCancelledError
from test_code_execution import runtime
from test_coding_conversation_flow import ApprovingReviewer, Model


PROBLEM = "Python solution 함수를 작성해줘. 정수 x의 두 배를 반환해야 해.\n3 -> 6\n5 -> 10"
BAD = "```python\ndef solution(x): return 0\n```"
REPEATED = "수정했습니다. 이제 올바른 코드입니다.\n```python\n# 수정된 풀이\ndef solution(x): return 0\n```"
GOOD = "```python\ndef solution(x): return x * 2\n```"
FAILURE = {"status": "failed", "results": [
    {"executed": True, "passed": False, "input": [3], "expected": 6, "actual": 0, "error": ""},
    {"executed": True, "passed": False, "input": [5], "expected": 10, "actual": 0, "error": ""},
]}


def assert_unchanged_not_delivered(text, review):
    assert review.status != "passed"
    assert review.revision_status == "unchanged"
    assert "코드가 변경되지" in text or "수정 미완료" in text
    assert not _blocks(text)
    assert "수정했습니다" not in text and "올바른 코드입니다" not in text


@pytest.mark.parametrize("execution", ["no_examples", "disabled", "unavailable", "timeout"])
def test_unchanged_failed_code_suppressed_even_without_execution(execution):
    def runner(*args, **kwargs):
        if execution == "timeout":
            raise TimeoutError("runner timed out")
        return {"status": "unavailable", "reason": "python_wasm_unavailable", "results": []}

    problem = "Python solution 함수를 작성해줘." if execution == "no_examples" else PROBLEM
    verifier = AnswerVerificationService(
        reviewer_factory=lambda: ApprovingReviewer(), repairer_factory=lambda: Model(REPEATED),
        execution_runner=None if execution == "disabled" else runner)
    text, review = verifier.verify(AnswerContract(
        problem, requires_code=True, requires_review=True, execution_problem=problem,
        previous_code=_blocks(BAD)[0].body, failure_feedback="오답이야", correction_authorized=True,
    ), REPEATED)
    assert_unchanged_not_delivered(text, review)


def test_same_failed_implementation_is_executed_once_then_suppressed():
    executions = []

    def runner(code, specification, **kwargs):
        executions.append(code)
        return FAILURE

    repair = Model(REPEATED)
    text, review = AnswerVerificationService(
        repairer_factory=lambda: repair, execution_runner=runner).verify(
            AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM,
                           previous_code=_blocks(BAD)[0].body, failure_feedback="다시 풀어줘"), BAD)
    assert len(executions) == 1
    assert len(repair.requests) == 1
    assert_unchanged_not_delivered(text, review)
    assert review.execution_history[0]["results"][0]["actual"] == 0
    payload = json.loads(repair.requests[0][-1]["content"])
    assert '"actual": 0' in "\n".join(payload["issues"])


@pytest.mark.parametrize("fixed", [False, True])
def test_real_wasi_conversation_followup_preserves_problem_and_failed_code(runtime, fixed):
    # Three distinct failing candidates leave a visible failed answer to which
    # the next short user turn refers, just like the reported conversation.
    first_repair = Model(BAD.replace("return 0", "return 1"), BAD.replace("return 0", "return 2"))
    first = ConversationService(Model(BAD), answer_verifier=AnswerVerificationService(
        repairer_factory=lambda: first_repair)).respond(PROBLEM, [])
    assert first.answer_review.execution_status == "failed"
    assert "return 2" in first

    unchanged = "수정했습니다.\n" + BAD.replace("return 0", "return 2")
    repair = Model(GOOD if fixed else unchanged)
    draft = Model(unchanged)
    second = ConversationService(draft, answer_verifier=AnswerVerificationService(
        repairer_factory=lambda: repair)).respond("틀렸어. 다시 풀어줘", [
            {"role": "user", "content": PROBLEM}, {"role": "assistant", "content": str(first)},
        ])
    assert "3 -> 6" in draft.requests[0][-1]["content"]
    payload = json.loads(repair.requests[0][-1]["content"])
    assert "return 2" in payload["previous_code"]
    assert "3 -> 6" in payload["request"]
    assert '"actual": 2' in "\n".join(payload["issues"])
    if fixed:
        assert second.answer_review.status == "passed"
        assert second.answer_review.execution_status == "passed"
        assert "2/2" in second and "return x * 2" in second
    else:
        assert_unchanged_not_delivered(second, second.answer_review)
        assert len(second.answer_review.execution_history) == 1
        final_draft = Model(GOOD)
        third = ConversationService(final_draft).respond("다시 풀어줘", [
            {"role": "user", "content": PROBLEM}, {"role": "assistant", "content": str(first)},
            {"role": "user", "content": "틀렸어. 다시 풀어줘"},
            {"role": "assistant", "content": str(second)},
        ])
        assert "return 2" in final_draft.requests[0][-1]["content"]
        assert third.answer_review.execution_status == "passed" and "2/2" in third


def test_changed_but_unchecked_repair_is_not_mislabelled_unchanged():
    executions = []

    def runner(code, specification, **kwargs):
        executions.append(code)
        if len(executions) == 1:
            return FAILURE
        raise TimeoutError("changed candidate was not verified")

    text, review = AnswerVerificationService(
        repairer_factory=lambda: Model(GOOD), execution_runner=runner).verify(
            AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM,
                           previous_code=_blocks(BAD)[0].body, failure_feedback="오답이야"), BAD)
    assert "return x * 2" in text
    assert review.status != "passed" and review.revision_status != "unchanged"
    assert not review.code_executed and not review.test_results


def test_explaining_existing_code_does_not_require_an_implementation_change():
    text, review = AnswerVerificationService(
        reviewer_factory=lambda: ApprovingReviewer(), execution_runner=None).verify(
            AnswerContract("이 코드가 어떻게 작동하는지 설명해줘", requires_review=True,
                           previous_code=_blocks(BAD)[0].body), "항상 0을 반환합니다.\n" + BAD)
    assert "return 0" in text and review.revision_status != "unchanged"


def test_changed_but_failing_code_cannot_keep_success_narrative():
    repair = Model("해결했습니다.\n" + BAD.replace("return 0", "return 1"),
                   "해결했습니다.\n" + BAD.replace("return 0", "return 2"))
    text, review = AnswerVerificationService(repairer_factory=lambda: repair,
        execution_runner=lambda *a, **kw: FAILURE).verify(
            AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM), BAD)
    assert review.status == "failed" and "return 2" in text
    assert "해결했습니다" not in text and "실행 검증 실패" in text


@pytest.mark.parametrize("cancel_at", ["runner", "repair"])
def test_unchanged_revision_handling_does_not_swallow_cancellation(cancel_at):
    def runner(*args, **kwargs):
        if cancel_at == "runner":
            raise ToolCancelledError("stop")
        return FAILURE

    with pytest.raises(ToolCancelledError):
        AnswerVerificationService(execution_runner=runner,
            repairer_factory=lambda: Model(ToolCancelledError("stop"))).verify(
                AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM,
                               previous_code=_blocks(BAD)[0].body, failure_feedback="오답이야"), BAD)
