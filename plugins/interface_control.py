"""Natural-language control for live interface surfaces."""
from __future__ import annotations

from core.interface_control import get_interface_control_bridge
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Evidence, ToolRunResult


class InterfaceControlPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "interface_control"
        self.description = "실제 UI 화면 제어"

    def get_tools(self):
        return [
            ToolSchema(
                "open_interface_surface",
                "설정·권한·세션·플러그인·지식 그래프 같은 실제 UI 화면을 엽니다",
                {"type": "object", "properties": {
                    "surface": {"type": "string", "enum": [
                        "command_center", "continuity", "permissions", "sessions", "plugins",
                        "mail", "calendar", "voice", "knowledge_graph",
                    ]},
                }, "required": ["surface"]}, [], side_effect="execute",
            ),
        ]

    def get_intents(self):
        return [
            IntentSchema(
                "interface.open_surface", "애플리케이션 관리 화면 열기", "open_interface_surface",
                ["커맨드 센터", "권한 관리", "세션 관리", "플러그인 진단", "목소리 설정",
                 "지식 그래프"],
                [SlotSchema("surface", "열 UI 화면", "어떤 화면을 열까요?")],
                execution_hints=["열", "실행", "보여", "켜"], request_type="execute",
            ),
        ]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        value = " ".join(str(text or "").casefold().split())
        if intent_name == "interface.open_surface":
            mapping = {
                "command_center": ("커맨드 센터", "command center", "통합 센터"),
                "continuity": ("기억 알림", "이어가기", "연속성"),
                "permissions": ("권한", "승인"), "sessions": ("세션", "대화 관리"),
                "plugins": ("플러그인", "도구 진단"), "voice": ("목소리", "음성 설정", "tts"),
                "mail": ("메일", "받은 편지함"), "calendar": ("캘린더", "달력"),
                "knowledge_graph": ("지식 그래프", "지식그래프", "knowledge graph"),
            }
            for key, hints in mapping.items():
                if any(hint in value for hint in hints):
                    slots["surface"] = key
                    break
        return slots

    def execute_tool(self, tool_name, tool_input):
        bridge = get_interface_control_bridge()
        try:
            if tool_name == "open_interface_surface":
                surface = str(tool_input.get("surface", ""))
                bridge.call("open_surface", surface)
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output="요청한 화면을 열었습니다.",
                    evidence=[Evidence("qt_interface", "실행 중인 Qt UI에 화면 열기 이벤트를 전달했습니다.", {"surface": surface})],
                )
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
        return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 도구: {tool_name}")

    def present_result(self, tool_name, result):
        return str(result)
