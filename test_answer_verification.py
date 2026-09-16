"""Contract review is bounded and never executes generated or user code."""
import json
from dataclasses import FrozenInstanceError

import pytest

from core.answer_verification import (
    AnswerReviewPolicy, AnswerVerificationService, build_answer_contract, requires_answer_review,
)
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context


class Reviewer:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat_structured(self, messages, json_schema):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        value = next(self.responses, "passed")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(payload)
        if isinstance(value, str) and value in {"passed", "failed", "unverified"}:
            return {"criteria": [{"id": key, "status": value,
                "reason": "정적 대조 통과" if value == "passed" else "경계 조건이 잘못되었습니다.",
                "quote": payload["draft"][:min(16, len(payload["draft"]))]}
                for key in payload["criteria"]]}
        return value


class Repairer:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def service(reviewer=None, repairer=None):
    def forbidden():
        raise AssertionError("unexpected model factory")
    return AnswerVerificationService(
        (lambda: reviewer) if reviewer else forbidden,
        (lambda: repairer) if repairer else forbidden,
    )


def examples(count, *, code=True):
    return "\n\n".join(f"{i}. 예시 설명 {i}\n" + (
        f"```python\ndef example_{i}(n):\n    return 0 if n == 0 else n\n```" if code else "작동 원리를 설명합니다.")
        for i in range(1, count + 1))


def test_greeting_has_no_extra_model_call_and_contract_is_immutable():
    contract = build_answer_contract("안녕")
    text, review = service().verify(contract, "안녕!")
    assert text == "안녕!" and review.status == "not_required"
    assert review.critique_calls == review.repair_calls == 0
    with pytest.raises(FrozenInstanceError):
        contract.requires_review = True


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix,indent", [("   - 코드", "     "), ("   1. 코드", "      "), ("", "   ")])
def test_list_nested_fences_preserve_code_and_count(newline, prefix, indent):
    parts = []
    for index in range(1, 4):
        parts.append(newline.join([
            f"{index}. 예제", prefix, indent + "```python", indent + "def value():",
            indent + "    return 0", indent + "```", "",
        ]))
    draft = newline.join(parts)
    text, review = service(Reviewer()).verify(build_answer_contract("예제 3개를 코드와 해설로 보여줘"), draft)
    assert text == draft and review.status == "passed"
    assert review.observed_count == 3 and review.repair_calls == 0


def test_indented_literal_fences_outside_list_are_not_executable_examples():
    from core.answer_verification import _blocks
    literal = "    ```python\n    x = 1\n    ```\n"
    assert not _blocks(literal)


def test_nested_fence_offsets_and_code_tabs_are_preserved():
    from core.answer_verification import _blocks
    draft = '1. 설명\r\n\t```python\r\n\tdef value():\r\n\t    return "a\\tb"\r\n\t```\r\n'
    block, = _blocks(draft)
    assert draft[block.start:block.end].startswith('\t```python')
    assert block.body == 'def value():\r\n    return "a\\tb"\r\n'


def test_contract_ignores_count_inside_source_and_inherits_continuation():
    contract = build_answer_contract('"100개 예시"는 인용이야. 예제 10개를 코드와 함께 설명해줘')
    assert contract.requested_count == 10
    assert contract.requires_review and contract.requires_code and contract.require_code_per_item
    assert "10" in contract.generation_guidance
    followup = build_answer_contract("계속해줘", [{"role": "user", "content": contract.message}])
    assert followup.requested_count == 10


def test_generated_python_fence_triggers_review_without_request_keywords():
    contract = build_answer_contract("원리를 알려줘")
    draft = '원리는 아래와 같습니다.\n```python\nx = 1\n```'
    assert not contract.requires_review and requires_answer_review(contract, draft)
    reviewer = Reviewer()
    _, review = service(reviewer).verify(contract, draft)
    assert review.status == "passed"
    assert review.critique_calls == 1 and not review.code_executed


