from types import SimpleNamespace

from config import Config


class RuntimeSettings:
    assistant_name = "아니스"

    def __init__(self):
        self.values = {
            "response_language": "한국어와 영어 병기",
            "response_style": "친근한 반말로 설명하되 분석은 논리적으로",
        }

    def get(self, key):
        return self.values.get(key, "")


def test_system_prompt_is_adaptive_and_allows_full_analysis(monkeypatch):
    monkeypatch.setattr(
        "core.assistant_settings.get_assistant_settings",
        lambda: RuntimeSettings(),
    )

    prompt = Config.get_system_prompt(
        {"name": "지원", "preferences": "근거와 검증 결과를 함께 설명"}
    )

    assert "비서 이름: 아니스" in prompt
    assert "응답 언어: 한국어와 영어 병기" in prompt
    assert "친근한 반말로 설명하되 분석은 논리적으로" in prompt
    assert "여러 조건이 있는 분석" in prompt
    assert "세부 내용을 충분히 설명" in prompt
    assert "답변 길이와 형식을 인위적으로 제한하지 마세요" in prompt
    assert "이름: 지원" in prompt
    assert "근거와 검증 결과를 함께 설명" in prompt


def test_system_prompt_has_no_legacy_length_address_or_mode_constraints(monkeypatch):
    monkeypatch.setattr(
        "core.assistant_settings.get_assistant_settings",
        lambda: RuntimeSettings(),
    )
    prompt = Config.get_system_prompt()

    for legacy_fragment in (
        "25자 이내",
        "한 문장만",
        "보스",
        "[GAME]",
        "[WORK]",
        "토니 스타크",
        "아이언맨",
    ):
        assert legacy_fragment not in prompt


def test_base_llm_client_uses_the_single_dynamic_prompt_builder(monkeypatch):
    from core.llm import BaseLLMClient

    monkeypatch.setattr("core.llm.get_tools_schema", lambda: [])
    monkeypatch.setattr(
        Config,
        "get_system_prompt",
        classmethod(lambda cls, user_profile=None: "runtime-adaptive-prompt"),
    )

    client = BaseLLMClient()

    assert client.system_prompt == "runtime-adaptive-prompt"
    assert client.tools == []
