"""일정 등록은 명시한 날짜와 제목을 보존하고 저장 후 다시 확인한다."""
from datetime import date, datetime, timedelta

import pytest

from core.intent_router import IntentRouter
from core.local_calendar import CALENDAR_TIMEZONE, LocalCalendarStore
from core.plugin import PluginRegistry
from core.semantic_request import SemanticRequestInterpreter
from core.tool_result import ToolRunStatus
from plugins.alarm import AlarmPlugin
from plugins.calendar import CalendarPlugin
from plugins.local_calendar import LocalCalendarPlugin


class _DateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 11, 14, tzinfo=CALENDAR_TIMEZONE)


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    monkeypatch.setattr("plugins.local_calendar.datetime", _DateTime)
    store = LocalCalendarStore(tmp_path / "calendar.db")
    plugin = LocalCalendarPlugin(store)
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    registry.register_plugin(CalendarPlugin())
    registry.register_plugin(AlarmPlugin())
    yield plugin, store, registry, IntentRouter(registry)
    registry.shutdown()


@pytest.mark.parametrize("text,title,start", [
    ("10월 25일 오후 5시에 치과 예약을 등록해줘", "치과 예약", "2026-10-25T17:00:00+09:00"),
    ("내일 오후 3시에 회의 일정 추가해줘", "회의", "2026-10-12T15:00:00+09:00"),
    ("2026-10-25 17:00 회의 일정 등록해줘", "회의", "2026-10-25T17:00:00"),
    ("모레 오전 12시 반에 팀 회의 일정 등록해줘", "팀 회의", "2026-10-13T00:30:00+09:00"),
    ("25일 오후 5시 치과 예약 잡아줘", "치과 예약", "2026-10-25T17:00:00+09:00"),
    ("5일 오후 5시 회의 일정 등록해줘", "회의", "2026-11-05T17:00:00+09:00"),
    ("1월 5일 17:00 회의 일정 등록해줘", "회의", "2027-01-05T17:00:00+09:00"),
    ("내일 오후 5:30 치과 예약 등록해줘", "치과 예약", "2026-10-12T17:30:00+09:00"),
])
def test_chat_resolves_explicit_numeric_and_relative_dates(calendar, text, title, start):
    _, _, registry, router = calendar
    resolution = router.resolve(text)
    assert resolution.ready and resolution.execution_requested
    assert resolution.intent_name == "local_calendar.create_event"
    assert resolution.slots == {"title": title, "start": start}
    assert registry.validate_contracts() == {}


@pytest.mark.parametrize("text", [
    "25일 5시에 치과 예약 등록해줘",
    "10월 25일 치과 예약 등록해줘",
    "오후 5시에 치과 예약 등록해줘",
    "2월 30일 오후 5시 치과 예약 등록해줘",
    "내일 오후 17시 치과 예약 등록해줘",
    "내일 오후 3시부터 5시까지 회의 일정 등록해줘",
    "내일 종일 오후 3시 회의 일정 등록해줘",
])
def test_missing_or_ambiguous_date_time_asks_instead_of_guessing(calendar, text):
    resolution = calendar[3].resolve(text)
    assert resolution.matched and not resolution.ready
    assert "start" not in resolution.slots
    assert "날짜와 시간" in resolution.question


def test_date_and_title_followups_keep_confirmed_slots(calendar):
    router = calendar[3]
    first = router.resolve("치과 예약 일정 등록해줘")
    assert first.slots == {"title": "치과 예약"} and first.question
    second = router.resolve("10월 25일 오후 5시", first.intent_name, first.slots)
    assert second.ready and second.slots["title"] == "치과 예약"
    empty_title = router.resolve("내일 오후 3시 일정 등록해줘")
    assert empty_title.question and "title" not in empty_title.slots
    titled = router.resolve("제목은 팀 회의", empty_title.intent_name, empty_title.slots)
    assert titled.ready and titled.slots["title"] == "팀 회의"
    assert titled.slots["start"] == empty_title.slots["start"]


