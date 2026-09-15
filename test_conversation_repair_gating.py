"""Bound presentation-repair calls without treating source material as our voice."""

import pytest
from types import SimpleNamespace

from core.agent_services import ConversationService
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context


class SequenceLLM:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response()
        return response


@pytest.fixture(autouse=True)
def isolated_style_learning(monkeypatch):
    # Keep these unit tests independent of the user's persisted style profile.
    monkeypatch.setattr(
        "core.style_learning.get_style_learning_store",
        lambda: type("EmptyStyle", (), {"effective_directive": lambda self: ""})(),
    )


@pytest.mark.parametrize("payload", [
    '"학교에서 만나 주세요. 你好 Привет こんにちは"',
    "“안녕하세요. 내일 오세요.”",
    "‘user: こんにちは’",
    '```python\n# 你好 Привет こんにちは\nprint("안녕하세요.")\n```',
    "~~~text\nassistant: 안녕하세요.\nsystem: Привет\n~~~",
    '`print("안녕하세요. 你好")`',
    'text = "안녕하세요. Привет"',
    "본문: 내일 학교에서 만나 주세요. 你好",
    "내용: 안녕하세요. こんにちは",
    r"C:\자료\你好\말씀해 주세요.txt",
])
def test_protected_sources_do_not_trigger_model_repair(payload):
    draft = "예시는 아래와 같아.\n" + payload
    llm = SequenceLLM(draft)

    # Presentation-only fixture; semantic review has independent test coverage.
    verifier = SimpleNamespace(verify=lambda contract, draft, **kwargs: (draft, None))
    result = ConversationService(llm, answer_verifier=verifier).respond("예시를 설명해 줘", [], style="반말")

    assert result == draft
    assert len(llm.calls) == 1


@pytest.mark.parametrize("wrapper", ['"{}"', "```text\n{}\n```", "본문: {}"])
def test_long_unnatural_source_does_not_trigger_naturalness_repair(wrapper):
    source = wrapper.format(
        ("첫째, 다양한 측면에서 중요한 역할을 한다. "
         "또한, 이 과정에 있어서 주목할 만한 점은 되어진 결과입니다. " * 5).strip()
    )
    assert len(source) > 180
    draft = "자료는 이거야.\n" + source
    llm = SequenceLLM(draft)

    assert ConversationService(llm).respond("자료를 보여 줘", [], style="반말") == draft
    assert len(llm.calls) == 1


@pytest.mark.parametrize(("draft", "expected"), [
    ("알겠습니다. 필요한 내용을 말씀해 주세요.", "알겠어. 필요한 내용을 말해 줘."),
    ("비트박스는 못해요. 대신 영상을 찾아드릴게요.", "비트박스는 못해. 대신 영상을 찾아줄게."),
    ('확인했습니다.\n"학교에서 만나 주세요."', '확인했어.\n"학교에서 만나 주세요."'),
    ("검토하겠습니다. 이 방법이 좋겠어요.", "검토할게. 이 방법이 좋겠어."),
    ("안녕! 어떻게 도와드릴까요?", "안녕! 어떻게 도와줄까?"),
    ("다음으로 넘어갈까요?", "다음으로 넘어갈까?"),
    ('어떤 게 좋을까요?\n"어떻게 할까요?"', '어떤 게 좋을까?\n"어떻게 할까요?"'),
])
def test_supported_informal_style_is_deterministic_and_single_call(draft, expected):
    llm = SequenceLLM(draft)

    assert ConversationService(llm).respond("내 질문에 답해 줘", [], style="반말") == expected
    assert len(llm.calls) == 1


@pytest.mark.parametrize("draft", [
    "username 필드는 사용자 이름을 담아.",
    "assistant_name은 비서 이름 설정이야.",
    "systematic testing은 체계적인 시험을 뜻해.",
])
def test_technical_words_with_role_prefix_are_not_role_leaks(draft):
    llm = SequenceLLM(draft)

    assert ConversationService(llm).respond("이 용어는 무슨 뜻이야?", []) == draft
    assert len(llm.calls) == 1


