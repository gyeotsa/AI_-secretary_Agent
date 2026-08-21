"""카카오톡 등 Windows 데스크톱 메신저의 승인 기반 실제 전송 Plugin."""
from __future__ import annotations

from typing import Any, Dict, List
import json
import re

from core.desktop_messaging import DesktopMessagingRuntime
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Evidence, ToolRunResult


class DesktopMessagingPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "desktop_messaging"
        self.description = "수신자 대화창을 검증한 Windows 데스크톱 메신저 전송"
        self.supported_os = ["Windows"]
        self.runtime = DesktopMessagingRuntime()

    def get_tools(self) -> List[ToolSchema]:
        return [ToolSchema(
            "desktop_send_message", "카카오톡 수신자 대화창을 확인한 뒤 메시지를 실제 전송합니다",
            {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["kakaotalk"], "default": "kakaotalk"},
                    "recipient": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                },
                "required": ["provider", "recipient", "message"],
                "additionalProperties": False,
            },
            ["windows_api", "external_send"], side_effect="external_send",
            verification_required=True, timeout_seconds=30, max_retries=0,
        )]

    def get_intents(self) -> List[IntentSchema]:
        return [IntentSchema(
            "messaging.send", "카카오톡 메시지 전송", "desktop_send_message",
            ["카카오톡으로", "카카오 톡으로", "카톡으로", "카톡 보내", "카카오톡 보내"],
            [
                SlotSchema("provider", "사용할 메신저", "어떤 메신저로 보낼까요, 보스?", role="constraint"),
                SlotSchema("recipient", "메시지를 받을 사람", "누구에게 보낼까요, 보스?", role="target"),
                SlotSchema("message", "보낼 메시지", "어떤 내용을 보낼까요, 보스?", role="parameter"),
            ],
            execution_hints=["보내", "전송", "보내줘", "보내줄래"],
            utterance_patterns=[r"(?:카카오\s*톡|카톡|kakaotalk).*(?:에게|한테).*(?:보내|전송)"],
            constraint_slots=["provider"], request_type="external_send",
        )]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name != "messaging.send":
            return slots
        normalized = re.sub(r"카카오\s*톡", "카카오톡", text, flags=re.I)
        if re.search(r"(?:카카오톡|카톡|kakaotalk)", normalized, re.I):
            slots["provider"] = "kakaotalk"
        body = re.sub(r"^.*?(?:카카오톡|카톡|kakaotalk)(?:으로|에서|을|를)?\s*", "", normalized,
                      count=1, flags=re.I)
        match = re.search(
            r"^\s*(?P<recipient>.+?)(?:에게|한테|께)\s*"
            r"(?P<message>.+?)\s*(?:라고|이라고)?\s*(?:보내|전송)",
            body, re.I | re.S,
        )
        if match:
            # 조사 '에게/한테/께'는 정규식 경계에서 이미 제거된다. 이름 자체가
            # '이'로 끝날 수 있으므로 뒤 글자를 임의로 조사로 간주해 자르지 않는다.
            recipient = match.group("recipient").strip()
            message = match.group("message").strip()
            message = re.sub(r"^(?:메시지|내용)(?:로|은|는)?\s*", "", message)
            message = re.sub(r"\s*(?:라고|이라고)$", "", message)
            message = message.strip(" \t\r\n\"'“”‘’")
            if recipient:
                slots["recipient"] = recipient
            if message:
                slots["message"] = message
        return slots

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if tool_name != "desktop_send_message":
            return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 메신저 도구: {tool_name}")
        try:
            result = self.runtime.send(
                str(tool_input.get("provider", "kakaotalk")),
                str(tool_input.get("recipient", "")),
                str(tool_input.get("message", "")),
            )
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=json.dumps(result, ensure_ascii=False),
                evidence=[Evidence(
                    "desktop_message_dispatch",
                    "수신자와 정확히 일치하는 대화창을 전송 직전에 확인하고 키 입력을 전달했습니다.",
                    {
                        "provider": result["provider"], "recipient": result["recipient"],
                        "window_title": result["window_title"], "window_handle": result["window_handle"],
                        "process_id": result["process_id"],
                        "input_dispatched_at": result["input_dispatched_at"],
                        "delivery_receipt_verified": False,
                    },
                )],
            )
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=f"메시지 전송 중단: {exc}")

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name != "desktop_send_message" or str(result).startswith("오류:"):
            return str(result)
        data = json.loads(str(result))
        return (
            f"카카오톡에서 {data['recipient']}님의 대화창을 확인하고 메시지 전송 입력을 완료했습니다. "
            "카카오톡이 제공하는 별도의 수신 확인 정보까지는 확인하지 못했습니다, 보스."
        )