@pytest.mark.parametrize("count", [9, 11])
def test_missing_or_extra_count_gets_one_repair_before_review(count):
    contract = build_answer_contract("예시 10개를 코드와 함께 설명해줘")
    reviewer, repairer = Reviewer(), Repairer(examples(10))
    text, review = service(reviewer, repairer).verify(contract, examples(count))
    assert text == examples(10) and review.status == "passed"
    assert review.observed_count == 10 and review.requested_count == 10
    assert review.critique_calls == review.repair_calls == 1
    assert set(reviewer.calls[0]["criteria"]) == {"requirements", "code_semantics", *(f"item_{i}" for i in range(1, 11))}


def test_count_ignores_nested_numbering_and_numbers_inside_code():
    draft = ('1. 첫 번째 설명\n   1. 중첩 설명\n```python\n# 2. 주석 번호\ns = "3. 예시"\n```\n'
             '2. 두 번째 설명\n   1. 중첩 설명\n```python\nx = 2\n```')
    _, review = service(Reviewer()).verify(build_answer_contract("예시 2개를 코드와 함께 설명해줘"), draft)
    assert review.status == "passed" and review.observed_count == 2
    assert review.repair_calls == 0


def test_duplicate_item_number_does_not_count_as_complete():
    draft = "1. 첫 설명\n1. 중복 설명"
    text, review = service(repairer=Repairer(draft)).verify(build_answer_contract("예시 2개 설명해줘"), draft)
    assert review.status == "incomplete" and "중복" in " ".join(review.issues)
    assert "검수가 완료되지" in text
    assert review.repair_calls == 1 and review.critique_calls == 0


@pytest.mark.parametrize("bad", [
    lambda payload: {"criteria": []},
    lambda payload: {"criteria": [{"id": key, "status": "passed", "reason": "좋음", "quote": "없는 가짜 인용"}
                                  for key in payload["criteria"]]},
    lambda payload: {"criteria": [{"id": "requirements", "status": "passed", "reason": "좋음", "quote": payload["draft"][:10]}] * len(payload["criteria"])},
    lambda payload: {"criteria": [{"id": key, "status": "passed", "reason": "좋음", "quote": payload["draft"][:10], "extra": True}
                                  for key in payload["criteria"]]},
])
def test_malformed_or_ungrounded_critique_is_unverified_without_retry(bad):
    draft = examples(2)
    reviewer = Reviewer(bad)
    text, review = service(reviewer).verify(build_answer_contract("예시 2개를 코드와 설명해줘"), draft)
    assert review.status == "unverified"
    assert text.endswith(draft) and "검수가 완료되지" in text
    assert review.critique_calls == 1 and review.repair_calls == 0


def test_semantic_failure_repairs_generated_code_once_then_rechecks():
    draft = "코드 예시입니다.\n```python\ndef any_name(n):\n    return 1\n```"
    corrected = "코드 예시입니다.\n```python\ndef any_name(n):\n    return 0\n```"
    reviewer, repairer = Reviewer("failed", "passed"), Repairer(corrected)
    text, review = service(reviewer, repairer).verify(build_answer_contract("코드 예시를 설명해줘"), draft)
    assert text == corrected and review.status == "passed"
    assert review.critique_calls == 2 and review.repair_calls == 1


def test_failed_second_critique_never_loops_or_claims_completion():
    reviewer, repairer = Reviewer("failed", "failed"), Repairer(examples(1))
    _, review = service(reviewer, repairer).verify(build_answer_contract("예시 1개를 코드와 설명해줘"), examples(1))
    assert review.status == "failed"
    assert len(reviewer.calls) == 2 and len(repairer.calls) == 1


