"""Real guest execution and correction evidence, not simulated test outcomes."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from core.answer_verification import AnswerContract, AnswerVerificationService, _blocks
from core.code_execution import WASM_PATH, check_examples, extract_examples
from core.agent_services import ConversationService
from core.plugin import ToolCancelledError
from scripts.qa_coding_repair import PROBLEM, BAD_ANSWER
from test_coding_conversation_flow import GOOD_ANSWER, Model, coding_runtime
from test_semantic_executor_flow import make_executor


@pytest.fixture
def runtime():
    pytest.importorskip("wasmtime")
    if not WASM_PATH.exists():
        pytest.skip("Run scripts/setup_coding_runtime.py for real WASI execution tests")


def test_literal_examples_and_ambiguous_oracles():
    assert len(extract_examples(PROBLEM)["cases"]) == 4
    table = "solution 함수를 완성해주세요\n입출력 예\nn\tinfo\tresult\n5\t[2,1]\t[0,5]"
    assert extract_examples(table)["cases"][0]["input"] == [5, [2, 1]]
    assert extract_examples("solution(x)\n1 -> 2\n1 -> 3") is None
    assert extract_examples("solution 함수 구현해줘. 기대값은 네가 알아서 정해") is None
    assert extract_examples("solution(x)\n입출력 예\n__import__('os').system('bad') -> 1") is None
    assert extract_examples('solution(x)\n"a->b" -> "c=>d"')["cases"][0]["expected"] == "c=>d"
    assert extract_examples('solution(x)\n"a->b", "c"') is None


def test_real_archery_good_and_bad(runtime):
    cases = extract_examples(PROBLEM)
    good = check_examples(_blocks(GOOD_ANSWER)[0].body, cases)
    bad = check_examples(_blocks(BAD_ANSWER)[0].body, cases)
    assert good["status"] == "passed" and len(good["results"]) == 4
    assert bad["status"] == "failed" and sum(r["passed"] for r in bad["results"]) == 1
    assert bad["results"][0]["actual"] == [-1]


@pytest.mark.parametrize("code", [
    "def solution(x): return open('C:/Windows/win.ini').read()",
    "def solution(x): return open('escape.txt','w').write('no')",
    "import socket\ndef solution(x): return socket.create_connection(('127.0.0.1',11434))",
    "def solution(x):\n while True: pass",
    "def solution(x): return [0]*10**9",
])
def test_guest_cannot_access_host_or_exceed_limits(runtime, code):
    outcome = check_examples(code, extract_examples("solution(x)\n1 -> 2"), seconds=1.0)
    assert outcome["status"] == "failed" and outcome["results"][0]["error"]


def test_guest_environment_is_not_inherited(runtime, monkeypatch):
    monkeypatch.setenv("ANIS_TEST_SECRET", "never-send-to-guest")
    code = "import os\ndef solution(x): return os.getenv('ANIS_TEST_SECRET')"
    assert check_examples(code, extract_examples("solution(x)\n1 -> null"))["status"] == "passed"


def test_actual_failures_drive_repair_and_scoped_storage(runtime, coding_runtime):
    executor, store = coding_runtime
    repaired = Model(GOOD_ANSWER)
    verifier = AnswerVerificationService(repairer_factory=lambda: repaired,
        reviewer_factory=lambda: pytest.fail("WASI verdict must not depend on model approval"))
    executor.conversation_service = ConversationService(Model(BAD_ANSWER), answer_verifier=verifier)
    outcome = executor.execute_turn(PROBLEM, "executed")
    assert outcome.answer_review["execution_status"] == "passed"
    assert outcome.answer_review["code_executed"]
    assert "4/4" in outcome.response and "숨겨진" in outcome.response
    payload = json.loads(repaired.requests[0][-1]["content"])
    assert '"actual": [-1]' in payload["issues"][0]
    latest = store.latest_attempt("executed", executor._workspace_scope())
    assert latest["verified"] and latest["verification_source"] == "executed_tests"
    assert "return [-1]" in latest["failed_code"]
    prior = store.get_attempt(latest["previous_attempt_id"], session_id="executed",
                             workspace_path=executor._workspace_scope())
    assert not prior["verified"] and prior["test_results"][0]["actual"] == [-1]


def test_failed_correction_stays_failed(runtime):
    wrong = "```python\ndef solution(n, info): return [0]*11\n```"
    repair = Model(wrong, wrong)
    text, review = AnswerVerificationService(repairer_factory=lambda: repair).verify(
        AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM), BAD_ANSWER)
    assert review.status == "failed" and review.repair_calls == 2
    assert not all(r["passed"] for r in review.test_results)
    assert "실패 예제" in text and "원문 예제" in text


@pytest.mark.parametrize("utterance", ["이 코드를 실행하지 말고 설명만 해줘",
    "solution 함수를 작성해줘. 실행하지 마.\n1 -> 2"])
def test_no_execution_for_explanation_or_explicit_refusal(utterance):
    reviewer = SimpleNamespace(verify=lambda *a, **k: (a[1], None))
    service = ConversationService(Model("설명만 제공합니다."), answer_verifier=reviewer)
    captured = []
    reviewer.verify = lambda c, d, **kw: (captured.append(c) or d, None)
    service.respond(utterance, [
        {"role": "user", "content": PROBLEM}, {"role": "assistant", "content": GOOD_ANSWER}])
    assert not captured[0].execution_problem


def test_cancellation_interrupts_running_guest(runtime):
    from core.turn_context import TurnExecutionContext, bind_turn_context
    context = TurnExecutionContext("cancel", "cancel")
    timer = threading.Timer(0.2, context.cancel)
    started = time.monotonic()
    timer.start()
    try:
        with bind_turn_context(context), pytest.raises(ToolCancelledError):
            check_examples("def solution(x):\n while True: pass", extract_examples("solution(x)\n1 -> 2"))
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 2


def test_missing_code_can_be_repaired_then_test_failure_corrected(runtime):
    repair = Model(BAD_ANSWER, GOOD_ANSWER)
    text, review = AnswerVerificationService(repairer_factory=lambda: repair).verify(
        AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM), "이제 풀어볼게요.")
    assert review.status == "passed" and review.repair_calls == 2
    assert "4/4" in text


def test_repair_without_code_preserves_failed_execution(runtime, coding_runtime):
    executor, store = coding_runtime
    executor.conversation_service = ConversationService(Model(BAD_ANSWER), answer_verifier=
        AnswerVerificationService(repairer_factory=lambda: Model("풀이 준비 중", "풀이 준비 중")))
    result = executor.execute_turn(PROBLEM, "incomplete")
    latest = store.latest_attempt("incomplete", executor._workspace_scope())
    assert "풀이 미완성" in result.response and not result.answer_review["code_executed"]
    assert not latest["verified"] and latest["test_results"][0]["executed"]


def test_unavailable_runtime_never_becomes_passed():
    text, review = AnswerVerificationService(execution_runner=lambda *a, **kw:
        {"status": "unavailable", "reason": "python_wasm_unavailable", "results": []}).verify(
        AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM), BAD_ANSWER)
    assert review.status == "unverified" and not review.code_executed
    assert "미수행" in text and review.repair_calls == 0


def test_cancellation_propagates_from_runner():
    def cancel(*a, **k):
        raise ToolCancelledError("stop")
    with pytest.raises(ToolCancelledError):
        AnswerVerificationService(execution_runner=cancel).verify(
            AnswerContract(PROBLEM, requires_code=True, execution_problem=PROBLEM), BAD_ANSWER)
