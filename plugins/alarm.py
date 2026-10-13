"""Plugin Registry based one-shot alarm."""
import json
import re
from decimal import Decimal
from typing import Any, Dict, List

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.scheduler import get_automation_engine
from core.tool_result import Artifact, Evidence, ToolRunResult
from core.utterance_scope import mask_quoted_payloads


_NUMBERS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6,
            "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
_DURATION = re.compile(
    r"(?<![\d.+-])(?P<amount>[+-]?(?:\d+(?:\.\d+)?|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열))"
    r"\s*(?P<unit>시간|분|초)(?P<half>\s*반)?"
)
_MESSAGE = re.compile(r"(?:메시지|내용)\s*[:：]?\s*(.+)", re.S)


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
            ["알람", "타이머", "깨워줘", "깨워 주세요"],
            [SlotSchema("delay_seconds", "알람까지 남은 초", "얼마 뒤에 알람을 울릴까요, 보스? 1초 이상의 시간을 알려주세요. (예: 5분 뒤)")],
            execution_hints=["맞춰", "설정", "울려", "깨워", "알람", "타이머"],
            utterance_patterns=[r"(?:\d+|한|두|세|네)\s*(?:초|분|시간)\s*(?:뒤|후|있다가).{0,12}(?:깨워|알려|울려)"],
        )]

    @staticmethod
    def is_registration_request(text: str) -> bool:
        visible = mask_quoted_payloads(text)
        visible = _MESSAGE.split(visible, maxsplit=1)[0].strip()
        if re.search(r"\S+면(?=\s|[,，])", visible):
            return False
        if re.search(r"알람|타이머", visible):
            return bool(re.search(r"(?:맞춰|설정해|울려|켜|시작해)\s*(?:줘|주세요|줄래)", visible))
        return bool(re.fullmatch(
            r"(?:지금부터\s*)?(?:" + _DURATION.pattern + r"\s*)+\s*(?:뒤|후|있다가)(?:에)?\s*"
            r"(?:깨워|알려|울려)\s*(?:줘|주세요|줄래)[.!?\s]*", visible,
        ))

    @staticmethod
    def is_slot_reply(text: str, current_slots: Dict[str, Any]) -> bool:
        value = str(text).strip(" .!?\t\n")
        value = re.sub(r"\s*(?:뒤|후|있다가)(?:에)?$", "", value)
        return bool(_DURATION.search(value)) and not _DURATION.sub("", value).strip()

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name != "alarm.set_relative":
            return slots
        message_match = _MESSAGE.search(text)
        if message_match:
            slots["message"] = message_match.group(1).strip()
        visible = mask_quoted_payloads(text[:message_match.start()] if message_match else text)
        visible = re.sub(r"지금부터\s*", "", visible)
        visible = re.sub(r"(알람|타이머)(?:을|를)?(?=[+-]?\d)", r"\1 ", visible)
        matches = list(_DURATION.finditer(visible))
        if matches:
            slots.pop("delay_seconds", None)
            if re.search(r"(?:오늘|내일|모레|이틀|하루|매일|매주|매달|반복|마다|전에?|\d\s*시(?!간)|\d\s*일|약|대략|정도|에서|부터|~)", visible):
                return slots
            if matches[0].start() and re.match(r"[\w.+-]", visible[matches[0].start() - 1]):
                return slots
            if any(visible[left.end():right.start()].strip() for left, right in zip(matches, matches[1:])):
                return slots
            seconds = Decimal(0)
            for match in matches:
                amount = Decimal(_NUMBERS.get(match["amount"], match["amount"]))
                if amount < 0:
                    return slots
                multiplier = {"초": 1, "분": 60, "시간": 3600}[match["unit"]]
                seconds += amount * multiplier
                if match["half"]:
                    seconds += Decimal(multiplier) / 2
            if seconds >= 1 and seconds == seconds.to_integral_value():
                slots["delay_seconds"] = int(seconds)
        return slots

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if tool_name != "set_alarm":
            return ToolRunResult.failed(
                tool_name=tool_name, error=f"알 수 없는 툴 '{tool_name}'"
            )
        delay_seconds = tool_input.get("delay_seconds")
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or delay_seconds < 1:
            return ToolRunResult.failed(tool_name=tool_name, error="알람은 1초 이상의 정수 초로 지정해야 합니다.")
        engine = get_automation_engine()
        message = str(tool_input.get("message") or "알람 시간입니다.")
        raw = engine.add_alarm(delay_seconds, message)
        if raw.startswith(("오류:", "알람 등록 오류:")):
            return ToolRunResult.failed(tool_name=tool_name, error=raw)
        try:
            data = json.loads(raw)
            job_id = int(data["job_id"])
            scheduled = next(
                (job for job in engine.scheduled_jobs if int(job["id"]) == job_id),
                None,
            )
            record = engine.get_job_record(job_id)
            completed = bool(record and not record["enabled"] and record["last_run"])
            if (not record or record.get("action_type") != "alarm"
                    or record.get("schedule_type") != "once_at"
                    or record.get("schedule_value") != data["fire_at"]
                    or record.get("prompt") != message
                    or data["delay_seconds"] != delay_seconds
                    or (not completed and (not scheduled or scheduled.get("action_type") != "alarm"))):
                raise ValueError("Scheduler DB에서 등록된 알람을 확인하지 못했습니다.")
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
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        delay = " ".join(f"{amount}{unit}" for amount, unit in
                         ((hours, "시간"), (minutes, "분"), (seconds, "초")) if amount)
        return f"{delay} 뒤에 알람을 울릴게요, 보스."