def test_source_code_is_immutable_but_not_required_to_be_repeated():
    source = "def provided(n):\n    return n + 1"
    contract = build_answer_contract("실행하지 말고 이 코드를 설명해줘.\n```python\n" + source + "\n```")
    draft = "입력에 1을 더합니다.\n```python\n" + source + "\n```"
    changed = draft.replace("n + 1", "n + 2")
    text, review = service(Reviewer("failed"), Repairer(changed)).verify(contract, draft)
    assert text.endswith(draft) and "n + 2" not in text
    assert review.status != "passed" and review.critique_calls == 1
    explanation = "입력받은 숫자에 1을 더한 값을 반환하는 함수입니다."
    text, review = service(Reviewer()).verify(contract, explanation)
    assert text == explanation and review.status == "passed"


def test_authorized_source_correction_is_allowed():
    source = "def provided(n):\n    return 1"
    contract = build_answer_contract("이 코드 오류를 고쳐줘.\n```python\n" + source + "\n```")
    draft = "코드 예시입니다.\n```python\n" + source + "\n```"
    corrected = draft.replace("return 1", "return 0")
    text, review = service(Reviewer("failed", "passed"), Repairer(corrected)).verify(contract, draft)
    assert text == corrected and review.status == "passed"


def test_verbatim_requirement_cannot_silently_drop_input_source():
    contract = build_answer_contract('코드를 원문 그대로 보여줘.\n```python\nx = 1\n```')
    draft = "설명만 있습니다."
    text, review = service(repairer=Repairer(draft)).verify(contract, draft)
    assert review.status == "incomplete" and review.repair_calls == 1
    assert "원문" in " ".join(review.issues)


def test_python_is_parsed_but_never_executed(monkeypatch):
    writes = []
    monkeypatch.setattr("pathlib.Path.write_text", lambda *args, **kwargs: writes.append(args))
    draft = '실행하지 않는 예시입니다.\n```python\nfrom pathlib import Path\nPath("must-not-exist.txt").write_text("unsafe")\n```'
    reviewer = Reviewer()
    _, review = service(reviewer).verify(build_answer_contract("설명해줘"), draft)
    assert review.status == "passed" and not writes
    assert reviewer.calls[0]["execution_evidence"] == []
    assert review.code_executed is False


def test_bad_python_syntax_requests_repair_not_execution():
    draft = "예시입니다.\n```python\ndef wrong(:\n    pass\n```"
    fixed = "예시입니다.\n```python\ndef arbitrary_name():\n    pass\n```"
    text, review = service(Reviewer(), Repairer(fixed)).verify(build_answer_contract("파이썬 코드 설명해줘"), draft)
    assert text == fixed and review.status == "passed"
    assert review.critique_calls == review.repair_calls == 1


def test_broken_supplied_code_may_be_quoted_when_explaining_error():
    source = "def broken(:\n    pass"
    contract = build_answer_contract("이 코드의 오류를 설명해줘.\n```python\n" + source + "\n```")
    draft = "괄호 안 매개변수 구문이 잘못되어 문법 오류가 납니다.\n```python\n" + source + "\n```"
    _, review = service(Reviewer()).verify(contract, draft)
    assert review.status == "passed" and review.repair_calls == 0


@pytest.mark.parametrize("stage", ["review", "repair"])
def test_cancellation_propagates(stage):
    reviewer = Reviewer(ToolCancelledError("cancelled")) if stage == "review" else Reviewer("failed")
    repairer = Repairer(ToolCancelledError("cancelled"))
    with pytest.raises(ToolCancelledError):
        service(reviewer, repairer).verify(build_answer_contract("코드 설명해줘"), examples(1))


def test_wrapped_cancellation_does_not_fall_back():
    context = TurnExecutionContext("answer-review", "test")
    def cancel(payload):
        context.cancel()
        raise TimeoutError("interrupted transport")
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        service(Reviewer(cancel)).verify(build_answer_contract("코드 설명해줘"), examples(1))


def test_provider_failure_is_unverified_and_keeps_draft():
    draft = examples(1)
    text, review = service(Reviewer(TimeoutError("offline"))).verify(build_answer_contract("코드 설명해줘"), draft)
    assert text.endswith(draft) and review.status == "unverified"
    assert review.critique_calls == 1 and not review.repair_calls
    assert json.loads(json.dumps(review.to_dict()))["code_executed"] is False