def test_explicit_title_notes_location_are_preserved(calendar):
    text = '내일 오후 3시 일정 등록해줘, 제목은 "프로젝트 Q4 검토", 장소는 "본사 2층", 메모는 "A/B 비용 1,234원 확인"'
    resolution = calendar[3].resolve(text)
    assert resolution.ready
    assert resolution.slots["title"] == "프로젝트 Q4 검토"
    assert resolution.slots["location"] == "본사 2층"
    assert resolution.slots["description"] == "A/B 비용 1,234원 확인"
    assert calendar[3].resolution_preserves_user_content(text, resolution)


def test_real_store_readback_and_default_duration(calendar):
    plugin, store, _, router = calendar
    resolution = router.resolve("10월 25일 오후 5시에 치과 예약을 등록해줘")
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded
    actual = result.evidence[0].data["event"]
    assert result.evidence[0].kind == "local_calendar_event"
    assert result.evidence[0].data["db_path"] == str(store.db_path)
    assert actual == LocalCalendarStore(store.db_path).get_event(actual["id"])
    assert actual["end_at"] - actual["start_at"] == 3600
    assert "기본 1시간" in result.raw_output and "17:00" in result.raw_output and "18:00" in result.raw_output
    assert result.artifacts[0].uri == actual["id"]


def test_all_day_defaults_to_one_day_and_inclusive_korean_end_date(calendar):
    plugin, store, _, router = calendar
    one = router.resolve("내일 종일 휴가 일정 등록해줘")
    assert one.ready and one.slots == {"title": "휴가", "start": "2026-10-12", "all_day": True}
    result = plugin.execute_tool(one.tool_name, one.slots)
    assert result.succeeded and "기본 1일" in result.raw_output
    event = result.evidence[0].data["event"]
    assert event["end_at"] - event["start_at"] == 86400
    assert store.list_events(date(2026, 10, 13), date(2026, 10, 14)) == []
    multiple = router.resolve("10월 25일부터 26일까지 종일 휴가 일정 등록해줘")
    assert multiple.ready and multiple.slots["end"] == "2026-10-27"


def test_explicit_end_and_utc_timezone_are_respected(calendar):
    plugin, _, _, router = calendar
    resolution = router.resolve("내일 오후 3시부터 오후 4시 30분까지 회의 일정 등록해줘")
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded
    assert result.evidence[0].data["event"]["end_at"] - result.evidence[0].data["event"]["start_at"] == 5400
    assert "기본" not in result.raw_output
    utc = plugin.execute_tool("local_calendar_create_event", {"title": "UTC 회의", "start": "2026-10-25T08:00:00Z"})
    assert utc.succeeded and "17:00" in utc.raw_output


@pytest.mark.parametrize("changes", [
    {"title": " \n\t"}, {"title": 1}, {"start": "2026-10-25"},
    {"start": "2026-02-30T17:00"}, {"end": "2026-10-25T16:00"},
    {"all_day": "true"}, {"location": None}, {"description": []},
    {"all_day": True},
])
def test_invalid_tool_inputs_do_not_save(calendar, changes):
    plugin, store, _, _ = calendar
    result = plugin.execute_tool("local_calendar_create_event", {"title": "회의", "start": "2026-10-25T17:00", **changes})
    assert result.status == ToolRunStatus.FAILED
    assert store.list_events(date(2026, 1, 1), date(2027, 1, 1)) == []


def test_registry_rejects_extra_fields_and_wrong_types(calendar):
    _, store, registry, _ = calendar
    for changes in ({"all_day": "false"}, {"alarm": True}, {"title": "\t "}):
        result = registry.execute_tool("local_calendar_create_event", {"title": "회의", "start": "2026-10-25T17:00", **changes})
        assert not result.succeeded
    assert store.list_events(date(2026, 1, 1), date(2027, 1, 1)) == []