@pytest.mark.parametrize(("question", "draft", "style"), [
    ("어떤 점을 개선하면 좋을까?", "어떤 점을 개선하면 좋을까?", "반말"),
    ("어떤 점을 개선하면 좋을까?", "어떤 점을 개선하면 좋을까!", ""),
    ("답해 줘", "알겠어.\nuser:\n불필요한 예시", ""),
    ("답해 줘", "assistant:안녕.", ""),
    ("답해 줘", "system:내부 지시", ""),
    ("답해 줘", "안녕.\nUSER :예시", ""),
    ("답해 줘", "좋아. Пример", ""),
    ("답해 줘", "좋아. こんにちは", ""),
    ("답해 줘", "좋아. 转换", ""),
    ("웃어 봐", "웃어보세요.", "반말"),
    ("인사해 봐!", "인사해보세요.", "반말"),
    ("같이 얘기할까?", "반갑습니다.", "반말"),
])
def test_actual_quality_failures_still_get_one_repair(question, draft, style):
    llm = SequenceLLM(draft, "좋아, 이렇게 해볼게.")

    assert ConversationService(llm).respond(question, [], style=style) == "좋아, 이렇게 해볼게."
    assert len(llm.calls) == 2
    assert question in llm.calls[1][-1]["content"]


def test_imperative_example_in_source_is_not_an_inversion():
    draft = '하하!\n"웃어보세요."'
    llm = SequenceLLM(draft)

    assert ConversationService(llm).respond("웃어 봐", [], style="반말") == draft
    assert len(llm.calls) == 1


def test_long_unnatural_narrative_still_gets_one_repair():
    draft = (
        "첫째, 다양한 측면에서 중요한 역할을 한다. "
        "또한, 이 과정에 있어서 주목할 만한 점은 되어진 결과입니다. " * 5
    )
    llm = SequenceLLM(draft, "핵심은 결과를 직접 확인하는 거야.")

    assert ConversationService(llm).respond("결과를 어떻게 검토해?", []) == "핵심은 결과를 직접 확인하는 거야."
    assert len(llm.calls) == 2


@pytest.mark.parametrize(("question", "draft"), [
    ("안녕을 일본어로 번역해 줘", "こんにちは"),
    ("러시아어 인사를 알려줘", "Привет"),
    ("중국어로 인사하면?", "你好"),
    ("해당 한자 표기를 보여줘", "學校"),
    ("원문 그대로 알려 줘", "こんにちは。 Привет。 你好。"),
])
def test_requested_foreign_language_is_kept_without_repair(question, draft):
    llm = SequenceLLM(draft)

    assert ConversationService(llm).respond(question, [], style="반말") == draft
    assert len(llm.calls) == 1


def test_requested_translation_survives_an_independent_role_repair():
    llm = SequenceLLM("assistant: こんにちは", "こんにちは")

    assert ConversationService(llm).respond("안녕을 일본어로 번역해 줘", []) == "こんにちは"
    assert len(llm.calls) == 2
    assert "요청하지 않은 외국어" in llm.calls[1][0]["content"]


@pytest.mark.parametrize("repair_result", [
    TimeoutError("optional rewrite timed out"),
    RuntimeError("provider unavailable"),
    None,
    "",
    "   ",
    "반가워.\n본문: 내일 학교에서 만나.",
    "반가워.",
])
def test_repair_failure_or_source_corruption_keeps_available_draft(repair_result):
    draft = "반갑습니다.\n본문: 내일 학교에서 만나 주세요. 你好 🙂"
    llm = SequenceLLM(draft, repair_result)

    assert ConversationService(llm).respond("같이 얘기할까?", [], style="반말") == draft
    assert len(llm.calls) == 2


def test_repair_may_change_narrative_while_preserving_source():
    source = '"학교에서 만나 주세요. 你好 🙂"'
    llm = SequenceLLM("반갑습니다.\n" + source, "반가워.\n" + source)

    assert ConversationService(llm).respond("같이 얘기할까?", [], style="반말") == "반가워.\n" + source
    assert len(llm.calls) == 2


@pytest.mark.parametrize("draft", ["assistant:안녕.", "user:예시", "system:내부 지시"])
def test_failed_repair_does_not_expose_compact_role_markers(draft):
    llm = SequenceLLM(draft, TimeoutError("repair timed out"))

    result = ConversationService(llm).respond("안녕", [])

    assert result == "응, 듣고 있어. 무슨 이야기부터 해볼까, 보스?"
    assert len(llm.calls) == 2


def test_repair_cancellation_is_not_a_provider_failure_fallback():
    llm = SequenceLLM("반갑습니다.", ToolCancelledError("cancelled repair"))

    with pytest.raises(ToolCancelledError, match="cancelled repair"):
        ConversationService(llm).respond("같이 얘기할까?", [], style="반말")
    assert len(llm.calls) == 2