def test_oversized_review_never_calls_a_model():
    draft = "긴 답변" * 6000
    text, review = service().verify(build_answer_contract("코드 설명해줘"), draft)
    assert text.endswith(draft) and review.status == "unverified"
    assert review.critique_calls == review.repair_calls == 0


def test_default_local_clients_are_lazy_and_use_existing_roles(monkeypatch):
    roles = []
    reviewer = Reviewer("failed", "passed")
    repairer = Repairer(examples(1))
    monkeypatch.setattr("core.answer_verification._local_client", lambda role: roles.append(role) or (
        reviewer if role == "reasoning" else repairer))
    verifier = AnswerVerificationService()
    assert roles == []
    verifier.verify(build_answer_contract("안녕"), "반가워!")
    assert roles == []
    _, review = verifier.verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert roles == ["reasoning", "code", "reasoning"] and review.status == "passed"


def test_unclosed_fence_failure_retains_draft_and_notice_precedes_it():
    draft = "설명입니다.\n```python\ndef unfinished():"
    text, review = service(repairer=Repairer(draft)).verify(build_answer_contract("코드 설명해줘"), draft)
    assert text.startswith("[답변 검수가") and text.endswith(draft)
    assert review.status == "incomplete" and review.critique_calls == 0


def test_truncated_critique_and_repair_cannot_pass():
    from core.llm import ProseResponse
    partial = ProseResponse("잘린 답변", truncated=True)
    _, review = service(Reviewer(lambda payload: partial)).verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert review.status == "unverified" and review.repair_calls == 0
    text, review = service(Reviewer("failed"), Repairer(partial)).verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert review.status == "failed" and text.endswith(examples(1))


def test_failed_post_repair_review_does_not_reuse_the_old_verdict():
    reviewer = Reviewer("failed", TimeoutError("offline"))
    repaired = examples(1).replace("else n", "else n + 1")
    text, review = service(reviewer, Repairer(repaired)).verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert review.status == "unverified" and not review.criteria_results
    assert text.endswith(repaired) and review.critique_calls == 2
    assert "경계 조건이 잘못되었습니다." not in review.issues


def test_rejected_repair_reports_retained_original_count():
    draft, rejected = examples(2), examples(4)
    text, review = service(repairer=Repairer(rejected)).verify(
        build_answer_contract("예시 3개를 코드와 설명해줘"), draft)
    assert text.endswith(draft) and not text.endswith(rejected)
    assert review.observed_count == 2 and review.requested_count == 3
    assert review.status == "incomplete" and review.critique_calls == 0
    assert any("2개를 확인" in issue for issue in review.issues)
    assert any(issue.startswith("채택하지 않은 수정 후보:") and "4개를 확인" in issue
               for issue in review.issues)


def test_rejected_presentation_repair_keeps_original_critique_evidence():
    draft, rejected = examples(2), examples(1)
    text, review = service(Reviewer(), Repairer(rejected)).verify(
        build_answer_contract("예시 2개를 코드와 설명해줘"), draft,
        repair_hint="말투를 통일하세요.")
    assert text.endswith(draft) and review.observed_count == 2
    assert review.status == "incomplete" and review.critique_calls == 1
    assert review.criteria_results and all(row.quote in draft for row in review.criteria_results)


@pytest.mark.parametrize("replacement", [
    '코드 설명입니다. "alpha source"와 "beta source"와 "alpha source".',
    '코드 설명입니다. "beta source"와 "alpha source".',
    '코드 설명입니다. "alpha source"만 있습니다.',
])
def test_repair_cannot_duplicate_reorder_or_omit_protected_sources(replacement):
    contract = build_answer_contract('이 코드 문구를 설명해줘: "alpha source", "beta source"')
    draft = '코드 설명입니다. "alpha source"와 "beta source"가 있습니다.'
    text, review = service(Reviewer("failed"), Repairer(replacement)).verify(contract, draft)
    assert text.endswith(draft) and review.status != "passed"
    assert review.repair_calls == 1 and review.critique_calls == 1