@pytest.mark.parametrize("failure", ["missing", "different", "reminder", "exception"])
def test_saved_but_unverified_event_is_not_reported_successfully(calendar, monkeypatch, failure):
    plugin, store, _, _ = calendar
    read = store.get_event
    def broken_read(event_id):
        if failure == "exception":
            raise OSError("readback unavailable")
        if failure == "reminder":
            return {**read(event_id), "reminder_minutes": None}
        return None if failure == "missing" else {**read(event_id), "title": "다른 일정"}
    monkeypatch.setattr(store, "get_event", broken_read)
    result = plugin.execute_tool("local_calendar_create_event", {"title": "회의", "start": "2026-10-25T17:00"})
    assert result.status == ToolRunStatus.UNVERIFIED and not result.succeeded
    assert "중복 등록" in result.raw_output
    assert len(LocalCalendarStore(store.db_path).list_events(date(2026, 10, 25), date(2026, 10, 26))) == 1


def test_database_write_failure_has_no_success_claim(calendar, monkeypatch):
    plugin, store, _, _ = calendar
    def fail(*args, **kwargs):
        raise OSError("database unavailable")
    monkeypatch.setattr(store, "add_event", fail)
    result = plugin.execute_tool("local_calendar_create_event", {"title": "회의", "start": "2026-10-25T17:00"})
    assert result.status == ToolRunStatus.FAILED and "등록 실패" in result.raw_output
    assert not result.artifacts


def _saved_delete_event(store, title="치과 예약", start="2026-10-25T17:00:00+09:00"):
    begin = datetime.fromisoformat(start)
    return store.add_event(title, begin, begin + timedelta(hours=1))


@pytest.mark.parametrize("text,expected", [
    ("10월 25일 치과 예약 일정 삭제해줘", {"title": "치과 예약", "date": "2026-10-25"}),
    ("2026-10-25 오후 5시 치과 예약 취소해줘", {"title": "치과 예약", "date": "2026-10-25", "time": "17:00"}),
    ("내 캘린더에서 25일 17:00 치과 예약 일정 지워줘", {"title": "치과 예약", "date": "2026-10-25", "time": "17:00"}),
    ('2026-10-25T08:00:00Z "치과 예약" 일정 삭제해줘', {"title": "치과 예약", "date": "2026-10-25", "time": "17:00"}),
    ('2026-10-25 "17시 예약" 일정 삭제해줘', {"title": "17시 예약", "date": "2026-10-25"}),
])
def test_explicit_delete_uses_numeric_selectors_from_user_input(calendar, text, expected):
    plugin, store, registry, _ = calendar
    saved = _saved_delete_event(store, expected["title"])
    decision = SemanticRequestInterpreter(None, registry).interpret(text)
    assert decision.source == "local_calendar_contract" and decision.operation == "change"
    resolution = decision.to_resolution(registry)
    assert resolution.ready and resolution.intent_name == "local_calendar.delete_event"
    assert resolution.slots == expected
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded and "삭제했습니다" in result.raw_output
    assert store.get_event(saved["id"]) is None
    assert result.evidence[0].kind == "local_calendar_deleted_event"
    assert result.evidence[0].data == {"db_path": str(store.db_path), "event": saved}


def test_duplicate_delete_requires_time_and_followup_resolves_same_title(calendar):
    plugin, store, registry, _ = calendar
    first = _saved_delete_event(store)
    second = _saved_delete_event(store, start="2026-10-25T18:00:00+09:00")
    interpreter = SemanticRequestInterpreter(None, registry)
    decision = interpreter.interpret("10월 25일 치과 예약 일정 삭제해줘")
    assert decision.needs_clarification and "17:00" in decision.clarification_question and "18:00" in decision.clarification_question
    assert not decision.to_resolution(registry).ready
    assert store.get_event(first["id"]) and store.get_event(second["id"])
    reply = interpreter.interpret("오후 5시", pending={"intent_name": decision.intent_name, "slots": decision.slots})
    assert reply.relation == "continue" and reply.to_resolution(registry).ready
    result = plugin.execute_tool(reply.tool_names[0], reply.slots)
    assert result.succeeded and store.get_event(first["id"]) is None
    assert store.get_event(second["id"]) == second