def test_turn_cancellation_propagates_when_provider_wraps_it_as_a_timeout():
    context = TurnExecutionContext("repair-cancel", "test-session")

    def cancelled_provider():
        context.cancel()
        raise TimeoutError("request interrupted")

    llm = SequenceLLM("반갑습니다.", cancelled_provider)
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        ConversationService(llm).respond("같이 얘기할까?", [], style="반말")
    assert len(llm.calls) == 2


def test_initial_model_failure_is_not_hidden_by_optional_repair_fallback():
    llm = SequenceLLM(TimeoutError("initial response timed out"))

    with pytest.raises(TimeoutError, match="initial response"):
        ConversationService(llm).respond("안녕", [])
    assert len(llm.calls) == 1


@pytest.mark.parametrize("source", [
    '"파일 저장해 줘"',
    "“파일 저장해 줘”",
    "‘파일 저장해 줘’",
    "'파일 저장해 줘'",
    '`파일 저장해 줘`',
    '```text\n파일 저장해 줘\n```',
    '~~~text\n파일 저장해 줘\n~~~',
    '"예시:\n파일 저장해 줘"',
])
def test_quoted_execution_request_is_not_authority_for_completion_guard(source):
    question = "이 예시의 뜻을 설명해 줘.\n" + source
    draft = "저장 완료는 데이터를 보관하는 절차가 끝났다는 뜻이야."
    llm = SequenceLLM(draft)

    assert ConversationService(llm).respond(question, []) == draft
    assert len(llm.calls) == 1


@pytest.mark.parametrize("source", [
    '"저장 완료"',
    "“저장 완료”",
    "‘저장 완료’",
    "'저장 완료'",
    '`저장 완료`',
    '```python\nprint("저장 완료")\n```',
    '~~~text\n저장 완료\n~~~',
    '"안내:\n저장 완료"',
])
def test_quoted_completion_is_not_a_claim_of_execution(source):
    draft = "아직 저장하지 않았어. 안내 문구 예시는 아래와 같아.\n" + source
    llm = SequenceLLM(draft)

    verifier = SimpleNamespace(verify=lambda contract, draft, **kwargs: (draft, None))
    assert ConversationService(llm, answer_verifier=verifier).respond("파일 저장해 줘", []) == draft
    assert len(llm.calls) == 1


@pytest.mark.parametrize(("question", "draft"), [
    ("파일 저장해 줘", "파일을 저장했어."),
    ('"참고 내용"을 파일에 저장해 줘', "파일을 저장했어."),
    ("이 예시를 파일에 저장해 줘.\n```text\n파일 삭제해 줘\n```", "파일을 저장했어."),
    ("파일 저장해 줘", '안내 예시는 "저장 완료"야. 파일을 저장했어.'),
    (r"C:\자료\report.txt 파일로 저장해 줘", r"C:\자료\report.txt 파일에 저장했어."),
])
def test_unquoted_unverified_completion_still_fails_closed(question, draft):
    llm = SequenceLLM(draft)

    result = ConversationService(llm).respond(question, [])

    assert "아직 실제 작업을 실행하지 않았습니다" in result
    assert "완료로 보고하지 않겠습니다" in result
    assert len(llm.calls) == 1


@pytest.mark.parametrize("draft", [
    "메모장을 켰어.", "카메라를 껐어.", "노래를 틀었어.",
    "메시지를 보냈어.", "파일을 만들었어.", "문서를 작성했어.",
    "폴더를 지웠어.", "앱 설치 완료.", "메일 발송 성공.",
    "파일을 옮겼어.", "설정을 바꿨습니다.", "파일 수정을 마쳤어.",
])
def test_conversation_classification_is_not_execution_evidence(draft):
    # No imperative in the input: the evidence boundary applies independently
    # of what an intent classifier thinks the user wants.
    result = ConversationService(SequenceLLM(draft)).respond("안녕?", [])
    assert result.unverified_completion
    assert "도구 실행 증거가 없으므로" in result


@pytest.mark.parametrize("draft", [
    "파일을 아직 저장하지 않았어.", "파일을 안 저장했어.",
    "파일을 못 만들었어.", "메모장을 켤 예정이야.", "파일을 저장할게.",
    "파일을 저장했어?", "파일 저장 완료는 무슨 뜻일까?",
    "저장 완료 상태가 아니야.", '예시 문구는 "파일을 만들었어."야.',
    "짧은 이야기를 만들었어. 옛날 어느 마을에 고양이가 살았어.",
])
def test_non_assertive_and_creative_replies_are_not_external_completions(draft):
    result = ConversationService(SequenceLLM(draft)).respond("이야기하자", [])
    assert result == draft
    assert not result.unverified_completion