def test_source_body_crlf_spacing_and_backslashes_are_preserved():
    source = 'path = "C:\\\\demo"\r\nvalue = [1,  2]  '
    contract = build_answer_contract("이 코드를 설명해줘.\r\n```python\r\n" + source + "\r\n```")
    draft = "원본 코드 설명입니다.\r\n```python\r\n" + source + "\r\n```"
    repaired = draft.replace("원본 코드 설명입니다.", "경로와 목록을 정의하는 코드입니다.")
    text, review = service(Reviewer("failed", "passed"), Repairer(repaired)).verify(contract, draft)
    assert text == repaired and review.status == "passed" and source in text


@pytest.mark.parametrize("field,size,expected", [
    ("reason", 500, "passed"), ("reason", 501, "unverified"),
    ("quote", 500, "passed"), ("quote", 501, "unverified"),
])
def test_review_evidence_has_enforced_length_bounds(field, size, expected):
    draft = "검증 대상 " + "a" * 600
    def evidence(payload):
        rows = [{"id": key, "status": "passed", "reason": "정적 검토 결과입니다.",
                 "quote": "검증 대상"} for key in payload["criteria"]]
        for row in rows:
            row[field] = "a" * size
        return {"criteria": rows}
    _, review = service(Reviewer(evidence)).verify(build_answer_contract("코드 설명해줘"), draft)
    assert review.status == expected and review.repair_calls == 0


@pytest.mark.parametrize("raw", [" " * 24_001, {"criteria": [], "padding": "a" * 24_001}], ids=["text", "object"])
def test_oversized_raw_review_is_rejected_before_schema_parsing(raw, monkeypatch):
    def forbidden(*args):
        raise AssertionError("oversized response must not reach JSON parsing")
    monkeypatch.setattr("core.answer_verification.parse_json_object", forbidden)
    _, review = service(Reviewer(raw)).verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert review.status == "unverified" and review.critique_calls == 1
    assert review.issues == ("답변 검수를 완료하지 못했습니다(oversized_review).",)


@pytest.mark.parametrize("raw", [None, {"text": "잘못된 형태"}, "", " " * 24_001 + "수정 답변"])
def test_empty_non_text_and_raw_oversized_repairs_keep_original(raw):
    draft = examples(1)
    text, review = service(Reviewer("failed"), Repairer(raw)).verify(
        build_answer_contract("코드 설명해줘"), draft)
    assert text.endswith(draft) and review.status == "failed"
    assert review.critique_calls == review.repair_calls == 1


def test_item_limit_prevents_any_model_call():
    _, review = service().verify(build_answer_contract("예시 33개를 설명해줘"), examples(1))
    assert review.status == "unverified" and review.requested_count == 33
    assert review.critique_calls == review.repair_calls == 0


def test_already_cancelled_turn_never_constructs_clients():
    context = TurnExecutionContext("already-cancelled-review", "test")
    with bind_turn_context(context):
        context.cancel()
        with pytest.raises(ToolCancelledError):
            service().verify(build_answer_contract("코드 설명해줘"), examples(1))


