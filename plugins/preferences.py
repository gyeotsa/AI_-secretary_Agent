"""Declarative runtime preference intents backed by the persistent settings store."""
from __future__ import annotations

import re

from core.assistant_settings import SETTING_DEFINITIONS, get_assistant_settings
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class PreferencesPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "preferences"
        self.description = "비서 정체성·호출어·호칭·응답 환경설정"

    def get_tools(self):
        return [ToolSchema(
            "set_runtime_preference",
            "사용자 명령으로 런타임 환경설정을 영구 저장하고 즉시 반영합니다",
            {
                "type": "object",
                "properties": {
                    "setting": {"type": "string", "enum": list(SETTING_DEFINITIONS)},
                    "value": {"type": "string", "minLength": 1},
                },
                "required": ["setting", "value"],
            },
            [],
            side_effect="change",
        )]

    def get_intents(self):
        slots = [
            SlotSchema("setting", "변경할 설정", "무엇을 변경할지 알려주세요."),
            SlotSchema("value", "새 설정값", "어떤 값으로 변경할까요?"),
        ]
        return [IntentSchema(
            "preferences.set_runtime", "비서와 대화 환경설정 변경",
            "set_runtime_preference",
            ["너의 호칭", "네 호칭", "너의 이름", "네 이름", "비서 이름", "호출어", "웨이크워드",
             "나를 부를", "내 호칭", "사용자 호칭", "응답 스타일", "말투를", "응답 언어"],
            slots,
            execution_hints=["변경", "바꿔", "설정", "지정", "불러"],
            utterance_patterns=[
                r"(?:너의|네|비서)\s*(?:호칭|이름).{0,30}(?:변경|바꿔|설정)",
                r"(?:호출어|웨이크워드).{0,30}(?:변경|바꿔|설정)",
                r"(?:나를|내|사용자)\s*(?:부를|호칭).{0,30}(?:변경|바꿔|설정)",
            ],
            request_type="change",
        )]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if intent_name != "preferences.set_runtime":
            return slots
        normalized = text.strip()
        if re.search(r"(?:너의|네|비서)\s*(?:호칭|이름)", normalized):
            slots["setting"] = "assistant_name"
        elif re.search(r"(?:호출어|웨이크워드)", normalized):
            slots["setting"] = "wake_word"
        elif re.search(r"(?:나를\s*부를|내\s*호칭|사용자\s*호칭)", normalized):
            slots["setting"] = "user_address"
        elif re.search(r"(?:응답\s*스타일|말투)", normalized):
            slots["setting"] = "response_style"
        elif re.search(r"(?:응답\s*언어|사용\s*언어)", normalized):
            slots["setting"] = "response_language"

        value_patterns = [
            r"[\"'“”‘’]([^\"'“”‘’]+)[\"'“”‘’]",
            r"(?:호칭|이름|호출어|웨이크워드|말투|스타일|언어)(?:을|를)?\s*([^\s,!.?]+?)(?:으로|로)\s*(?:변경|바꿔|설정|지정)",
            r"([^\s,!.?]+?)(?:으로|로)\s*(?:불러|불러줘|해줘|해주세요)",
        ]
        for pattern in value_patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                slots["value"] = match.group(1).strip()
                break
        return slots

    def execute_tool(self, tool_name, tool_input):
        if tool_name != "set_runtime_preference":
            return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 도구: {tool_name}")
        setting, value = str(tool_input.get("setting", "")), str(tool_input.get("value", ""))
        try:
            saved = get_assistant_settings().set(setting, value)
            label = SETTING_DEFINITIONS[setting].label
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=f"{label}을(를) '{saved}'(으)로 변경했습니다.",
                evidence=[Evidence("preference_database", "저장한 설정값을 즉시 다시 확인했습니다.", {
                    "setting": setting, "value": saved,
                })],
                artifacts=[Artifact("preference_entry", setting)],
            )
        except ValueError as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))

    def present_result(self, tool_name, result):
        return str(result)
