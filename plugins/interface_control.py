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
                "set_gesture_sensitivity",
                "손 제스처 스와이프·회전 민감도를 0~100 사이 값으로 변경하고 저장합니다",
                {"type": "object", "properties": {
                    "sensitivity": {"type": "integer", "minimum": 0, "maximum": 100},
                }, "required": ["sensitivity"]}, [], side_effect="change",
            ),
            ToolSchema(
                "get_gesture_configuration",
                "현재 제스처 민감도와 명령 제스처 매핑을 조회합니다",
                {"type": "object", "properties": {}}, [], side_effect="read",
            ),
            ToolSchema(
                "set_gesture_command_mapping",
                "고정 손 모양에 실행할 안전한 UI 동작을 연결하고 명령 제스처를 켜거나 끕니다",
                {"type": "object", "properties": {
                    "pose": {"type": "string", "enum": [
                        "open_palm", "thumbs_up", "closed_fist", "point_up",
                    ]},
                    "action": {"type": "string", "enum": [
                        "", "stop_tts", "approve", "cancel", "next_workspace", "toggle_chat",
                    ]},
                    "enabled": {"type": "boolean"},
                }, "required": ["pose", "action"]}, [], side_effect="change",
            ),
            ToolSchema(
                "open_interface_surface",
                "설정·권한·세션·플러그인·Command Center 같은 실제 UI 화면을 엽니다",
                {"type": "object", "properties": {
                    "surface": {"type": "string", "enum": [
                        "command_center", "permissions", "sessions", "plugins", "voice", "specialists",
                        "gesture",
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
                "interface.gesture_sensitivity", "손 제스처 민감도 변경", "set_gesture_sensitivity",
                ["제스처 민감도", "손동작 민감도", "스와이프 민감도"],
                [SlotSchema("sensitivity", "0~100 민감도", "민감도를 0에서 100 사이 숫자로 알려주세요.")],
                execution_hints=["민감", "감도", "퍼센트", "%", "설정", "변경"],
                utterance_patterns=[
                    r"(?:손\s*)?(?:제스처|손동작|스와이프).{0,8}(?:민감도|감도).{0,8}\d{1,3}",
                ],
                request_type="change",
            ),
            IntentSchema(
                "interface.gesture_mapping", "손 모양별 UI 동작 설정", "set_gesture_command_mapping",
                ["제스처 동작 설정", "손모양 명령 설정", "손바닥 동작", "엄지 동작", "주먹 동작", "검지 동작"],
                [
                    SlotSchema("pose", "손 모양", "손바닥·엄지·주먹·검지 중 어떤 손 모양인가요?"),
                    SlotSchema("action", "실행 동작", "그 손 모양으로 어떤 동작을 실행할까요?"),
                ],
                execution_hints=["연결", "매핑", "설정", "바꿔", "할당"],
                utterance_patterns=[
                    r"(?:손바닥|엄지|주먹|검지|손\s*모양|제스처).{0,40}(?:연결|매핑|설정|바꿔|할당)",
                ], request_type="change",
            ),
            IntentSchema(
                "interface.open_surface", "애플리케이션 관리 화면 열기", "open_interface_surface",
                ["커맨드 센터", "권한 관리", "세션 관리", "플러그인 진단", "목소리 설정", "전문가 작업공간",
                 "제스처 설정"],
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
        elif intent_name == "interface.gesture_sensitivity":
            match = re.search(r"(?<!\d)(100|[1-9]?\d)(?:\s*%|\s*퍼센트)?", value)
            if match:
                slots["sensitivity"] = int(match.group(1))
        elif intent_name == "interface.gesture_mapping":
            pose_hints = {
                "open_palm": ("손바닥", "손 펴", "펼친 손"),
                "thumbs_up": ("엄지", "따봉"),
                "closed_fist": ("주먹", "손 쥐"),
                "point_up": ("검지", "손가락 하나", "가리키"),
            }
            action_hints = {
                "stop_tts": ("음성 중지", "말 멈", "읽기 중지", "tts 중지"),
                "approve": ("승인", "허용", "확인"),
                "cancel": ("취소", "거절"),
                "next_workspace": ("다음 작업", "다음 전문가", "작업공간 전환"),
                "toggle_chat": ("채팅 접", "채팅 펼", "채팅 숨", "채팅 표시"),
                "": ("동작 없음", "해제", "아무것도 하지"),
            }
            for key, hints in pose_hints.items():
                if any(hint in value for hint in hints):
                    slots["pose"] = key
                    break
            if re.search(r"채팅(?:\s*(?:창|영역))?(?:을|를)?\s*(?:접|펼|숨|표시)", value):
                slots["action"] = "toggle_chat"
            for key, hints in action_hints.items():
                if "action" in slots:
                    break
                if any(hint in value for hint in hints):
                    slots["action"] = key
                    break
            if re.search(r"(?:끄|비활성|사용하지)", value):
                slots["enabled"] = False
            elif re.search(r"(?:켜|활성|사용해|연결|설정|할당)", value):
                slots["enabled"] = True
        elif intent_name == "interface.open_surface":
            mapping = {
                "command_center": ("커맨드 센터", "command center", "통합 센터"),
                "permissions": ("권한", "승인"), "sessions": ("세션", "대화 관리"),
                "plugins": ("플러그인", "도구 진단"), "voice": ("목소리", "음성 설정", "tts"),
                "specialists": ("전문가", "작업공간", "작업 공간"),
                "gesture": ("제스처 설정", "손동작 설정", "스와이프 설정"),
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
            if tool_name == "set_gesture_sensitivity":
                sensitivity = max(0, min(100, int(tool_input.get("sensitivity"))))
                configuration = bridge.call(
                    "set_gesture_configuration", {"sensitivity": sensitivity, "persist": True}
                )
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=f"손 제스처 민감도를 {configuration['sensitivity']}%로 설정했습니다.",
                    evidence=[Evidence(
                        "gesture_runtime", "실행 중인 런타임 설정 적용 및 영구 저장을 확인했습니다.",
                        configuration,
                    )],
                )
            if tool_name == "get_gesture_configuration":
                configuration = bridge.call("get_gesture_configuration")
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=f"현재 손 제스처 민감도는 {configuration['sensitivity']}%입니다.",
                    evidence=[Evidence("gesture_runtime", "현재 런타임 설정 조회", configuration)],
                )
            if tool_name == "set_gesture_command_mapping":
                pose = str(tool_input.get("pose", ""))
                action = str(tool_input.get("action", ""))
                allowed_poses = {"open_palm", "thumbs_up", "closed_fist", "point_up"}
                allowed_actions = {"", "stop_tts", "approve", "cancel", "next_workspace", "toggle_chat"}
                if pose not in allowed_poses:
                    raise ValueError(f"지원하지 않는 손 모양입니다: {pose or '(비어 있음)'}")
                if action not in allowed_actions:
                    raise ValueError(f"지원하지 않는 제스처 동작입니다: {action}")
                current = bridge.call("get_gesture_configuration")
                mapping = dict(current.get("gesture_mapping") or {})
                mapping[pose] = action
                enabled_value = tool_input.get("enabled", True)
                enabled = (
                    enabled_value if isinstance(enabled_value, bool)
                    else str(enabled_value).strip().casefold() in {"1", "true", "yes", "on", "사용", "켜짐"}
                )
                configuration = bridge.call("set_gesture_configuration", {
                    "gesture_mapping": mapping,
                    "command_gestures_enabled": enabled,
                    "persist": True,
                })
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output="손 모양 동작 설정을 저장하고 실행 중인 인식기에 적용했습니다.",
                    evidence=[Evidence(
                        "gesture_runtime", "명령 제스처 매핑 적용 및 영구 저장을 확인했습니다.",
                        configuration,
                    )],
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
