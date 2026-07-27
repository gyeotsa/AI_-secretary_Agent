"""Plugin Registry based one-shot alarm."""
import json
import re
from typing import Any, Dict, List

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.scheduler import get_automation_engine


class AlarmPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "alarm"
        self.description = "한 번만 실행되는 상대 시간 알람"

    def get_tools(self) -> List[ToolSchema]:
        return [ToolSchema(
            "set_alarm", "지정된 시간 뒤 한 번 알림을 발생시킵니다",
            {
                "type": "object",
                "properties": {
                    "delay_seconds": {"type": "integer", "minimum": 1},
                    "message": {"type": "string"},
                },
                "required": ["delay_seconds"],
            },
            ["automation"],
        )]

    def get_intents(self) -> List[IntentSchema]:
        return [IntentSchema(
            "alarm.set_relative", "상대 시간 알람 설정", "set_alarm",
            ["알람", "타이머"],
            [SlotSchema("delay_seconds", "알람까지 남은 초", "얼마 뒤에 알람을 울릴까요, 보스?")],
            execution_hints=["맞춰", "설정", "울려", "알람", "타이머"],
        )]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name != "alarm.set_relative":
            return slots
        match = re.search(r"(\d+)\s*(초|분|시간)\s*(?:뒤|후)?", text)
        if match:
            multiplier = {"초": 1, "분": 60, "시간": 3600}[match.group(2)]
            slots["delay_seconds"] = int(match.group(1)) * multiplier
        message_match = re.search(r"(?:메시지|내용)\s*[:：]?\s*(.+)", text)
        if message_match:
            slots["message"] = message_match.group(1).strip()
        return slots

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        if tool_name != "set_alarm":
            return f"오류: 알 수 없는 툴 '{tool_name}'"
        return get_automation_engine().add_alarm(
            int(tool_input["delay_seconds"]),
            str(tool_input.get("message") or "알람 시간입니다."),
        )

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name != "set_alarm" or result.startswith(("오류:", "알람 등록 오류:")):
            return result
        data = json.loads(result)
        seconds = int(data["delay_seconds"])
        if seconds % 3600 == 0:
            delay = f"{seconds // 3600}시간"
        elif seconds % 60 == 0:
            delay = f"{seconds // 60}분"
        else:
            delay = f"{seconds}초"
        return f"{delay} 뒤에 알람을 울릴게요, 보스."
