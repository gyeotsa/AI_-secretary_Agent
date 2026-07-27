"""외부 계정 없이 사용할 수 있는 표준 iCalendar 이벤트 플러그인."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4
import re
from dateparser.search import search_dates

from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema, IntentSchema, SlotSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


class CalendarPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "calendar"
        self.description = "표준 iCalendar(.ics) 이벤트 생성"

    def get_tools(self) -> List[ToolSchema]:
        return [ToolSchema("calendar_create_event", "ICS 캘린더 이벤트 파일을 생성합니다", {
            "type": "object", "properties": {
                "path": {"type": "string"}, "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 날짜/시간"},
                "end": {"type": "string", "description": "ISO 8601 날짜/시간"},
                "description": {"type": "string", "default": ""},
            }, "required": ["path", "title", "start", "end"],
        }, ["filesystem_write"])]

    def get_intents(self) -> List[IntentSchema]:
        return [IntentSchema(
            name="calendar.create_event",
            description="표준 iCalendar 일정 파일 생성",
            tool_name="calendar_create_event",
            utterance_hints=[".ics", "ics 파일", "캘린더 파일", "일정 파일"],
            slots=[
                SlotSchema("title", "일정 또는 파일 제목", "일정 제목은 무엇으로 할까요, 보스?"),
                SlotSchema("start", "ISO 8601 시작 날짜/시간", "일정은 언제 시작하나요, 보스?"),
                SlotSchema("end", "ISO 8601 종료 날짜/시간", "일정은 언제 끝나나요, 보스?"),
                SlotSchema("path", "저장할 .ics 파일 경로", "어디에 저장할까요, 보스?"),
            ],
            execution_hints=["생성해", "만들어", "작성해", "저장해"],
            follow_up_hints=["다시", "같은 일정", "그 일정", "캘린더로 바꿔"],
            capability_response=(
                "네, 가능합니다, 보스. 표준 iCalendar(.ics) 일정 파일을 생성할 수 있습니다. "
                "필요한 정보가 빠져 있으면 하나씩 확인한 뒤 생성합니다."
            ),
        )]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name != "calendar.create_event":
            return slots

        title_match = re.search(r"([^\s]+?)(?:이라는|라는)\s*이름", text)
        if title_match:
            previous_title = str(slots.get("title", ""))
            slots["title"] = title_match.group(1)
            previous_path = slots.get("path")
            if previous_path and previous_title:
                old_path = Path(str(previous_path))
                if old_path.stem == previous_title:
                    slots["path"] = str(old_path.with_name(f"{slots['title']}.ics"))

        if "바탕화면" in text:
            title = str(slots.get("title", "일정"))
            safe_title = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "_", title).strip("_") or "일정"
            slots["path"] = str(Path.home() / "Desktop" / f"{safe_title}.ics")
        path_match = re.search(r"([A-Za-z]:\\[^\r\n]+?\.ics|/[^\r\n]+?\.ics)", text, re.I)
        if path_match:
            slots["path"] = path_match.group(1)

        dated_matches = []
        for pattern in (
            r"(?P<year>\d{2,4})년\s*(?P<month>\d{1,2})월\s*(?P<day>\d{1,2})일",
            r"(?P<year>\d{2,4})\s*[/.-]\s*(?P<month>\d{1,2})\s*[/.-]\s*(?P<day>\d{1,2})",
        ):
            for match in re.finditer(pattern, text):
                dated_matches.append((match.start(), match.end(), match.groupdict()))
        dated_matches.sort(key=lambda item: item[0])
        parsed_dates = []
        short_dates = []
        for _start, _end, parts in dated_matches:
            year, month, day = parts["year"], parts["month"], parts["day"]
            numeric_year = int(year)
            if numeric_year < 100:
                numeric_year += 2000
            parsed_dates.append(f"{numeric_year:04d}-{int(month):02d}-{int(day):02d}")
        if parsed_dates:
            slots["start"] = parsed_dates[0]
            if len(parsed_dates) > 1:
                slots["end"] = parsed_dates[1]
            else:
                tail = text[dated_matches[0][1]:]
                end_day = re.search(r"(\d{1,2})일(?:에)?\s*끝", tail)
                if end_day:
                    year, month, _ = parsed_dates[0].split("-")
                    slots["end"] = f"{year}-{month}-{int(end_day.group(1)):02d}"
        else:
            short_dates = re.findall(r"(?<!년\s)(\d{1,2})월\s*(\d{1,2})일", text)
            for month, day in short_dates:
                if slots.get("start"):
                    year = str(slots["start"])[:4]
                else:
                    year = str(datetime.now().year)
                value = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
                if not slots.get("start"):
                    slots["start"] = value
                elif not slots.get("end"):
                    slots["end"] = value

        # Standards-based fallback covers additional numeric/ISO date and time
        # variants without making the caller memorize one input grammar.
        if not parsed_dates and not short_dates:
            discovered = search_dates(
                text,
                settings={
                    "RELATIVE_BASE": datetime.now(),
                    "PREFER_DATES_FROM": "future",
                    "RETURN_AS_TIMEZONE_AWARE": False,
                },
            ) or []
            normalized_dates = []
            for matched_text, value in discovered:
                has_time = bool(re.search(r"\d{1,2}:\d{2}", matched_text))
                normalized_dates.append(value.isoformat(timespec="seconds") if has_time else value.date().isoformat())
            if normalized_dates and not slots.get("start"):
                slots["start"] = normalized_dates[0]
            if len(normalized_dates) > 1 and not slots.get("end"):
                slots["end"] = normalized_dates[1]

        times = re.findall(r"(오전|오후)?\s*(\d{1,2})시(?:\s*(\d{1,2})분)?", text)
        if times:
            converted = []
            for meridiem, hour, minute in times:
                numeric_hour = int(hour)
                if meridiem == "오후" and numeric_hour < 12:
                    numeric_hour += 12
                if meridiem == "오전" and numeric_hour == 12:
                    numeric_hour = 0
                converted.append(f"{numeric_hour:02d}:{int(minute or 0):02d}:00")
            if slots.get("start") and "T" not in str(slots["start"]):
                slots["start"] = f"{slots['start']}T{converted[0]}"
            if len(converted) > 1 and slots.get("end") and "T" not in str(slots["end"]):
                slots["end"] = f"{slots['end']}T{converted[1]}"
        return slots

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if tool_name != "calendar_create_event":
            return ToolRunResult.failed(
                tool_name=tool_name, error=f"알 수 없는 툴 '{tool_name}'"
            )
        try:
            path = Path(str(tool_input["path"])).expanduser().resolve()
            ok, error = SafetyLayer.validate_path(str(path))
            if not ok:
                return error
            start_text, end_text = str(tool_input["start"]), str(tool_input["end"])
            start = datetime.fromisoformat(start_text)
            end = datetime.fromisoformat(end_text)
            if end <= start:
                raise ValueError("종료 시간은 시작 시간보다 뒤여야 합니다.")
            fmt = "%Y%m%dT%H%M%S"
            content = "\r\n".join([
                "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JARVIS//AI Secretary//KO",
                "BEGIN:VEVENT", f"UID:{uuid4()}@jarvis.local",
                f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}Z",
                (f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}" if "T" not in start_text else f"DTSTART:{start.strftime(fmt)}"),
                (f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}" if "T" not in end_text else f"DTEND:{end.strftime(fmt)}"),
                f"SUMMARY:{_escape(str(tool_input['title']))}",
                f"DESCRIPTION:{_escape(str(tool_input.get('description', '')))}",
                "END:VEVENT", "END:VCALENDAR", "",
            ])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
            saved = path.read_text(encoding="utf-8")
            required = ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "END:VEVENT", "END:VCALENDAR")
            if not all(marker in saved for marker in required):
                raise ValueError("저장된 파일의 iCalendar 구조 검증에 실패했습니다.")
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=f"캘린더 이벤트 생성 성공: {path}",
                evidence=[Evidence(
                    "icalendar_structure",
                    "저장된 iCalendar 이벤트 구조와 날짜를 확인했습니다.",
                    {
                        "path": str(path),
                        "title": str(tool_input["title"]),
                        "start": start_text,
                        "end": end_text,
                        "size": path.stat().st_size,
                    },
                )],
                artifacts=[Artifact("calendar", str(path), {"format": "ics"})],
            )
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
