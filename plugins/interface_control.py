"""Natural-language control for the live gesture camera and interface surfaces."""
from __future__ import annotations

import re

from core.interface_control import get_interface_control_bridge
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Evidence, ToolRunResult


class InterfaceControlPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "interface_control"
        self.description = "손 제스처 카메라와 실제 UI 화면 제어"

    def get_tools(self):
        return [
            ToolSchema(
                "set_gesture_camera_control",
                "손 제스처 인식용 로컬 카메라를 켜거나 끄고 설정을 영구 저장합니다",
                {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"]},
                [], side_effect="change",
            ),
            ToolSchema(
                "get_gesture_camera_status",
                "손 제스처 카메라의 실제 실행·오류·로컬 처리 상태를 조회합니다",
                {"type": "object", "properties": {}}, [], side_effect="read",
            ),
            ToolSchema(
                "open_interface_surface",
                "설정·권한·세션·플러그인·Command Center 같은 실제 UI 화면을 엽니다",
                {"type": "object", "properties": {
                    "surface": {"type": "string", "enum": [
                        "command_center", "permissions", "sessions", "plugins", "voice", "specialists",
                    ]},
                }, "required": ["surface"]}, [], side_effect="execute",
            ),
        ]

    def get_intents(self):
        return [
            IntentSchema(
                "interface.gesture_camera", "손 제스처 카메라 켜기·끄기", "set_gesture_camera_control",
                ["손 제스처 카메라", "제스처 카메라", "손 인식 카메라", "손동작 카메라",
                 "카메라 켜", "카메라 꺼"],
                [SlotSchema("enabled", "카메라 실행 여부", "손 제스처 카메라를 켤까요, 끌까요?")],
                execution_hints=["켜", "시작", "활성화", "꺼", "중지", "비활성화"],
                utterance_patterns=[
                    r"(?:손\s*)?(?:제스처|손동작|손\s*인식).{0,8}카메라.{0,10}(?:켜|꺼|끄|시작|중지|활성화|비활성화)",
                    r"(?:^|\s)카메라.{0,8}(?:켜|꺼|끄|시작|중지)(?:줘|주세요|라|$)",
                ],
                request_type="change",
            ),
            IntentSchema(
                "interface.gesture_status", "손 제스처 카메라 상태 조회", "get_gesture_camera_status",
                ["손 제스처 상태", "제스처 카메라 상태", "손 인식 상태"], [],
                execution_hints=["상태", "작동", "실행", "켜져", "꺼져"], request_type="query",
            ),
            IntentSchema(
                "interface.open_surface", "애플리케이션 관리 화면 열기", "open_interface_surface",
                ["커맨드 센터", "권한 관리", "세션 관리", "플러그인 진단", "목소리 설정", "전문가 작업공간"],
                [SlotSchema("surface", "열 UI 화면", "어떤 화면을 열까요?")],
                execution_hints=["열", "실행", "보여", "켜"], request_type="execute",
            ),
        ]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        value = " ".join(str(text or "").casefold().split())
        if intent_name == "interface.gesture_camera":
            if re.search(r"(?:꺼|끄|중지|정지|비활성)", value):
                slots["enabled"] = False
            elif re.search(r"(?:켜|시작|활성)", value):
                slots["enabled"] = True
        elif intent_name == "interface.open_surface":
            mapping = {
                "command_center": ("커맨드 센터", "command center", "통합 센터"),
                "permissions": ("권한", "승인"), "sessions": ("세션", "대화 관리"),
                "plugins": ("플러그인", "도구 진단"), "voice": ("목소리", "음성 설정", "tts"),
                "specialists": ("전문가", "작업공간", "작업 공간"),
            }
            for key, hints in mapping.items():
                if any(hint in value for hint in hints):
                    slots["surface"] = key
                    break
        return slots

    def execute_tool(self, tool_name, tool_input):
        bridge = get_interface_control_bridge()
        try:
            if tool_name == "set_gesture_camera_control":
                enabled = bool(tool_input.get("enabled"))
                status = bridge.call("set_gesture_camera", enabled)
                state = "켜짐" if status.get("running") else "꺼짐"
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=f"손 제스처 카메라: {state}",
                    evidence=[Evidence("gesture_runtime", "실제 카메라 런타임 상태를 확인했습니다.", status)],
                )
            if tool_name == "get_gesture_camera_status":
                status = bridge.call("get_gesture_status")
                state = "실행 중" if status.get("running") else "중지됨"
                detail = f" ({status['error']})" if status.get("error") else ""
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=f"손 제스처 카메라가 {state}입니다{detail}",
                    evidence=[Evidence("gesture_runtime", "런타임 상태 조회", status)],
                )
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