def test_local_critic_and_repair_use_bounded_context_and_private_release_profiles(monkeypatch):
    from core.llm import OllamaClient
    from core.model_registry import ModelProfile

    shared = {role: ModelProfile(role, "test-only", 0.1, 2048, "5m")
              for role in ("reasoning", "code")}
    calls = []
    reviewer = Reviewer("failed", "passed")
    repaired = examples(1).replace("else n", "else n + 1")

    def initialize(client, role):
        client.role = role
        client.profile = shared[role]

    def structured(client, messages, json_schema=None, *, context_window=None,
                   request_timeout=None, max_output_tokens=None):
        calls.append((client.role, client.profile, context_window, json_schema,
                      request_timeout, max_output_tokens))
        if client.role == "code":
            return repaired
        return reviewer.chat_structured(messages, json_schema)

    def forbidden(*args, **kwargs):
        raise AssertionError("no real network or unbounded chat is allowed")

    monkeypatch.setattr(OllamaClient, "__init__", initialize)
    monkeypatch.setattr(OllamaClient, "chat_structured", structured)
    monkeypatch.setattr(OllamaClient, "chat", forbidden)
    monkeypatch.setattr("core.llm.post_json", forbidden)
    text, review = AnswerVerificationService().verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert text == repaired and review.status == "passed"
    assert [call[0] for call in calls] == ["reasoning", "code", "reasoning"]
    assert all(profile.keep_alive == "0" and profile is not shared[role] and context == 8192
               for role, profile, context, schema, timeout, output in calls)
    assert all(profile.keep_alive == "5m" for profile in shared.values())
    assert all(0 < call[4] <= 45 for call in calls)
    assert [call[5] for call in calls] == [1536, 4096, 1536]
    assert calls[1][3] is None
    assert calls[0][3]["properties"]["criteria"]["maxProperties"] == 2


def test_keyed_criteria_constrains_every_requirement_without_duplicate_ids():
    def keyed(payload):
        return {"criteria": {key: {"status": "passed", "reason": "정적 검토", "quote": payload["evidence_quotes"][0]}
                             for key in payload["criteria"]}}
    draft = examples(3)
    _, review = service(Reviewer(keyed)).verify(build_answer_contract("예시 3개를 코드와 설명해줘"), draft)
    assert review.status == "passed" and len(review.criteria_results) == 5


def test_duplicate_json_object_keys_never_silently_override_a_review():
    raw = '{"criteria": {}, "criteria": {}}'
    _, review = service(Reviewer(raw)).verify(build_answer_contract("코드를 설명해줘"), examples(1))
    assert review.status == "unverified"
    assert "duplicate_json_key" in review.issues[0]


class ReviewClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_review_policy_defaults_are_immutable_and_separate_from_role_settings():
    policy = AnswerReviewPolicy()
    assert (policy.total_seconds, policy.critique_max_tokens, policy.repair_max_tokens) == (45, 1536, 4096)
    with pytest.raises(FrozenInstanceError):
        policy.total_seconds = 1


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "45", 10 ** 400])
def test_review_policy_rejects_invalid_time(value):
    with pytest.raises(ValueError, match="positive finite number"):
        AnswerReviewPolicy(total_seconds=value)


@pytest.mark.parametrize("name", ["critique_max_tokens", "repair_max_tokens"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, float("nan"), float("inf"), "1536"])
def test_review_policy_rejects_invalid_output_limits(name, value):
    with pytest.raises(ValueError, match="positive integer"):
        AnswerReviewPolicy(**{name: value})


@pytest.mark.parametrize("stage", ["initial", "repair", "recheck"])
def test_expired_factory_never_dispatches_the_next_call(stage):
    clock = ReviewClock()
    reviewer = Reviewer("failed", "passed")
    repairer = Repairer(examples(1).replace("else n", "else n + 1"))
    factories = []

    def reviewer_factory():
        kind = "initial" if not reviewer.calls else "recheck"
        factories.append(kind)
        if kind == stage:
            clock.advance(45)
        return reviewer

    def repairer_factory():
        factories.append("repair")
        if stage == "repair":
            clock.advance(45)
        return repairer

    verifier = AnswerVerificationService(reviewer_factory, repairer_factory, clock=clock)
    draft = examples(1)
    text, review = verifier.verify(build_answer_contract("코드 설명해줘"), draft)
    assert review.status == "unverified"
    assert any("review_budget_exhausted" in issue for issue in review.issues)
    expected = {"initial": (0, 0), "repair": (1, 0), "recheck": (1, 1)}[stage]
    assert (review.critique_calls, review.repair_calls) == expected
    assert (len(reviewer.calls), len(repairer.calls)) == expected
    assert factories[-1] == stage
    if stage == "recheck":
        assert text.endswith(repairer.value) and not review.criteria_results
        assert any(issue.startswith("이전 초안 검수:") for issue in review.issues)
    else:
        assert text.endswith(draft)
    if stage == "repair":
        assert "경계 조건이 잘못되었습니다." in review.issues
        assert all(row.quote in draft for row in review.criteria_results)


