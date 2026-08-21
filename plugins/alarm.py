"""Plugin Registry based one-shot alarm."""
import json
import re
from typing import Any, Dict, List

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.scheduler import get_automation_engine
from core.tool_result import Artifact, Evidence, ToolRunResult


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

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if tool_name != "set_alarm":
            return ToolRunResult.failed(
                tool_name=tool_name, error=f"알 수 없는 툴 '{tool_name}'"
            )
        engine = get_automation_engine()
        raw = engine.add_alarm(
            int(tool_input["delay_seconds"]),
            str(tool_input.get("message") or "알람 시간입니다."),
        )
        if raw.startswith(("오류:", "알람 등록 오류:")):
            return ToolRunResult.failed(tool_name=tool_name, error=raw)
        try:
            data = json.loads(raw)
            job_id = int(data["job_id"])
            scheduled = next(
                (job for job in engine.scheduled_jobs if int(job["id"]) == job_id),
                None,
            )
            if not scheduled or scheduled.get("action_type") != "alarm":
                raise ValueError("Scheduler 메모리에서 등록된 알람을 확인하지 못했습니다.")
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=raw,
                evidence=[Evidence(
                    "scheduler_job",
                    "알람 작업의 DB ID와 Scheduler 등록 상태를 확인했습니다.",
                    {
                        "job_id": job_id,
                        "delay_seconds": int(data["delay_seconds"]),
                        "fire_at": data["fire_at"],
                        "engine_running": engine.is_running(),
                    },
                )],
                artifacts=[Artifact("scheduler_job", str(job_id), {"fire_at": data["fire_at"]})],
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc), raw_output=raw)

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
