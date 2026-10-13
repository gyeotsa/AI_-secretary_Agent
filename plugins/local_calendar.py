"""아니스 내부 캘린더 일정 등록과 채팅의 날짜·시간 확인."""
from datetime import date, datetime, time, timedelta
import re

from core.local_calendar import CALENDAR_TIMEZONE, LocalCalendarStore
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult
from core.utterance_scope import mask_quoted_payloads


_DATE = re.compile(
    r"(?<!\d)(?:(?P<iso_year>\d{4})[-/.](?P<iso_month>\d{1,2})[-/.](?P<iso_day>\d{1,2})|"
    r"(?:(?P<year>\d{4})년\s*)?(?:(?P<month>\d{1,2})월\s*)?(?P<day>\d{1,2})일|"
    r"(?P<relative>오늘|내일|모레))(?:에|부터|까지)?"
)
_TIME = re.compile(
    r"(?<![\d:+-])(?P<meridian>오전|오후)?\s*(?:(?P<colon_hour>\d{1,2}):(?P<colon_minute>\d{2})(?::(?P<second>\d{2}))?|"
    r"(?P<hour>\d{1,2})시(?!간)(?:\s*(?:(?P<minute>\d{1,2})분|(?P<half>반)))?)"
    r"(?:에|부터|까지)?"
)
_ISO_DATETIME = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?"
)
_CLOCK_RANGE = re.compile(
    r"(?<![T\d:+-])(?P<first>(?:오전|오후)?\s*\d{1,2}:\d{2}(?::\d{2})?)\s*[-~∼–]\s*"
    r"(?P<last>(?:오전|오후)?\s*\d{1,2}:\d{2}(?::\d{2})?)"
)
_COMMAND = re.compile(
    r"(?:을|를)?\s*(?:등록|추가|저장|잡아)(?:해|해\s*줘|해\s*주세요|해\s*줄래|해줄래|줘|주세요)?[.!?\s]*$"
)
_DELETE_COMMAND = re.compile(
    r"(?:을|를)?\s*(?:(?:삭제|취소)(?:해\s*(?:줘|주세요|줄래)|해)|지워\s*(?:줘|주세요|줄래))[.!?\s]*$"
)
_FIELD = re.compile(
    r"(?P<name>제목|장소|위치|메모|설명|내용)(?:은|는)?\s*[:：=]?\s*"
    r'(?P<value>"[^"\n]+"|“[^”\n]+”|\'[^\'\n]+\'|[^,;\n]+)'
)
_EXTERNAL_CALENDAR = re.compile(r"구글|google|네이버|naver|카카오|outlook|아웃룩|\.ics|ics\s*파일|캘린더\s*파일|일정\s*파일", re.I)
_REMINDER_ENDING = r"(?:\s*(?:설정(?:해서|하고|해줘|해)?|맞춰(?:서|주고|줘)?|해(?:서|주고|줘)))?"
_REMINDER_TIME = re.compile(
    r"(?P<approx>약\s*|대략\s*)?(?P<amount>[+-]?\d+(?:\.\d+)?(?:\s*[~∼–-]\s*\d+(?:\.\d+)?)?)\s*"
    r"(?P<unit>분|시간)\s*(?P<uncertain>정도|쯤)?\s*전(?:에|으로)?\s*알림(?:을|은|으로)?" + _REMINDER_ENDING
)
_REMINDER_OFF = re.compile(r"알림(?:을|은|는)?\s*(?:없이|끄고|끔|없음|꺼서|꺼줘)" + _REMINDER_ENDING)