@pytest.mark.parametrize("stage", ["initial", "repair", "recheck"])
@pytest.mark.parametrize("late_error", [False, True])
def test_late_result_or_error_is_unverified_and_preserves_available_evidence(stage, late_error):
    clock = ReviewClock()

    class TimedReviewer(Reviewer):
        def chat_structured(self, messages, json_schema):
            kind = "initial" if not self.calls else "recheck"
            result = super().chat_structured(messages, json_schema)
            if stage == kind:
                clock.advance(45)
                if late_error:
                    raise TimeoutError("private provider detail")
            return result

    class TimedRepairer(Repairer):
        def chat(self, messages):
            result = super().chat(messages)
            if stage == "repair":
                clock.advance(45)
                if late_error:
                    raise TimeoutError("private provider detail")
            return result

    draft = examples(1)
    repaired = draft.replace("else n", "else n + 1")
    reviewer = TimedReviewer("passed" if stage == "initial" else "failed", "passed")
    repairer = TimedRepairer(repaired)
    verifier = AnswerVerificationService(lambda: reviewer, lambda: repairer, clock=clock)
    text, review = verifier.verify(build_answer_contract("코드 설명해줘"), draft)
    assert review.status == "unverified" and "private provider detail" not in str(review.to_dict())
    assert any("review_budget_exhausted" in issue for issue in review.issues)
    expected = {"initial": (1, 0), "repair": (1, 1), "recheck": (2, 1)}[stage]
    assert (review.critique_calls, review.repair_calls) == expected
    assert (len(reviewer.calls), len(repairer.calls)) == expected
    assert text.endswith(repaired if stage == "recheck" else draft)
    if stage == "repair":
        assert "경계 조건이 잘못되었습니다." in review.issues
        assert review.criteria_results
    elif stage == "recheck":
        assert any(issue.startswith("이전 초안 검수:") for issue in review.issues)
        assert not review.criteria_results


def test_static_work_can_consume_budget_before_any_factory(monkeypatch):
    import core.answer_verification as module
    clock = ReviewClock()
    original = module._static_check

    def slow_static(*args):
        clock.advance(45)
        return original(*args)

    def forbidden():
        pytest.fail("expired verification must not construct clients")

    monkeypatch.setattr(module, "_static_check", slow_static)
    text, review = AnswerVerificationService(forbidden, forbidden, clock=clock).verify(
        build_answer_contract("코드 설명해줘"), examples(1))
    assert text.endswith(examples(1)) and review.status == "unverified"
    assert (review.critique_calls, review.repair_calls) == (0, 0)
    assert "review_budget_exhausted" in review.issues[-1]


def test_budget_is_checked_before_returning_passed_verdict(monkeypatch):
    clock = ReviewClock()
    verifier = AnswerVerificationService(lambda: Reviewer(), clock=clock)
    original = verifier._critique

    def finish_at_deadline(*args):
        result = original(*args)
        clock.advance(45)
        return result

    monkeypatch.setattr(verifier, "_critique", finish_at_deadline)
    text, review = verifier.verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert review.status == "unverified" and text.endswith(examples(1))
    assert "review_budget_exhausted" in review.issues[-1]


@pytest.mark.parametrize("raises", [False, True])
def test_cancellation_wins_over_simultaneous_budget_expiry(raises):
    clock = ReviewClock()
    context = TurnExecutionContext("review-budget-cancel", "test")

    def cancel(payload):
        clock.advance(45)
        context.cancel()
        if raises:
            raise TimeoutError("late transport failure")
        return {"criteria": {}}

    verifier = AnswerVerificationService(lambda: Reviewer(cancel), clock=clock)
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        verifier.verify(build_answer_contract("코드 설명해줘"), examples(1))


