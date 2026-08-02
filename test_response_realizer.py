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