def _parse_date(match, today):
    parts = match.groupdict()
    if parts["relative"]:
        return today + timedelta(days={"오늘": 0, "내일": 1, "모레": 2}[parts["relative"]])
    day = int(parts["iso_day"] or parts["day"])
    year = parts["iso_year"] or parts["year"]
    month = parts["iso_month"] or parts["month"]
    if year:
        return date(int(year), int(month) if month else today.month, day)
    if month:
        value = date(today.year, int(month), day)
        return value if value >= today else date(today.year + 1, int(month), day)
    for offset in range(13):
        candidate_month = today.month - 1 + offset
        try:
            value = date(today.year + candidate_month // 12, candidate_month % 12 + 1, day)
        except ValueError:
            continue
        if value >= today:
            return value
    raise ValueError("유효한 날짜가 필요합니다.")


def _parse_time(match):
    parts = match.groupdict()
    hour = int(parts["colon_hour"] or parts["hour"])
    meridian = parts["meridian"]
    if meridian:
        if not 1 <= hour <= 12:
            raise ValueError("오전·오후 시간은 1시부터 12시까지입니다.")
        hour = hour % 12 + (12 if meridian == "오후" else 0)
    elif parts["colon_hour"] is None and 1 <= hour <= 12:
        raise ValueError("오전인지 오후인지 확인해야 합니다.")
    return time(hour, 30 if parts["half"] else int(parts["colon_minute"] or parts["minute"] or 0), int(parts["second"] or 0))


class LocalCalendarPlugin(BasePlugin):
    def __init__(self, store=None):
        super().__init__()
        self.name = "local_calendar"
        self.description = "아니스 내 캘린더에 일정을 로컬 저장하고 실제 등록 내용을 확인"
        self.store = store

    def get_tools(self):
        return [ToolSchema(
            "local_calendar_create_event", "아니스 내부 캘린더에 일정을 등록합니다. 기본 알림은 시작 1시간 전입니다.",
            {"type": "object", "properties": {
                "title": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "start": {"type": "string", "description": "명시된 시작 날짜·시간 ISO 8601. 종일 일정만 날짜 단독 허용. 오전·오후 불명확 시 질문."},
                "end": {"type": "string", "description": "종료 ISO 8601. 생략하면 시간 일정 1시간, 종일 일정 1일. 종일 종료일은 제외."},
                "all_day": {"type": "boolean", "default": False},
                "location": {"type": "string", "default": ""},
                "description": {"type": "string", "default": ""},
                "reminder_minutes": {"type": ["integer", "null"], "minimum": 0, "maximum": 10080,
                                     "default": 60, "description": "시작 몇 분 전 알림. 0은 시작 시각, null은 알림 끔."},
            }, "required": ["title", "start"], "additionalProperties": False},
            [], side_effect="change", max_retries=0,
        ), ToolSchema(
            "local_calendar_delete_event", "아니스 내부 캘린더에서 제목과 시작 날짜가 정확히 일치하는 일정 하나를 삭제합니다. 동명 일정은 시작 시각도 필요합니다.",
            {"type": "object", "properties": {
                "title": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "삭제할 일정의 시작 날짜, 한국 시간 YYYY-MM-DD"},
                "time": {"type": "string", "pattern": r"^(?:[01]\d|2[0-3]):[0-5]\d$", "description": "명시된 시작 시각, 한국 시간 HH:MM. 후보가 여러 개면 확인해야 합니다."},
            }, "required": ["title", "date"], "additionalProperties": False},
            [], side_effect="change", max_retries=0,
        )]

    def get_intents(self):
        return [IntentSchema(
            "local_calendar.create_event", "아니스 내부 캘린더 일정 등록", "local_calendar_create_event",
            ["일정", "스케줄", "스케쥴", "내 캘린더", "내부 캘린더", "캘린더에", "예약"],
            [SlotSchema("title", "일정 제목", "일정은 한 번에 하나씩 등록합니다. 등록할 일정 하나의 제목을 알려주세요."),
             SlotSchema("start", "시작 날짜와 시간", "시작 날짜와 시간을 함께 알려주세요. 1~12시는 오전·오후도 필요합니다. (예: 10월 25일 오후 5시, 종일이면 내일 종일)")],
            execution_hints=["등록", "추가", "저장", "잡아"],
            utterance_patterns=[r"(?:일정|스케[줄쥴]|예약|캘린더).{0,80}(?:등록|추가|저장|잡아)"],
            follow_up_hints=["같은 일정", "그 일정", "다시 등록"],
            capability_response="내 캘린더에 날짜, 시간, 제목을 지정해 일정을 등록할 수 있습니다.",
        ), IntentSchema(
            "local_calendar.delete_event", "아니스 내부 캘린더 일정 삭제", "local_calendar_delete_event",
            ["일정", "스케줄", "스케쥴", "내 캘린더", "내부 캘린더", "예약"],
            [SlotSchema("title", "삭제할 일정 제목", "삭제할 일정 하나의 정확한 제목을 알려주세요."),
             SlotSchema("date", "삭제할 일정 시작 날짜", "삭제할 일정 하나의 시작 날짜를 알려주세요. 시각도 지정하면 오전·오후를 함께 알려주세요. (예: 10월 25일 오후 5시)")],
            execution_hints=["삭제", "지워", "취소"],
            utterance_patterns=[r"(?:일정|스케[줄쥴]|예약|캘린더).{0,80}(?:삭제|지워|취소)"],
            capability_response="내 캘린더에 저장된 일정 하나를 제목과 날짜로 찾아 삭제할 수 있습니다.",
        )]

    def intent_applicable(self, intent_name, text):
        return not _EXTERNAL_CALENDAR.search(text)

    @staticmethod
    def is_registration_request(text):
        text = mask_quoted_payloads(text)
        if _EXTERNAL_CALENDAR.search(text) or re.search(r"알람|타이머|깨워", text):
            return False
        return bool(re.search(
            r"(?:일정|스케[줄쥴]|예약|캘린더).{0,180}(?:(?:등록|추가|저장)해\s*(?:줘|주세요|줄래)|잡아\s*(?:줘|주세요))"
            r"[.!?\s]*$", text,
        ))

    @staticmethod
    def is_deletion_request(text):
        text = mask_quoted_payloads(text)
        return (not _EXTERNAL_CALENDAR.search(text)
                and bool(re.search(r"일정|스케[줄쥴]|예약|캘린더", text))
                and bool(_DELETE_COMMAND.search(text)))

    @staticmethod
    def is_slot_reply(text, current_slots):
        value = str(text).strip(" .!?\t\n")
        field = _FIELD.fullmatch(value)
        if field and field["name"] == "제목":
            return bool(field["value"].strip(' \t"“”\''))
        if _REMINDER_TIME.fullmatch(value) or _REMINDER_OFF.fullmatch(value):
            return True
        value = _CLOCK_RANGE.sub(lambda match: match["first"] + "부터 " + match["last"] + "까지", value)
        has_date_or_time = bool(_DATE.search(value) or _TIME.search(value))
        value = re.sub(_ISO_DATETIME.pattern + r"(?:부터|까지|에)?", " ", value)
        for pattern in (_DATE, _TIME):
            value = pattern.sub(" ", value)
        value = re.sub(r"종일|하루\s*종일", " ", value)
        return has_date_or_time and not value.strip()

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if intent_name == "local_calendar.delete_event":
            return self._extract_delete_slots(text, slots)
        if intent_name != "local_calendar.create_event":
            return slots
        remaining = text
        for match in _FIELD.finditer(text):
            name = {"제목": "title", "장소": "location", "위치": "location", "메모": "description", "설명": "description", "내용": "description"}[match["name"]]
            slots[name] = _COMMAND.sub("", match["value"]).strip(' \t"“”\'')
            remaining = remaining.replace(match.group(), " ", 1)
        quoted = re.search(r'["“\']([^"”\'\n]+)["”\']', remaining)
        if quoted:
            slots["title"] = quoted.group(1)
            remaining = remaining.replace(quoted.group(), " ", 1)
        reminders = list(_REMINDER_TIME.finditer(remaining)) + list(_REMINDER_OFF.finditer(remaining))
        if reminders:
            if len(reminders) != 1:
                slots["reminder_minutes"] = "서로 다른 알림 설정"
            elif reminders[0].re is _REMINDER_OFF:
                slots["reminder_minutes"] = None
            else:
                match = reminders[0]
                amount = match["amount"]
                slots["reminder_minutes"] = (int(amount) * (60 if match["unit"] == "시간" else 1)
                    if re.fullmatch(r"[+-]?\d+", amount) and not match["approx"] and not match["uncertain"]
                    else match.group())
            for match in reminders:
                remaining = remaining.replace(match.group(), " ", 1)
        if re.search(r"(?:전|뒤|후)\s*알림|알림(?:을|은|는)?\s*(?:설정|없이|끄|꺼|끔|없|약|대략|\d)", remaining):
            slots["reminder_minutes"] = "확인이 필요한 알림 설정"
        if re.search(r"종일|하루\s*종일", remaining):
            slots["all_day"] = True
        remaining = _CLOCK_RANGE.sub(lambda match: match["first"] + "부터 " + match["last"] + "까지", remaining)
        today = datetime.now(CALENDAR_TIMEZONE).date()
        dates = list(_DATE.finditer(remaining))
        clocks = list(_TIME.finditer(remaining))
        iso_datetimes = list(_ISO_DATETIME.finditer(remaining))
        endpoints = iso_datetimes if len(iso_datetimes) > 1 else clocks if len(clocks) > 1 else dates
        if len(dates) > 2 or len(clocks) > 2 or len(endpoints) > 2:
            return {}
        if len(endpoints) == 2:
            between = _DATE.sub("", remaining[endpoints[0].end():endpoints[1].start()])
            connector = re.sub(r"(?:부터|까지|에서|\s|[~∼–-]|T)", "", between)
            explicit_range = ("부터" in endpoints[0].group() or "까지" in endpoints[1].group()
                              or bool(re.search(r"부터|까지|에서|[~∼–-]", between)))
            if connector or not explicit_range:
                return {}
        if dates:
            try:
                parsed_dates = [_parse_date(match, today) for match in dates]
                if slots.get("all_day"):
                    if clocks:
                        raise ValueError("종일 일정과 특정 시간을 동시에 지정했습니다.")
                    slots["start"] = parsed_dates[0].isoformat()
                    if len(parsed_dates) > 1:
                        end = parsed_dates[1] + (timedelta(days=1) if dates[1].group().endswith("까지") else timedelta())
                        slots["end"] = end.isoformat()
                elif iso_datetimes:
                    parsed = [datetime.fromisoformat(match.group().replace("Z", "+00:00")) for match in iso_datetimes]
                    slots["start"] = parsed[0].isoformat()
                    if len(parsed) > 1:
                        slots["end"] = parsed[1].isoformat()
                    elif len(clocks) > 1:
                        slots["end"] = datetime.combine(parsed_dates[-1], _parse_time(clocks[1]), CALENDAR_TIMEZONE).isoformat()
                elif clocks:
                    parsed_times = [_parse_time(match) for match in clocks]
                    slots["start"] = datetime.combine(parsed_dates[0], parsed_times[0], CALENDAR_TIMEZONE).isoformat()
                    if len(parsed_times) > 1:
                        slots["end"] = datetime.combine(parsed_dates[-1], parsed_times[1], CALENDAR_TIMEZONE).isoformat()
                else:
                    slots.pop("start", None)
            except ValueError:
                slots.pop("start", None)
        elif clocks and slots.get("start"):
            try:
                existing = datetime.fromisoformat(slots["start"])
                parsed_times = [_parse_time(match) for match in clocks]
                slots["start"] = datetime.combine(existing.date(), parsed_times[0], CALENDAR_TIMEZONE).isoformat()
                if len(parsed_times) > 1:
                    slots["end"] = datetime.combine(existing.date(), parsed_times[1], CALENDAR_TIMEZONE).isoformat()
            except (TypeError, ValueError):
                slots.pop("start", None)
        remaining = re.sub(_ISO_DATETIME.pattern + r"(?:부터|까지|에)?", " ", remaining)
        for pattern in (_DATE, _TIME):
            remaining = pattern.sub(" ", remaining)
        remaining = re.sub(r"(?:아니스(?:야)?[, ]*|(?:내|내부|아니스)?\s*캘린더(?:에|로)?|종일|하루\s*종일)", " ", remaining)
        title = _COMMAND.sub("", remaining).strip(" ,;.!?\t")
        title = re.sub(r"(?:이라는|라는)\s*(?:제목|이름)(?:으로)?$", "", title).strip()
        title = re.sub(r"(?:일정|스케[줄쥴])(?:을|를)?$", "", title).strip()
        if title and "title" not in slots:
            slots["title"] = " ".join(title.split())
        return slots

    @staticmethod
    def _extract_delete_slots(text, slots):
        remaining = text
        field = next((m for m in _FIELD.finditer(text) if m["name"] == "제목"), None)
        quoted = re.search(r'["“\']([^"”\'\n]+)["”\']', text) if field is None else None
        if field or quoted:
            match = field or quoted
            value = field["value"] if field else quoted.group(1)
            slots["title"] = _DELETE_COMMAND.sub("", value).strip(' \t"“”\'')
            remaining = remaining.replace(match.group(), " ", 1)
        dates, clocks = list(_DATE.finditer(remaining)), list(_TIME.finditer(remaining))
        if len(dates) > 1 or len(clocks) > 1:
            return {}
        try:
            iso = _ISO_DATETIME.search(remaining)
            if iso:
                start = datetime.fromisoformat(iso.group().replace("Z", "+00:00"))
                start = start.replace(tzinfo=CALENDAR_TIMEZONE) if start.utcoffset() is None else start.astimezone(CALENDAR_TIMEZONE)
                slots.update(date=start.date().isoformat(), time=start.strftime("%H:%M"))
            else:
                if dates:
                    slots["date"] = _parse_date(dates[0], datetime.now(CALENDAR_TIMEZONE).date()).isoformat()
                if clocks:
                    slots["time"] = _parse_time(clocks[0]).strftime("%H:%M")
        except ValueError:
            # An ambiguous explicit clock must never degrade to date-only deletion.
            slots.pop("date", None)
            slots.pop("time", None)
        remaining = _ISO_DATETIME.sub(" ", remaining)
        for pattern in (_DATE, _TIME):
            remaining = pattern.sub(" ", remaining)
        remaining = re.sub(r"아니스(?:야)?[, ]*|(?:내|내부|아니스)?\s*캘린더(?:에서|에|로)?", " ", remaining)
        title = _DELETE_COMMAND.sub("", remaining).strip(" ,;.!?\t")
        title = re.sub(r"(?:일정|스케[줄쥴])(?:을|를)?$", "", title).strip()
        if title and "title" not in slots:
            slots["title"] = " ".join(title.split())
        return slots

    def _delete_candidates(self, slots):
        title, day = slots.get("title"), slots.get("date")
        if not isinstance(title, str) or not title.strip() or not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("삭제할 일정의 정확한 제목과 시작 날짜(YYYY-MM-DD)가 필요합니다.")
        start_date = date.fromisoformat(day)
        clock = slots.get("time")
        if clock is not None and (not isinstance(clock, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock)):
            raise ValueError("삭제할 일정의 시작 시각은 HH:MM으로 지정해야 합니다.")
        store = self.store or LocalCalendarStore()
        events = store.list_events(start_date, start_date + timedelta(days=1))
        candidates = [event for event in events
                      if event["title"] == title.strip()
                      and datetime.fromtimestamp(event["start_at"], CALENDAR_TIMEZONE).date() == start_date
                      and (clock is None or datetime.fromtimestamp(event["start_at"], CALENDAR_TIMEZONE).strftime("%H:%M") == clock)]
        return store, candidates

    def deletion_question(self, slots):
        if not slots.get("title") or not slots.get("date"):
            return ""
        try:
            _, candidates = self._delete_candidates(slots)
        except Exception:
            return "일정을 조회하지 못해 삭제하지 않았습니다. 내 캘린더를 확인하고 다시 요청해주세요."
        if not candidates:
            return f"{slots['date']}에 제목이 ‘{slots['title']}’인 일정을 찾지 못해 삭제하지 않았습니다. 제목과 날짜를 확인해주세요."
        if len(candidates) > 1:
            times = ", ".join(sorted({datetime.fromtimestamp(e["start_at"], CALENDAR_TIMEZONE).strftime("%H:%M") for e in candidates}))
            if slots.get("time") or len({e["start_at"] for e in candidates}) == 1:
                return "제목과 시작 시각까지 같은 일정이 여러 개라 삭제하지 않았습니다. 내 캘린더에서 삭제할 일정을 선택해주세요."
            return f"제목이 ‘{slots['title']}’인 일정이 {len(candidates)}개입니다. 시작 시각({times}, 한국 시간) 중 삭제할 일정의 시간을 알려주세요."
        return ""

    def _execute_delete(self, tool_input):
        tool_name = "local_calendar_delete_event"
        deleted = None
        try:
            if set(tool_input) - {"title", "date", "time"}:
                raise ValueError("삭제 도구에는 제목, 날짜, 시작 시각만 지정할 수 있습니다.")
            store, candidates = self._delete_candidates(tool_input)
            if len(candidates) != 1:
                raise ValueError(self.deletion_question(tool_input))
            expected = candidates[0]
            deleted = store.delete_event(expected["id"])
            if deleted is None:
                raise ValueError("삭제 직전에 해당 일정이 없어져 삭제하지 않았습니다.")
            if deleted != expected or store.get_event(expected["id"]) is not None or self._delete_candidates(tool_input)[1]:
                raise ValueError("요청한 일정의 삭제 결과를 확인하지 못했습니다.")
            start = datetime.fromtimestamp(deleted["start_at"], CALENDAR_TIMEZONE).strftime("%Y년 %m월 %d일 %H:%M")
            return ToolRunResult.successful(
                tool_name=tool_name, raw_output=f"내 캘린더에서 ‘{deleted['title']}’ 일정을 삭제했습니다.\n시작: {start} (한국 시간)",
                evidence=[Evidence("local_calendar_deleted_event", "정확히 일치하는 일정 하나를 삭제하고 DB에서 부재를 다시 확인했습니다.",
                                   {"db_path": str(store.db_path), "event": deleted})],
            )
        except Exception as exc:
            if deleted:
                return ToolRunResult.unverified(tool_name=tool_name,
                    raw_output=f"일정 삭제 후 확인에 실패했습니다: {exc}\n이미 삭제되었을 수 있으므로 내 캘린더를 확인해주세요.")
            return ToolRunResult.failed(tool_name=tool_name, error=f"일정 삭제 실패: {exc}")

    def execute_tool(self, tool_name, tool_input):
        if tool_name == "local_calendar_delete_event":
            return self._execute_delete(tool_input)
        if tool_name != "local_calendar_create_event":
            return ToolRunResult.failed(tool_name=tool_name, error="알 수 없는 내부 캘린더 도구입니다.")
        saved = None
        try:
            title = tool_input["title"]
            if not isinstance(title, str) or not title.strip():
                raise ValueError("일정 제목이 필요합니다.")
            all_day = tool_input.get("all_day", False)
            if type(all_day) is not bool:
                raise ValueError("종일 여부는 boolean이어야 합니다.")
            values = []
            for name in ("start", "end"):
                value = tool_input.get(name)
                if value is None and name == "end":
                    values.append(values[0] + (timedelta(days=1) if all_day else timedelta(hours=1)))
                    continue
                if not isinstance(value, str) or (not all_day and not re.search(r"[T ]\d{2}:\d{2}", value)):
                    raise ValueError("시간 일정에는 날짜와 시작·종료 시간이 명시되어야 합니다.")
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                values.append(parsed.replace(tzinfo=CALENDAR_TIMEZONE) if parsed.tzinfo is None else parsed.astimezone(CALENDAR_TIMEZONE))
            location = tool_input.get("location", "")
            description = tool_input.get("description", "")
            if not isinstance(location, str) or not isinstance(description, str):
                raise ValueError("장소와 메모는 문자열이어야 합니다.")
            reminder = tool_input.get("reminder_minutes", 60)
            if reminder is not None and (type(reminder) is not int or not 0 <= reminder <= 10080):
                raise ValueError("알림 시간은 0~10080분 정수 또는 null이어야 합니다.")
            store = self.store or LocalCalendarStore()
            expected = {"title": title.strip(), "start_at": int(values[0].timestamp()), "end_at": int(values[1].timestamp()),
                        "all_day": int(all_day), "location": location, "description": description, "reminder_minutes": reminder}
            saved = store.add_event(title, values[0], values[1], all_day=all_day, location=location,
                                    description=description, reminder_minutes=reminder)
            actual = store.get_event(saved["id"])
            if not actual or actual.get("id") != saved["id"] or any(actual.get(name) != value for name, value in expected.items()):
                raise ValueError("저장된 일정의 제목·시간·내용이 등록 요청과 일치하지 않습니다.")
            start_label = values[0].strftime("%Y년 %m월 %d일 %H:%M")
            end_label = values[1].strftime("%Y년 %m월 %d일 %H:%M")
            output = f"내 캘린더에 ‘{actual['title']}’ 일정을 등록했습니다.\n시작: {start_label}\n종료: {end_label} (한국 시간)"
            if all_day:
                output += "\n종일 일정이며 종료일은 포함하지 않습니다."
            if "end" not in tool_input:
                output += "\n종료 시간을 지정하지 않아 기본 " + ("1일로" if all_day else "1시간으로") + " 등록했습니다."
            output += "\n알림: " + ("끔" if reminder is None else "시작 시각" if reminder == 0 else f"시작 {reminder}분 전")
            if location:
                output += f"\n장소: {actual['location']}"
            if description:
                output += f"\n메모: {actual['description']}"
            return ToolRunResult.successful(
                tool_name=tool_name, raw_output=output,
                evidence=[Evidence("local_calendar_event", "로컬 DB에서 등록된 일정을 다시 읽고 전체 내용을 확인했습니다.",
                                   {"db_path": str(store.db_path), "event": actual})],
                artifacts=[Artifact("local_calendar_event", str(actual["id"]), {"db_path": str(store.db_path)})],
            )
        except Exception as exc:
            if saved:
                return ToolRunResult.unverified(tool_name=tool_name,
                    raw_output=f"일정 저장 후 확인에 실패했습니다: {exc}\n이미 저장되었을 수 있으므로 중복 등록 전에 내 캘린더를 확인해주세요.",
                    artifacts=[Artifact("local_calendar_event", str(saved["id"]))])
            return ToolRunResult.failed(tool_name=tool_name, error=f"일정 등록 실패: {exc}")