def test_local_calls_share_remaining_budget_and_custom_output_caps(monkeypatch):
    from core.llm import OllamaClient
    from core.model_registry import ModelProfile

    clock = ReviewClock()
    reviewer = Reviewer("failed", "passed")
    profile = ModelProfile("reasoning", "test-only", 0.1, 1024, "5m")
    calls = []
    elapsed = iter([9, 12, 3])
    client = object.__new__(OllamaClient)
    client.profile = profile
    repaired = examples(1).replace("else n", "else n + 1")

    def structured(_client, messages, json_schema=None, *, context_window=None,
                   request_timeout=None, max_output_tokens=None):
        calls.append((request_timeout, max_output_tokens, context_window))
        clock.advance(next(elapsed))
        return repaired if json_schema is None else reviewer.chat_structured(messages, json_schema)

    monkeypatch.setattr(OllamaClient, "chat_structured", structured)
    verifier = AnswerVerificationService(lambda: client, lambda: client,
        policy=AnswerReviewPolicy(30, 700, 900), clock=clock)
    text, review = verifier.verify(build_answer_contract("코드 설명해줘"), examples(1))
    assert text == repaired and review.status == "passed"
    assert calls == [(30, 700, 8192), (21, 900, 8192), (9, 700, 8192)]
    assert client.profile is profile and (profile.max_tokens, profile.keep_alive) == (1024, "5m")


def test_reused_service_starts_a_new_budget_for_each_verification():
    clock = ReviewClock()

    class SlowReviewer(Reviewer):
        def chat_structured(self, messages, json_schema):
            result = super().chat_structured(messages, json_schema)
            clock.advance(30)
            return result

    verifier = AnswerVerificationService(lambda: SlowReviewer(), clock=clock)
    for _ in range(2):
        _, review = verifier.verify(build_answer_contract("코드 설명해줘"), examples(1))
        assert review.status == "passed"


@pytest.mark.parametrize("stage", ["initial", "repair", "recheck"])
def test_direct_client_cancellation_wins_without_bound_context(stage):
    clock = ReviewClock()

    class CancellingReviewer(Reviewer):
        def chat_structured(self, messages, json_schema):
            kind = "initial" if not self.calls else "recheck"
            result = super().chat_structured(messages, json_schema)
            if stage == kind:
                clock.advance(45)
                raise ToolCancelledError("cancelled directly by client")
            return result

    class CancellingRepairer(Repairer):
        def chat(self, messages):
            result = super().chat(messages)
            if stage == "repair":
                clock.advance(45)
                raise ToolCancelledError("cancelled directly by client")
            return result

    reviewer = CancellingReviewer("failed", "passed")
    repairer = CancellingRepairer(examples(1))
    verifier = AnswerVerificationService(lambda: reviewer, lambda: repairer, clock=clock)
    with pytest.raises(ToolCancelledError, match="cancelled directly"):
        verifier.verify(build_answer_contract("코드 설명해줘"), examples(1))
    expected = {"initial": (1, 0), "repair": (1, 1), "recheck": (2, 1)}[stage]
    assert (len(reviewer.calls), len(repairer.calls)) == expected


@pytest.mark.parametrize("error", [TimeoutError("private detail"), ValueError("truncated private reply")])
def test_recheck_failure_retains_historical_issues_without_stale_verdict(error):
    draft = examples(1)
    repaired = draft.replace("else n", "else n + 1")
    verifier = service(Reviewer("failed", error), Repairer(repaired))
    text, review = verifier.verify(build_answer_contract("코드 설명해줘"), draft)
    assert text.endswith(repaired) and review.status == "unverified"
    assert not review.criteria_results  # The old failure was about the old draft.
    assert "이전 초안 검수: 경계 조건이 잘못되었습니다." in review.issues
    assert "private" not in str(review.to_dict())
