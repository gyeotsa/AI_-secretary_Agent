import pytest

from core.response_realizer import ResponseRealizer, protected_facts


class StubLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return self.response


def test_accepts_natural_rewrite_that_preserves_email():
    llm = StubLLM("등록된 이메일은 ice318@naver.com이야.")
    result = ResponseRealizer(llm).realize(
        "사용자 이메일: ice318@naver.com",
        tool_name="get_user_profile",
        user_request="내 메일 주소 알려줘",
        assistant_name="아니스",
        address="지휘관님",
        style="자연스러운 반말",
    )
    assert result == "등록된 이메일은 ice318@naver.com이야."
    assert len(llm.calls) == 1


def test_rejects_rewrite_that_changes_verified_weather_numbers():
    canonical = "현재 서울의 기온은 27도이고 습도는 64%입니다."
    llm = StubLLM("서울은 지금 22도이고 습도는 40%야.")
    result = ResponseRealizer(llm).realize(
        canonical,
        tool_name="get_weather",
        user_request="서울 날씨 알려줘",
        assistant_name="아니스",
        address="지휘관님",
    )
    assert result == canonical


def test_literal_repeat_bypasses_rewrite():
    llm = StubLLM("바뀐 말")
    result = ResponseRealizer(llm).realize(
        "GAME이라고 말해줘",
        tool_name="repeat_text",
        user_request="그대로 말해줘",
        assistant_name="아니스",
        address="지휘관님",
    )
    assert result == "GAME이라고 말해줘"
    assert llm.calls == []


@pytest.mark.parametrize("tool_name", ["communication_read_summary", "cloud_search_evidence", "cloud_sync_documents"])
def test_grounded_communication_summary_keeps_quote_source_pairs(tool_name):
    canonical = "핵심 내용: 일정은 내일로 변경합니다. [message-17]\n원문: 일정은 내일로 변경합니다."
    llm = StubLLM("일정이 바뀌었대요.")
    result = ResponseRealizer(llm).realize(
        canonical,
        tool_name=tool_name,
        user_request="채널 메시지를 요약해줘",
        assistant_name="아니스",
        address="지휘관님",
        style="반말로 짧게",
    )
    assert result == canonical
    assert llm.calls == []


def test_short_research_report_keeps_inference_and_recommendation_caveats():
    canonical = (
        "추천(추론): 신제품 소개 영상을 준비해 보세요. [S1]\n"
        "한계: 조회수 상승은 보장할 수 없으며 내 계정의 실제 반응은 확인하지 못했습니다.\n"
        "[S1] https://example.org/announcement"
    )
    llm = StubLLM("소개 영상을 올리면 인기가 생겨요. 출처: https://example.org/announcement")
    result = ResponseRealizer(llm).realize(canonical, tool_name="browser_research",
        user_request="어떤 영상을 올리면 인기 있을지 추천해줘", assistant_name="아니스", address="보스")
    assert result == canonical
    assert llm.calls == []


def test_local_research_report_keeps_markdown_prefix(monkeypatch):
    monkeypatch.setattr("core.response_realizer.is_gpt_enabled", lambda: False)
    canonical = "# 조사 결과: 질문\n\n출처: https://example.org/study"
    llm = StubLLM("바꾸지 마세요")
    result = ResponseRealizer(llm).realize(canonical, tool_name="browser_research",
        status="completed", user_request="조사해줘", assistant_name="아니스", address="보스")
    assert result == canonical and llm.calls == []


@pytest.mark.parametrize("tool_name", ["browser_research", ""])
def test_research_failure_remains_the_observed_error_even_without_tool_receipt(tool_name):
    canonical = "요청 해석 모델의 응답 시간이 초과되어 작업을 실행하지 않았습니다."
    llm = StubLLM("선호하는 영상 주제를 알려주시면 인기 콘텐츠를 추천해 드립니다.")
    result = ResponseRealizer(llm).realize(canonical, tool_name=tool_name, status="failed",
        user_request="인스타 트렌드를 조사해줘", assistant_name="아니스", address="보스")
    assert result == canonical and llm.calls == []


def test_filename_is_protected_but_absolute_path_can_be_omitted():
    canonical = r"파일 생성 성공: C:\Users\ice31\Desktop\report.docx"
    assert protected_facts(canonical) == ["report.docx"]
    llm = StubLLM("report.docx 파일을 만들었어.")
    result = ResponseRealizer(llm).realize(
        canonical,
        tool_name="create_word_document",
        user_request="보고서 파일 만들어줘",
        assistant_name="아니스",
        address="지휘관님",
        style="반말",
    )
    assert result == "report.docx 파일을 만들었어."
