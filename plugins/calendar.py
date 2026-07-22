"""외부 계정 없이 사용할 수 있는 표준 iCalendar 이벤트 플러그인."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema


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

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        if tool_name != "calendar_create_event":
            return f"오류: 알 수 없는 툴 '{tool_name}'"
        try:
            path = Path(str(tool_input["path"])).expanduser().resolve()
            ok, error = SafetyLayer.validate_path(str(path))
            if not ok:
                return error
            start = datetime.fromisoformat(str(tool_input["start"]))
            end = datetime.fromisoformat(str(tool_input["end"]))
            if end <= start:
                raise ValueError("종료 시간은 시작 시간보다 뒤여야 합니다.")
            fmt = "%Y%m%dT%H%M%S"
            content = "\r\n".join([
                "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JARVIS//AI Secretary//KO",
                "BEGIN:VEVENT", f"UID:{uuid4()}@jarvis.local",
                f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}Z", f"DTSTART:{start.strftime(fmt)}",
                f"DTEND:{end.strftime(fmt)}", f"SUMMARY:{_escape(str(tool_input['title']))}",
                f"DESCRIPTION:{_escape(str(tool_input.get('description', '')))}",
                "END:VEVENT", "END:VCALENDAR", "",
            ])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
            return f"캘린더 이벤트 생성 성공: {path}"
        except Exception as exc:
            return f"오류: {exc}"