def test_delete_missing_target_and_identical_duplicates_never_mutate(calendar):
    plugin, store, registry, _ = calendar
    first = _saved_delete_event(store)
    second = _saved_delete_event(store)
    for slots in ({"title": "다른 제목", "date": "2026-10-25"}, {"title": "치과 예약", "date": "2026-10-25", "time": "17:00"}):
        result = plugin.execute_tool("local_calendar_delete_event", slots)
        assert not result.succeeded and "삭제하지 않았습니다" in result.raw_output
    decision = SemanticRequestInterpreter(None, registry).interpret("10월 25일 치과 예약 일정 삭제해줘")
    assert decision.needs_clarification and "캘린더에서" in decision.clarification_question
    assert store.get_event(first["id"]) and store.get_event(second["id"])


@pytest.mark.parametrize("text", [
    "2026-10-25 5시 치과 예약 일정 삭제해줘",
    "2026-02-30 치과 예약 일정 삭제해줘",
    "2026-10-25 17:00부터 18:00까지 치과 예약 일정 삭제해줘",
    "2026-10-25 치과 예약, 2026-10-26 회의 일정 삭제해줘",
])
def test_invalid_or_multiple_delete_targets_ask_before_mutation(calendar, text):
    _, store, registry, _ = calendar
    saved = _saved_delete_event(store)
    decision = SemanticRequestInterpreter(None, registry).interpret(text)
    assert decision.needs_clarification and not decision.to_resolution(registry).ready
    assert store.get_event(saved["id"]) == saved


@pytest.mark.parametrize("change", [
    {"event_id": "model-guessed-id"}, {"title": "\t"}, {"date": "20261025"},
    {"date": "2026-02-30"}, {"date": "9999-12-31"}, {"time": "25:00"}, {"time": None},
])
def test_delete_schema_or_runtime_failure_leaves_rows_intact(calendar, change):
    _, store, registry, _ = calendar
    saved = _saved_delete_event(store)
    result = registry.execute_tool("local_calendar_delete_event", {"title": "치과 예약", "date": "2026-10-25", **change})
    assert not result.succeeded and store.get_event(saved["id"]) == saved


@pytest.mark.parametrize("text", [
    "내일 치과 예약 일정 삭제하지 마", "내일 치과 예약 일정 삭제하면 어떻게 돼?",
    "네이버 캘린더에서 내일 치과 예약 일정 삭제해줘", '"내일 치과 예약 일정 삭제해줘"라는 말의 뜻이 뭐야?',
])
def test_delete_scope_guards_and_external_calendars_do_not_select_local(calendar, text):
    _, _, registry, _ = calendar
    decision = SemanticRequestInterpreter(None, registry).interpret(text)
    assert "local_calendar_delete_event" not in decision.tool_names


def test_delete_not_available_for_model_to_invent_target(calendar):
    class InventedDelete:
        def chat(self, messages, **kwargs):
            return '{"relation":"new","operation":"change","tool_names":["local_calendar_delete_event"],"slots":{"title":"치과 예약","date":"2026-10-25"},"confidence":1,"needs_clarification":false,"clarification_question":"","control_scope":"current"}'
    _, store, registry, _ = calendar
    saved = _saved_delete_event(store)
    decision = SemanticRequestInterpreter(InventedDelete(), registry).interpret("오래된 내용을 정리해줘")
    assert not decision.grounded and not decision.to_resolution(registry).ready
    assert store.get_event(saved["id"]) == saved


