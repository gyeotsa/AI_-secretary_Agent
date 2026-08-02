from types import SimpleNamespace

from core.assistant_settings import AssistantSettings
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.tool_result import ToolRunStatus
from core.voice_runtime import WakeWordCandidateEvaluator
from plugins.preferences import PreferencesPlugin


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
