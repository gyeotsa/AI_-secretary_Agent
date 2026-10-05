"""Real routing/generation/review/storage boundaries with isolated model doubles."""
from types import SimpleNamespace
import json

import pytest

from core.agent_services import ConversationService
from core.answer_verification import AnswerVerificationService
from core.coding_experience import CodingExperienceStore
from core.plugin import ToolCancelledError
from core.semantic_request import SemanticRequestInterpreter, select_conversation_history
from core.turn_context import TurnExecutionContext
from scripts.qa_coding_repair import PROBLEM, BAD_ANSWER
from test_semantic_executor_flow import make_executor
from test_executor_semantic_conversation import prepare_conversation


GOOD_ANSWER = """남는 화살은 0점에 배정하고, 동률이면 낮은 점수부터 비교합니다.
```python
def solution(n, info):
    best_diff, best = 0, [-1]
    for mask in range(1 << 10):
        arrows = [info[i] + 1 if mask & (1 << i) else 0 for i in range(10)]
        if sum(arrows) > n:
            continue
        arrows.append(n - sum(arrows))
        diff = sum((10-i) if arrows[i] > info[i] else -(10-i) if info[i] else 0
                   for i in range(10))
        if diff > best_diff or (diff == best_diff and diff > 0 and arrows[::-1] > best[::-1]):
            best_diff, best = diff, arrows
    return best
```
"""


class Model:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.requests = []

    def chat(self, messages):
        self.requests.append(messages)
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return answer


class ApprovingReviewer:
    def __init__(self):
        self.requests = []

    def chat_structured(self, messages, **kwargs):
        request = json.loads(messages[-1]["content"])
        self.requests.append(request)
        return {"criteria": {key: {"status": "passed", "reason": "정적 검토 의견",
            "quote": (request["code_quotes"][0] if key == "code_semantics" and request.get("code_quotes")
                      else request["evidence_quotes"][0])} for key in request["criteria"]}}


@pytest.fixture
def coding_runtime(make_executor, monkeypatch, tmp_path):
    executor = make_executor()
    prepare_conversation(executor, monkeypatch)
    store = CodingExperienceStore(str(tmp_path / "coding.db"))
    monkeypatch.setattr("core.executor.get_coding_experience_store", lambda: store)
    def classify(messages, schema, **kwargs):
        assert messages[-1]["content"] == "통과했어"
        if "relation" in schema["properties"]:
            return json.dumps({"relation": "conversation", "operation": "conversation",
                "tool_names": [], "slots": {}, "confidence": 1.0,
                "needs_clarification": False, "clarification_question": "", "control_scope": "current"})
        return json.dumps({"mode": "answer", "answer_kind": "conversation", "confidence": 1.0})
    model = SimpleNamespace(chat_structured=classify)
    executor.semantic_interpreter = SemanticRequestInterpreter(
        model, executor.intent_router.registry, classify_response_mode=True)
    return executor, store


def test_problem_prose_followup_failed_code_repair_and_exact_user_confirmation(coding_runtime):
    executor, store = coding_runtime
    lookups = []
    executor.context_manager = SimpleNamespace(
        get_rag_context=lambda query, *, namespace: lookups.append((query, namespace)) or "",
        mark_response_usage=lambda response: None,
    )
    draft = Model("조건을 확인했습니다. 이제 풀어볼게요.", BAD_ANSWER, BAD_ANSWER, "알겠어.")
    repair = Model("이제 풀어볼게요.", GOOD_ANSWER)
    critic = ApprovingReviewer()
    executor.conversation_service = ConversationService(draft,
        answer_verifier=AnswerVerificationService(lambda: critic, lambda: repair, execution_runner=None))
    history = []
    outcomes = []
    for request in (PROBLEM, "풀어줘", "오답이야", "통과했어"):
        context = TurnExecutionContext(str(len(history)), "actual-session", executor._workspace_scope(), "test-namespace")
        outcome = executor.execute_turn(request, "stale-session", history, turn_context=context)
        outcomes.append(outcome)
        history.extend([{"role": "user", "content": request}, {"role": "assistant", "content": outcome.response}])
    assert outcomes[0].status == "partial" and "풀이 미완성" in outcomes[0].response
    assert "return [-1]" in outcomes[1].response
    assert "for mask" in outcomes[2].response
    assert outcomes[2].answer_review["repair_calls"] == 1
    assert not executor.plan_calls and not executor.test_surface.calls
    for messages in draft.requests[1:3] + repair.requests:
        assert "info" in messages[-1]["content"] and "0점" in messages[-1]["content"]
    assert "오답이야" in repair.requests[-1][-1]["content"]
    latest = store.latest_attempt("actual-session", executor._workspace_scope())
    prior = store.get_attempt(latest["previous_attempt_id"], session_id="actual-session", workspace_path=executor._workspace_scope())
    assert latest["problem"] == PROBLEM and latest["failure"] == "오답이야"
    assert "return [-1]" in latest["failed_code"] and prior["user_report"] == "failure"
    assert latest["user_report"] == "success" and not latest["verified"]
    assert latest["metadata"]["memory_namespace"] == "test-namespace"
    assert store.latest_attempt("stale-session", executor._workspace_scope()) is None
    assert lookups[0] == (PROBLEM, "test-namespace")
    assert lookups[2] == (PROBLEM + "\n오답이야", "test-namespace")