def test_delete_saved_but_readback_failed_is_unverified(calendar, monkeypatch):
    plugin, store, _, _ = calendar
    saved = _saved_delete_event(store)
    monkeypatch.setattr(store, "get_event", lambda _id: saved)
    result = plugin.execute_tool("local_calendar_delete_event", {"title": "치과 예약", "date": "2026-10-25"})
    assert result.status == ToolRunStatus.UNVERIFIED and "이미 삭제되었을 수" in result.raw_output
    assert LocalCalendarStore(store.db_path).get_event(saved["id"]) is None


@pytest.mark.parametrize("minutes", [0, 30, None])
def test_tool_saves_and_reports_custom_reminder(calendar, minutes):
    plugin, store, _, _ = calendar
    result = plugin.execute_tool("local_calendar_create_event", {
        "title": "회의", "start": "2026-10-25T17:00", "reminder_minutes": minutes,
    })
    assert result.succeeded
    event = result.evidence[0].data["event"]
    assert store.get_event(event["id"])["reminder_minutes"] == minutes
    label = "알림: 끔" if minutes is None else "알림: 시작 시각" if minutes == 0 else "알림: 시작 30분 전"
    assert label in result.raw_output


@pytest.mark.parametrize("minutes", [True, -1, 10081, "60", 1.5])
def test_tool_rejects_invalid_reminder_without_saving(calendar, minutes):
    plugin, store, _, _ = calendar
    result = plugin.execute_tool("local_calendar_create_event", {
        "title": "회의", "start": "2026-10-25T17:00", "reminder_minutes": minutes,
    })
    assert result.status == ToolRunStatus.FAILED
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == []


def test_router_keeps_external_calendar_alarm_negation_and_quoted_discussion_separate(calendar):
    _, _, _, router = calendar
    assert router.resolve("5분 뒤에 알람 맞춰줘").tool_name == "set_alarm"
    assert router.resolve("바탕화면에 .ics 캘린더 파일 만들어줘").tool_name == "calendar_create_event"
    assert router.resolve("네이버 캘린더에 내일 오후 3시 회의 일정 등록해줘").tool_name != "local_calendar_create_event"
    assert not router.resolve("내일 오후 3시 회의 일정을 등록하지 마").ready
    assert not router.resolve("'내일 오후 3시 회의 일정 등록해줘'라는 문장을 설명해줘").ready


@pytest.mark.parametrize("reply,expected", [
    ("내일 오후 3시", True), ("2026-10-25T17:00:00+09:00", True),
    ("오후 5시", True), ("내일 종일", True), ("제목은 팀 회의", True),
    ("응", False), ("고마워", False), ("내일 오후 3시에 메일 보내줘", False),
])
def test_slot_reply_grammar_is_bounded(reply, expected):
    assert LocalCalendarPlugin.is_slot_reply(reply, {}) is expected


def test_semantic_registration_and_date_title_followups_do_not_need_a_model(calendar):
    _, _, registry, _ = calendar
    interpreter = SemanticRequestInterpreter(None, registry)
    first = interpreter.interpret("치과 예약 일정 등록해줘")
    assert first.source == "local_calendar_contract" and first.needs_clarification
    follow = interpreter.interpret("내일 오후 3시", pending={"intent_name": first.intent_name, "slots": first.slots, "question": first.clarification_question})
    assert follow.relation == "continue" and follow.to_resolution(registry).ready
    assert follow.slots["title"] == "치과 예약"
    unnamed = interpreter.interpret("내일 오후 3시 일정 등록해줘")
    assert unnamed.needs_clarification
    titled = interpreter.interpret("제목은 팀 회의", pending={"intent_name": unnamed.intent_name, "slots": unnamed.slots, "question": unnamed.clarification_question})
    assert titled.to_resolution(registry).ready and titled.slots["title"] == "팀 회의"


def test_semantic_new_request_does_not_inherit_previous_event(calendar):
    _, _, registry, _ = calendar
    decision = SemanticRequestInterpreter(None, registry).interpret("내일 오후 3시 회의 일정 등록해줘", pending={
        "intent_name": "local_calendar.create_event", "slots": {"title": "치과", "start": "2026-10-25T17:00", "location": "기존 장소", "end": "2026-10-25T18:00"},
    })
    assert decision.source == "local_calendar_contract" and decision.relation == "new"
    assert decision.slots == {"title": "회의", "start": "2026-10-12T15:00:00+09:00"}


