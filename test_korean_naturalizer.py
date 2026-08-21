from core.agent_prompt_policy import agent_response_policy
from core.agent_services import ConversationService
from core.korean_naturalizer import (
    analyze_korean_naturalness, korean_writing_guidance, light_polish_korean,
)


def test_metrics_route_mechanical_korean_more_strictly():
    report = analyze_korean_naturalness(
        "첫째, 중요합니다. 둘째, 중요합니다. 또한, 다양한 측면에서 중요한 역할을 합니다. "
        "결론적으로 시사하는 바가 큽니다."
    )
    assert report.score >= 4
    assert report.route in {"standard", "heavy"}


def test_light_polish_preserves_numbers_paths_urls_and_code():
    source = "결과는 27.5% 입니다 !!! `print('안녕')` C:\\work\\report.py https://example.com"
    polished = light_polish_korean(source)
    for fact in ("27.5%", "`print('안녕')`", "C:\\work\\report.py", "https://example.com"):
        assert fact in polished


def test_guidance_prioritizes_user_style_and_verified_facts():
    guidance = korean_writing_guidance("밝은 반말")
    assert "밝은 반말" in guidance
    assert "수치" in guidance and "도구 결과" in guidance


def test_agent_policy_separates_conversation_from_tools_and_requires_verification():
    policy = agent_response_policy()
    assert "일반 대화" in policy
    assert "검증" in policy
    assert "최신 정보" in policy


def test_conversation_service_receives_natural_korean_and_agent_policy():
    class StubLLM:
        def __init__(self): self.calls = []
        def chat(self, messages):
            self.calls.append(messages)
            return "응, 자연스럽게 말해볼게."

    llm = StubLLM()
    result = ConversationService(llm).respond(
        "안녕", [], assistant_name="아니스", address="지휘관님", style="밝은 반말",
    )
    system = llm.calls[0][0]["content"]
    assert result == "응, 자연스럽게 말해볼게."
    assert "일반 대화" in system and "검증" in system
    assert "밝은 반말" in system
