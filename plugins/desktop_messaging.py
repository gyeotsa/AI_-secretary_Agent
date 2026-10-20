"""카카오톡 대화 읽기."""
from __future__ import annotations

from typing import Any, Dict, List
import json

from core.desktop_messaging import DesktopMessagingRuntime
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Evidence, ToolRunResult


class DesktopMessagingPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "desktop_messaging"
        self.description = "수신자 대화창을 검증한 카카오톡 읽기"
        self.supported_os = ["Windows"]
        self.runtime = DesktopMessagingRuntime()

    def get_tools(self) -> List[ToolSchema]:
        return [ToolSchema(
            "desktop_read_messages", "지정한 카카오톡 대화방에서 화면에 노출된 메시지를 읽습니다. 전체 기록이나 미확인 메시지 전부를 보장하지 않습니다",
            {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["kakaotalk"], "default": "kakaotalk"},
                    "recipient": {"type": "string", "minLength": 1,
                                  "description": "읽을 상대방 또는 대화방의 정확한 표시 이름"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                              "description": "화면에 노출된 메시지 중 읽을 최대 개수"},
                },
                "required": ["provider", "recipient"],
                "additionalProperties": False,
            },
            ["windows_api"], side_effect="read", verification_required=True,
            timeout_seconds=240, max_retries=0,
        )]

    def get_intents(self) -> List[IntentSchema]:
        return [IntentSchema(
            "messaging.read", "카카오톡 대화 읽기", "desktop_read_messages",
            ["카톡 확인", "카카오톡 읽", "카톡 내용", "카톡 왔", "톡 읽"],
            [
                SlotSchema("provider", "사용할 메신저", "어떤 메신저의 대화를 읽을까요?", role="constraint"),
                SlotSchema("recipient", "읽을 대화방", "누구와 나눈 대화를 읽을까요?", role="target"),
            ],
            execution_hints=["읽", "확인", "보여", "왔", "내용"],
            constraint_slots=["provider"], request_type="query",
        )]

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if tool_name == "desktop_read_messages":
            try:
                result = self.runtime.read(
                    tool_input.get("provider", "kakaotalk"),
                    tool_input.get("recipient", ""), limit=tool_input.get("limit", 20),
                )
                evidence = [Evidence(
                    "desktop_messages_visible", "수신자와 일치하는 대화방에서 접근성 정보 또는 화면 분석으로 메시지 조회를 시도했습니다.",
                    {"recipient": result.get("recipient"), "visible_only": True,
                     "history_complete": False, "read_verified": result.get("read_verified", False),
                     "read_method": result.get("read_method"),
                     "screenshot_sha256": result.get("screenshot_sha256"),
                     "reason": result.get("unverified_reason", "")},
                )]
                factory = ToolRunResult.successful if result.get("read_verified") else ToolRunResult.unverified
                return factory(tool_name=tool_name, raw_output=json.dumps(result, ensure_ascii=False),
                               evidence=evidence)
            except Exception as exc:
                return ToolRunResult.failed(tool_name=tool_name, error=f"카카오톡 읽기 중단: {exc}")
        return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 메신저 도구: {tool_name}")

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name == "desktop_read_messages" and not str(result).startswith("오류:"):
            data = json.loads(str(result))
            if not data.get("read_verified"):
                return (f"{data.get('recipient', '요청한 상대')}님의 대화창에서 메시지 내용을 읽지 못했습니다. "
                        + str(data.get("unverified_reason") or "카카오톡이 메시지 내용을 접근성 도구에 제공하지 않습니다."))
            lines = [f"{data['recipient']}님 대화방의 화면에 노출된 메시지입니다. 전체 기록이나 안 읽은 메시지 전부는 아닙니다."]
            for item in data.get("messages", []):
                sender = item.get("sender") or {"outgoing": "나", "incoming": "상대"}.get(item.get("direction"), "발신자 미확인")
                lines.append(f"{sender}: {item['text']}")
            return "\n".join(lines)
        return str(result)
