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
                    "additional_changes": {
                        "type": "object",
                        "additionalProperties": {"type": "string", "minLength": 1},
                    },
                },
                "required": ["setting", "value"],
            },
            [],
            side_effect="change",
        ), ToolSchema(
            "set_user_profile_value", "사용자가 알려준 개인 프로필 값을 영구 저장합니다",
            {"type": "object", "properties": {
                "key": {"type": "string"}, "value": {"type": "string", "minLength": 1},
            }, "required": ["key", "value"]}, [], side_effect="change",
        ), ToolSchema(
            "get_user_profile_value", "저장된 사용자 프로필 값을 조회합니다",
            {"type": "object", "properties": {
                "key": {"type": "string"},
            }, "required": ["key"]}, [], side_effect="read",
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
             "나를 부를", "내 호칭", "사용자 호칭", "응답 스타일", "말투를", "응답 언어",
             "반말로", "반말을", "반말 사용", "존댓말로", "존댓말을", "대답은 반말", "대답은 존댓말", "따라하지마",
             "따라 하지마", "짧게 답해", "간결하게 답해", "자세히 답해"],
            slots,
            execution_hints=["변경", "바꿔", "설정", "지정", "불러", "따라", "답해", "대답", "사용", "말해"],
            utterance_patterns=[
                r"(?:너의|네|비서)\s*(?:호칭|이름).{0,30}(?:변경|바꿔|설정)",
                r"(?:호출어|웨이크워드).{0,30}(?:변경|바꿔|설정)",
                r"(?:나를|내|사용자)\s*(?:부를|호칭).{0,30}(?:변경|바꿔|설정)",
                r"(?:호칭.{0,20}님.{0,20})?(?:반말|존댓말)(?:로|을)?.{0,20}(?:사용|말해|대답|답해)",
            ],
            negation_is_constraint=True,
            request_type="change",
        ), IntentSchema(
            "profile.get_value", "저장된 사용자 개인 정보 조회", "get_user_profile_value",
            ["내 메일주소", "내 메일 주소", "내 이메일", "내 이름", "내 프로필"],
            [SlotSchema("key", "조회할 프로필 항목", "어떤 프로필 정보를 확인할까요?")],
            execution_hints=["말해", "알려", "뭐", "무엇", "확인", "조회"],
            request_type="query",
        ), IntentSchema(
            "profile.set_value", "사용자가 제공한 개인 정보 저장", "set_user_profile_value",
            ["내 메일주소는", "내 메일 주소는", "내 이메일은", "내 이름은"],
            [SlotSchema("key", "저장할 프로필 항목", "어떤 정보인지 알려주세요."),
             SlotSchema("value", "저장할 값", "저장할 값을 알려주세요.")],
            execution_hints=["기억", "저장", "이야", "입니다", "야"],
            request_type="change",
        )]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        normalized = text.strip()
        if intent_name in {"profile.get_value", "profile.set_value"}:
            if re.search(r"(?:메일\s*주소|이메일)", normalized):
                slots["key"] = "email"
            elif re.search(r"(?:내\s*이름|사용자\s*이름)", normalized):
                slots["key"] = "name"
            if intent_name == "profile.set_value":
                email = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", normalized)
                if email:
                    slots["value"] = email.group(0)
                elif "key" in slots:
                    value = re.search(r"(?:은|는|이|가)\s*(.+?)(?:이야|야|입니다|라고\s*(?:기억|저장))?[.!?]*$", normalized)
                    if value:
                        slots["value"] = value.group(1).strip()
            return slots
        if intent_name != "preferences.set_runtime":
            return slots
        speech_style = ""
        if "반말" in normalized and re.search(r"(?:대답|답변|응답|말투|말해|이야기|사용|해|줘)", normalized):
            speech_style = (
                "호칭에는 님을 붙이고 나머지는 자연스러운 반말로 대답"
                if "호칭" in normalized and "님" in normalized
                else "자연스러운 반말로 대답"
            )
        elif "존댓말" in normalized and re.search(r"(?:대답|답변|응답|말투|말해|이야기|사용|해|줘)", normalized):
            speech_style = "자연스러운 존댓말로 대답"
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
        elif re.search(r"(?:내\s*말|사용자\s*말).{0,8}따라\s*하지\s*마", normalized):
            slots["setting"] = "response_style"
            slots["value"] = "사용자 발화를 그대로 반복하지 말고 핵심에 직접 답하는 자연스러운 반말"
        elif re.search(r"(?:짧게|간결하게).{0,8}(?:답해|대답해)", normalized):
            slots["setting"] = "response_style"
            slots["value"] = "핵심만 간결한 반말로 대답"
        elif re.search(r"자세히.{0,8}(?:답해|대답해)", normalized):
            slots["setting"] = "response_style"
            slots["value"] = "필요한 근거와 설명을 포함해 자세한 반말로 대답"

        value_patterns = [
            r"[\"'“”‘’]([^\"'“”‘’]+)[\"'“”‘’]",
            r"(?:호칭|이름|호출어|웨이크워드|말투|스타일|언어)(?:을|를)?\s*([^\s,!.?]+?)(?:으로|로)\s*(?:변경|바꿔|설정|지정)",
            r"([^\s,!.?]+?)(?:으로|로)\s*(?:불러|불러줘|해줘|해주세요)",
        ]
        if not slots.get("value"):
            for pattern in value_patterns:
                match = re.search(pattern, normalized, re.IGNORECASE)
                if match:
                    slots["value"] = match.group(1).strip()
                    break
        changes = {}
        if speech_style:
            changes["response_style"] = speech_style
        elif re.search(r"(?:대답|응답|말투).{0,12}반말", normalized):
            changes["response_style"] = "자연스러운 반말로 대답"
        elif re.search(r"(?:대답|응답|말투).{0,12}존댓말", normalized):
            changes["response_style"] = "자연스러운 존댓말로 대답"
        if speech_style and not slots.get("setting"):
            slots["setting"] = "response_style"
        if speech_style and slots.get("setting") == "response_style":
            slots["value"] = speech_style
        if changes and slots.get("setting") != "response_style":
            slots["additional_changes"] = changes
        elif changes and not slots.get("value"):
            slots["setting"] = "response_style"
            slots["value"] = changes["response_style"]
        return slots

    def execute_tool(self, tool_name, tool_input):
        if tool_name in {"set_user_profile_value", "get_user_profile_value"}:
            profile = get_assistant_settings().profile
            key = str(tool_input.get("key", "")).strip()
            if tool_name == "get_user_profile_value":
                value = profile.get(key, "")
                if not value:
                    return ToolRunResult.failed(tool_name=tool_name, error=f"저장된 {key} 정보가 없습니다.")
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=f"{key}: {value}",
                    evidence=[Evidence("profile_database", "사용자 프로필 DB에서 값을 조회했습니다.", {"key": key})],
                )
            value = str(tool_input.get("value", "")).strip()
            profile.set(key, value)
            return ToolRunResult.successful(
                tool_name=tool_name, raw_output=f"{key} 정보를 저장했습니다.",
                evidence=[Evidence("profile_database", "사용자 제공 프로필 값을 저장하고 다시 조회했습니다.", {
                    "key": key, "verified": profile.get(key, "") == value,
                })], artifacts=[Artifact("profile_entry", key)],
            )
        if tool_name != "set_runtime_preference":
            return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 도구: {tool_name}")
        setting, value = str(tool_input.get("setting", "")), str(tool_input.get("value", ""))
        try:
            settings = get_assistant_settings()
            saved = settings.set(setting, value)
            saved_changes = {setting: saved}
            for key, item in dict(tool_input.get("additional_changes") or {}).items():
                saved_changes[key] = settings.set(key, item)
            labels = [f"{SETTING_DEFINITIONS[key].label}='{item}'" for key, item in saved_changes.items()]
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=", ".join(labels) + "로 변경했습니다.",
                evidence=[Evidence("preference_database", "저장한 설정값을 즉시 다시 확인했습니다.", {
                    "changes": saved_changes,
                })],
                artifacts=[Artifact("preference_entry", key) for key in saved_changes],
            )
        except ValueError as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))

    def present_result(self, tool_name, result):
        if tool_name == "get_user_profile_value" and ":" in str(result):
            key, value = str(result).split(":", 1)
            label = {"email": "메일 주소", "name": "이름"}.get(key.strip(), key.strip())
            return f"저장된 {label}는 {value.strip()}입니다."
        return str(result)
