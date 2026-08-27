"""Persistent, runtime-readable assistant identity and conversation settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from core.user_profile import get_user_profile


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    label: str
    default: str
    max_length: int = 80


SETTING_DEFINITIONS: Dict[str, SettingDefinition] = {
    "assistant_name": SettingDefinition("assistant_name", "비서 이름", "자비스", 30),
    "wake_word": SettingDefinition("wake_word", "음성 호출어", "자비스", 30),
    "user_address": SettingDefinition("user_address", "사용자 호칭", "", 30),
    "response_style": SettingDefinition("response_style", "응답 스타일", "", 200),
    "response_language": SettingDefinition("response_language", "응답 언어", "한국어", 30),
    "gesture_camera_enabled": SettingDefinition(
        "gesture_camera_enabled", "손 제스처 카메라 자동 실행", "true", 5
    ),
    "gesture_sensitivity": SettingDefinition(
        "gesture_sensitivity", "손 제스처 민감도", "60", 3
    ),
    "gesture_command_enabled": SettingDefinition(
        "gesture_command_enabled", "명령 제스처 사용", "false", 5
    ),
    "gesture_mapping": SettingDefinition(
        "gesture_mapping", "제스처 동작 매핑",
        '{"open_palm":"stop_tts","thumbs_up":"approve",'
        '"closed_fist":"cancel","point_up":"next_workspace"}',
        500,
    ),
}


class AssistantSettings:
    """Single runtime source for settings that must change without restarting."""

    def __init__(self, profile=None):
        self.profile = profile or get_user_profile()

    def get(self, key: str) -> str:
        definition = SETTING_DEFINITIONS[key]
        value = " ".join(self.profile.get_preference(key, definition.default).strip().split())
        return value or definition.default

    def set(self, key: str, value: str) -> str:
        if key not in SETTING_DEFINITIONS:
            raise ValueError(f"지원하지 않는 설정입니다: {key}")
        definition = SETTING_DEFINITIONS[key]
        normalized = " ".join(str(value).strip(" \t\r\n,.'\"!?，。！？").split())
        if not normalized:
            raise ValueError(f"{definition.label} 값은 비어 있을 수 없습니다.")
        if len(normalized) > definition.max_length:
            raise ValueError(f"{definition.label} 값은 {definition.max_length}자 이하여야 합니다.")
        self.profile.set_preference(key, normalized)
        # A renamed assistant should be callable by that name immediately. Users can
        # still set wake_word separately afterwards when they want the two to differ.
        if key == "assistant_name":
            self.profile.set_preference("wake_word", normalized)
        return normalized

    @property
    def assistant_name(self) -> str:
        return self.get("assistant_name")

    @property
    def wake_word(self) -> str:
        return self.get("wake_word")


_assistant_settings = None


def get_assistant_settings() -> AssistantSettings:
    global _assistant_settings
    if _assistant_settings is None:
        _assistant_settings = AssistantSettings()
    return _assistant_settings
