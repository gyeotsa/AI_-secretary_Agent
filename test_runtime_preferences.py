from types import SimpleNamespace

from core.assistant_settings import AssistantSettings
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.tool_result import ToolRunStatus
from core.voice_runtime import WakeWordCandidateEvaluator
from plugins.preferences import PreferencesPlugin
from main_qt import strip_leading_wake_word


class MemoryProfile:
    def __init__(self):
        self.values = {}

    def get_preference(self, key, default=""):
        return self.values.get(key, default)

    def set_preference(self, key, value):
        self.values[key] = value


def test_assistant_name_persists_and_updates_wake_word():
    profile = MemoryProfile()
    settings = AssistantSettings(profile)
    assert settings.set("assistant_name", "아니스") == "아니스"
    assert settings.assistant_name == "아니스"
    assert settings.wake_word == "아니스"
    assert WakeWordCandidateEvaluator(settings.wake_word).select([
        SimpleNamespace(text="아니스 메모장 켜줘", acoustic_score=0.0, no_speech_probability=0.0)
    ]) == "아니스 메모장 켜줘"


def test_tts_enabled_setting_is_persistent_and_boolean():
    profile = MemoryProfile()
    settings = AssistantSettings(profile)
    assert settings.tts_enabled is True
    assert settings.set_tts_enabled(False) is False
    assert profile.values["tts_enabled"] == "false"
    assert AssistantSettings(profile).tts_enabled is False
    assert settings.set_tts_enabled(True) is True


def test_preference_intent_extracts_name_without_phrase_specific_execution(monkeypatch):
    plugin = PreferencesPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve("너의 호칭을 아니스로 변경해줘,.")
    assert resolution.ready
    assert resolution.tool_name == "set_runtime_preference"
    assert resolution.slots == {"setting": "assistant_name", "value": "아니스"}

    profile = MemoryProfile()
    settings = AssistantSettings(profile)
    monkeypatch.setattr("plugins.preferences.get_assistant_settings", lambda: settings)
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.status == ToolRunStatus.SUCCEEDED
    assert settings.assistant_name == settings.wake_word == "아니스"


def test_wake_word_can_be_configured_independently():
    plugin = PreferencesPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve("호출어를 컴패니언으로 바꿔줘")
    assert resolution.ready
    assert resolution.slots == {"setting": "wake_word", "value": "컴패니언"}


def test_compound_address_and_speech_style_are_both_applied(monkeypatch):
    plugin = PreferencesPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve(
        "내 호칭은 지휘관님으로 불러주고 대신 대답은 반말로 해."
    )
    assert resolution.ready
    assert resolution.slots["setting"] == "user_address"
    assert resolution.slots["value"] == "지휘관님"
    assert resolution.slots["additional_changes"] == {
        "response_style": "호칭에는 님을 붙이고 나머지는 자연스러운 반말로 대답"
    }

    settings = AssistantSettings(MemoryProfile())
    monkeypatch.setattr("plugins.preferences.get_assistant_settings", lambda: settings)
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded
    assert settings.get("user_address") == "지휘관님"
    assert settings.get("response_style") == "호칭에는 님을 붙이고 나머지는 자연스러운 반말로 대답"


def test_profile_question_is_not_misrouted_to_repeat_text(monkeypatch):
    plugin = PreferencesPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    from plugins.system_tools import SystemToolsPlugin
    registry.register_plugin(SystemToolsPlugin())

    resolution = IntentRouter(registry).resolve("내 메일주소 말해봐")
    assert resolution.intent_name == "profile.get_value"
    assert resolution.slots == {"key": "email"}


def test_direct_conversation_behavior_instruction_becomes_persistent_setting():
    plugin = PreferencesPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve("내 말 따라하지마")
    assert resolution.ready
    assert resolution.slots["setting"] == "response_style"
    assert "반복하지 말고" in resolution.slots["value"]


def test_response_style_understands_style_before_or_after_answer_verb():
    plugin = PreferencesPlugin()
    registry = PluginRegistry(); registry.register_plugin(plugin)
    for utterance in ("아니스 지금부터 반말로 대답해", "나에게 대답할 때 반말로 해", "반말로 말해줘"):
        resolution = IntentRouter(registry).resolve(utterance)
        assert resolution.ready
        assert resolution.slots["setting"] == "response_style"
        assert resolution.slots["value"] == "자연스러운 반말로 대답"

    detailed = IntentRouter(registry).resolve("아니스는 기본적으로 호칭만 님을 붙이고 반말을 사용해")
    assert detailed.ready and detailed.slots["setting"] == "response_style"
    assert detailed.slots["value"] == "호칭에는 님을 붙이고 나머지는 자연스러운 반말로 대답"


def test_leading_wake_word_is_removed_without_touching_subject_mentions():
    assert strip_leading_wake_word("아니스 메모장 켜줘", "아니스") == "메모장 켜줘"
    assert strip_leading_wake_word("아니스, 문서 작업모드 열어줘", "아니스") == "문서 작업모드 열어줘"
    assert strip_leading_wake_word("아니스 캐릭터를 조사해줘", "아니스") == "캐릭터를 조사해줘"
    assert strip_leading_wake_word("니케의 아니스를 조사해줘", "아니스") == "니케의 아니스를 조사해줘"
    assert strip_leading_wake_word("아니스", "아니스") == "아니스"


def test_banmal_style_applies_to_tool_result_before_address():
    from core.agent_services import _apply_requested_style
    assert _apply_requested_style("프로그램을 실행했습니다, 지휘관님.", "자연스러운 반말로 대답") == (
        "프로그램을 실행했어, 지휘관님."
    )


def test_banmal_style_does_not_mix_honorific_connectors():
    from core.agent_services import _apply_requested_style
    assert _apply_requested_style(
        "조회 대상을 더 구체적으로 말씀해 주시면 다시 확인하겠습니다, 지휘관님.",
        "자연스러운 반말로 대답",
    ) == "조회 대상을 더 구체적으로 말해 주면 다시 확인할게, 지휘관님."


def test_banmal_style_normalizes_common_mixed_endings():
    from core.agent_services import _apply_requested_style
    assert _apply_requested_style(
        "알겠습니다, 지휘관님. 영상을 봤어요. 더 필요한 건가요?",
        "자연스러운 반말로 대답",
    ) == "알겠어, 지휘관님. 영상을 봤어. 더 필요한 거야?"
    assert _apply_requested_style(
        "아직 학습하지 않았어요. 추가할 내용은 없으시나요?",
        "자연스러운 반말로 대답",
    ) == "아직 학습하지 않았어. 추가할 내용은 없어?"