@pytest.mark.parametrize("text", [
    "내일 오후 3시 회의 일정 등록하지 마",
    "회의가 확정되면 내일 오후 3시 회의 일정 등록해줘",
    "네이버 캘린더에 내일 오후 3시 회의 일정 등록해줘",
    "5분 뒤 알람 맞춰줘",
    "치과 예약했어", "일정 등록은 어떻게 해?",
    "'내일 오후 3시 회의 일정 등록해줘'라는 문장을 설명해줘",
])
def test_semantic_excluded_requests_do_not_use_registration_fast_path(calendar, text):
    _, _, registry, _ = calendar
    decision = SemanticRequestInterpreter(None, registry).interpret(text)
    assert decision.source != "local_calendar_contract"
    if text == "5분 뒤 알람 맞춰줘":
        assert decision.to_resolution(registry).tool_name == "set_alarm"
    else:
        assert not decision.to_resolution(registry).ready


def test_semantic_respects_allowed_tools(calendar):
    _, _, registry, _ = calendar
    decision = SemanticRequestInterpreter(None, registry).interpret("내일 오후 3시 회의 일정 등록해줘", allowed_tools=["set_alarm"])
    assert decision.source != "local_calendar_contract" and not decision.to_resolution(registry).ready


@pytest.mark.parametrize("text,start,end", [
    ("내일 15:00-17:00 회의 일정 등록해줘", "2026-10-12T15:00:00+09:00", "2026-10-12T17:00:00+09:00"),
    ("2026-10-25 15:00-17:00 회의 일정 등록해줘", "2026-10-25T15:00:00", "2026-10-25T17:00:00+09:00"),
    ("내일 오후 3:00~오후 5:00 회의 일정 등록해줘", "2026-10-12T15:00:00+09:00", "2026-10-12T17:00:00+09:00"),
    ("내일 오후 3시부터 모레 오후 5시까지 회의 일정 등록해줘", "2026-10-12T15:00:00+09:00", "2026-10-13T17:00:00+09:00"),
    ("2026-10-25T15:00부터 2026-10-25T17:00까지 회의 일정 등록해줘", "2026-10-25T15:00:00", "2026-10-25T17:00:00"),
])
def test_single_event_explicit_ranges_preserve_both_endpoints(calendar, text, start, end):
    plugin, _, registry, router = calendar
    resolution = router.resolve(text)
    assert resolution.ready and resolution.slots == {"title": "회의", "start": start, "end": end}
    decision = SemanticRequestInterpreter(None, registry).interpret(text)
    assert decision.to_resolution(registry).ready and decision.slots == resolution.slots
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded and "기본" not in result.raw_output
    actual = result.evidence[0].data["event"]
    assert actual["end_at"] == int(datetime.fromisoformat(end).replace(tzinfo=CALENDAR_TIMEZONE).timestamp())


@pytest.mark.parametrize("text", [
    "내일 오후 5시 치과 예약, 모레 오후 4시 미용실 예약 일정 등록해줘",
    "내일 15:00 치과 예약, 17:00 미용실 예약 일정 등록해줘",
    "내일 종일 휴가, 모레 종일 출장 일정 등록해줘",
    "내일 15:00 모레 17:00 회의 일정 등록해줘",
    "내일 15:00부터 16:00까지 회의, 17:00 미용실 예약 일정 등록해줘",
])
def test_multiple_events_are_asked_one_by_one_and_never_merged(calendar, text):
    _, store, registry, router = calendar
    resolution = router.resolve(text)
    assert resolution.matched and not resolution.ready and resolution.slots == {}
    assert "한 번에 하나씩" in resolution.question
    decision = SemanticRequestInterpreter(None, registry).interpret(text)
    assert decision.source == "local_calendar_contract" and decision.needs_clarification
    assert not decision.to_resolution(registry).ready and decision.slots == {}
    assert store.list_events(date(2026, 1, 1), date(2027, 1, 1)) == []