def test_cancellation_during_code_repair_reaches_executor(coding_runtime):
    executor, store = coding_runtime
    executor.conversation_service = ConversationService(Model("이제 풀어볼게요."),
        answer_verifier=AnswerVerificationService(lambda: ApprovingReviewer(),
                                                  lambda: Model(ToolCancelledError("cancelled"))))
    result = executor.execute_turn(PROBLEM, "cancel")
    assert result.status == "cancelled"
    assert store.latest_attempt("cancel", executor._workspace_scope()) is None


@pytest.mark.parametrize("failure_feedback", [None, "오답이야"])
def test_bound_explanation_overrules_code_label_without_demanding_code(failure_feedback):
    captured = []
    verifier = SimpleNamespace(verify=lambda c, d, **kw: (captured.append(c) or d, None))
    reply = ConversationService(Model("0점은 남은 화살을 넣는 자리입니다."), answer_verifier=verifier).respond(
        "이 코드의 조건을 코드 없이 설명해줘", [{"role": "user", "content": PROBLEM},
        {"role": "assistant", "content": GOOD_ANSWER}], answer_kind="code", failure_feedback=failure_feedback)
    assert not captured[0].requires_code and "0점" in reply


def test_explaining_error_handling_does_not_revoke_test_evidence(coding_runtime):
    from core.answer_verification import _blocks
    executor, store = coding_runtime
    scope = {"session_id": "explanation", "workspace_path": executor._workspace_scope()}
    attempt = store.record_attempt("explanation", "", GOOD_ANSWER,
        workspace_path=scope["workspace_path"], problem=PROBLEM)
    store.record_test_result(attempt, **scope, tested_code=_blocks(GOOD_ANSWER)[0].body.strip(),
        test_results=[{"executed": True, "passed": True,
                       "input": [1, [1] + [0] * 10], "expected": [-1], "actual": [-1]}])
    before = store.get_attempt(attempt, **scope)
    assert before["verified"]
    result = executor.execute_turn("이 코드의 오류 처리를 설명해줘", "explanation",
        [{"role": "user", "content": PROBLEM}, {"role": "assistant", "content": GOOD_ANSWER}])
    assert result.status == "completed"
    assert store.get_attempt(attempt, **scope) == before


def test_long_active_problem_survives_ui_history_and_repair():
    history = [{"role": "user", "content": PROBLEM}]
    for _ in range(9):
        history.extend([{"role": "assistant", "content": "낮은 점수부터 비교합니다."},
                        {"role": "user", "content": "이 조건은 무슨 뜻이야?"}])
    history.append({"role": "assistant", "content": "이제 문제를 풀어볼게요."})
    selected = select_conversation_history("풀어줘", history)
    assert selected[0]["content"] == PROBLEM
    repair = Model(GOOD_ANSWER)
    critic = ApprovingReviewer()
    result = ConversationService(Model("이제 풀어볼게요."),
        answer_verifier=AnswerVerificationService(lambda: critic, lambda: repair)).respond("풀어줘", selected)
    assert "for mask" in result
    assert PROBLEM in json.loads(repair.requests[0][-1]["content"])["request"].replace("\\n", "\n")


def test_index_retry_uses_exact_attempt_and_same_turn_namespace(coding_runtime):
    from core.turn_context import bind_turn_context
    executor, store = coding_runtime
    workspace = executor._workspace_scope()
    attempt = store.record_attempt("s", "오답", BAD_ANSWER, workspace_path=workspace, problem="test",
                                   metadata={"memory_namespace": "workspace-qa"})
    store.record_test_result(attempt, session_id="s", workspace_path=workspace,
        tested_code="def solution(n, info):\n    return [-1]",
        test_results=[{"executed": True, "passed": True, "input": [1], "expected": [-1], "actual": [-1]}])
    calls = []
    def add(**document):
        calls.append(document)
        if len(calls) == 1:
            raise OSError("temporary index outage")
    executor.context_manager = SimpleNamespace(rag=SimpleNamespace(add_text_document=add))
    with bind_turn_context(TurnExecutionContext("t", "s", workspace, "workspace-qa")):
        executor._publish_coding_experience(store, attempt, "s", workspace)
        executor._publish_coding_experience(store, attempt, "s", workspace)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0]["doc_id"] == "coding-experience-" + attempt
    assert calls[0]["namespace"] == "workspace-qa"


def test_archery_qa_checks_examples_arrow_total_and_low_score_tie_break():
    from scripts.check_archery_report import evaluate
    from core.answer_verification import _blocks
    assert evaluate(_blocks(GOOD_ANSWER)[0].body)["passed"]
    failed = evaluate(_blocks(BAD_ANSWER)[0].body)
    assert not failed["passed"] and failed["example_passes"] == 1


def test_self_declared_correctness_is_not_code_review_evidence():
    class SelfPraiseReviewer(ApprovingReviewer):
        def chat_structured(self, messages, **kwargs):
            result = super().chat_structured(messages, **kwargs)
            result["criteria"]["code_semantics"]["quote"] = "이 코드는 모든 조건을 충족합니다."
            return result
    service = ConversationService(Model(BAD_ANSWER + "\n이 코드는 모든 조건을 충족합니다."),
        answer_verifier=AnswerVerificationService(lambda: SelfPraiseReviewer(),
                                                  lambda: pytest.fail("untrusted review must not repair"),
                                                  execution_runner=None))
    response = service.respond(PROBLEM, [], answer_kind="code")
    assert response.answer_review.status == "unverified"
    assert any("ungrounded_code_review" in issue for issue in response.answer_review.issues)