@pytest.mark.parametrize("start", ["2026-10-25T15:00:00-05:00", "2026-10-25T15:00:00+09:00", "2026-10-25 15:00:00+09:00"])
def test_explicit_iso_timezone_offset_is_preserved(calendar, start):
    resolution = calendar[3].resolve(start + " 회의 일정 등록해줘")
    assert resolution.ready and resolution.slots == {"title": "회의", "start": start.replace(" ", "T")}


@pytest.mark.parametrize("setting,minutes", [
    ("30분 전에 알림 설정해서", 30), ("1시간 전 알림 설정해서", 60),
    ("2시간 전에 알림을 설정하고", 120), ("0분 전 알림", 0),
    ("알림 없이", None), ("알림 끄고", None), ("알림 끔", None), ("알림 없음", None),
])
def test_chat_reminder_setting_is_preserved_and_removed_from_title(calendar, setting, minutes):
    plugin, store, registry, router = calendar
    request = f"내일 오후 3시 회의 일정 {setting} 등록해줘"
    decision = SemanticRequestInterpreter(None, registry).interpret(request)
    resolution = router.resolve(request)
    assert decision.source == "local_calendar_contract" and decision.slots == resolution.slots
    assert resolution.ready and resolution.slots["title"] == "회의"
    assert resolution.slots["reminder_minutes"] == minutes
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded and store.get_event(result.artifacts[0].uri)["reminder_minutes"] == minutes


@pytest.mark.parametrize("title", ["30분 전 알림 시스템 검토", "알림 없이 업무 진행 검토", "알림 설정해서 비용 검토"])
@pytest.mark.parametrize("quoted", [True, False])
def test_title_payload_reminder_words_are_not_instructions(calendar, title, quoted):
    request = f'내일 오후 3시 "{title}" 일정 등록해줘' if quoted else f"내일 오후 3시 일정 등록해줘, 제목은 {title}"
    resolution = calendar[3].resolve(request)
    assert resolution.ready and resolution.slots["title"] == title
    assert "reminder_minutes" not in resolution.slots


@pytest.mark.parametrize("setting", [
    "10081분 전 알림", "169시간 전 알림", "-1분 전 알림", "0.5시간 전 알림",
    "약 30분 전 알림", "30분 정도 전 알림", "30~60분 전 알림",
    "30분 전 알림 60분 전 알림", "알림 설정해서", "두 시간 전 알림",
])
def test_ambiguous_or_invalid_reminder_is_never_silently_defaulted(calendar, setting):
    _, store, registry, router = calendar
    resolution = router.resolve(f"내일 오후 3시 회의 일정 {setting} 등록해줘")
    assert resolution.ready and "reminder_minutes" in resolution.slots
    result = registry.execute_tool(resolution.tool_name, resolution.slots)
    assert not result.succeeded
    assert store.list_events(date(2026, 10, 12), date(2026, 10, 13)) == []


def test_reminder_setting_survives_date_followup_and_can_be_corrected(calendar):
    _, _, registry, router = calendar
    first = router.resolve("회의 일정 30분 전 알림 설정해서 등록해줘")
    assert first.slots == {"title": "회의", "reminder_minutes": 30} and first.question
    followup = SemanticRequestInterpreter(None, registry).interpret("내일 오후 3시", pending={
        "intent_name": first.intent_name, "slots": first.slots, "question": first.question,
    })
    assert followup.to_resolution(registry).ready and followup.slots["reminder_minutes"] == 30
    correction = SemanticRequestInterpreter(None, registry).interpret("알림 없이", pending={
        "intent_name": first.intent_name, "slots": followup.slots, "question": "알림 시간을 확인해주세요.",
    })
    assert correction.to_resolution(registry).ready and correction.slots["reminder_minutes"] is None
