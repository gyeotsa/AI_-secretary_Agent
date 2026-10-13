from contextlib import closing
from datetime import datetime
import sqlite3

import pytest

from config import Config
from core.local_calendar import LocalCalendarStore
from core.tool_result import Evidence, ToolRunResult
from core.verifier import ToolVerifier


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    return LocalCalendarStore()


def result(store, event, *, db_path=None):
    return ToolRunResult.successful(
        tool_name="local_calendar_create_event", raw_output="내부 캘린더에 등록했습니다.",
        evidence=[Evidence("local_calendar_event", "저장 결과 확인", {
            "db_path": str(store.db_path if db_path is None else db_path), "event": event,
        })],
    )


def verify(request, output):
    return ToolVerifier().verify("local_calendar_create_event", request, output)


def test_verifies_actual_local_save_and_defaults(store):
    event = store.add_event("치과", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    request = {"title": " 치과 ", "start": "2026-10-25T17:00:00"}

    checked = verify(request, result(store, event))
    assert checked.success and checked.verified
    assert checked.details == {"event_id": event["id"], "method": "local_calendar_readback"}


def test_verifies_utc_time_and_optional_fields(store):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 19),
                            location="회의실", description="회의 메모")
    request = {"title": "회의", "start": "2026-10-25T08:00:00Z", "end": "2026-10-25T10:00:00+00:00",
               "location": "회의실", "description": "회의 메모", "all_day": False}

    assert verify(request, result(store, event)).success


@pytest.mark.parametrize("end", [None, "2026-10-27"])
def test_verifies_all_day_default_or_multiple_days(store, end):
    finish = datetime(2026, 10, 26) if end is None else datetime(2026, 10, 27)
    event = store.add_event("출장", datetime(2026, 10, 25), finish, all_day=True)
    request = {"title": "출장", "start": "2026-10-25", "all_day": True}
    if end is not None:
        request["end"] = end

    assert verify(request, result(store, event)).success


@pytest.mark.parametrize("field, value", [
    ("id", "missing"), ("title", "다른 제목"), ("start_at", 100), ("end_at", 200),
    ("all_day", 1), ("all_day", False), ("location", "다른 장소"), ("description", "다른 설명"),
    ("reminder_minutes", 30), ("reminder_minutes", True),
])
def test_rejects_tampered_event_evidence(store, field, value):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    event[field] = value

    assert not verify({"title": "회의", "start": "2026-10-25T17:00"}, result(store, event)).success


@pytest.mark.parametrize("invalid", [
    {"title": "다른 제목"}, {"title": " "}, {"start": "2026-10-25T16:00"},
    {"end": "2026-10-25T19:00"}, {"end": "2026-10-25T16:00"},
    {"all_day": True}, {"all_day": 1}, {"start": 42}, {"start": "tomorrow"},
    {"start": "2026-10-25"}, {"end": None}, {"location": None}, {"description": 42},
    {"location": "다른 장소"}, {"description": "다른 설명"}, {"unexpected": "value"},
    {"reminder_minutes": 30}, {"reminder_minutes": False}, {"reminder_minutes": -1},
    {"reminder_minutes": 10081}, {"reminder_minutes": "60"},
    {"reminder_minutes": 60.0},
])
def test_rejects_mismatched_or_invalid_request(store, invalid):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    request = {"title": "회의", "start": "2026-10-25T17:00"}
    request.update(invalid)

    assert not verify(request, result(store, event)).success


def test_rejects_missing_saved_event(store):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute("DELETE FROM calendar_events WHERE id = ?", (event["id"],))

    assert not verify({"title": "회의", "start": "2026-10-25T17:00"}, result(store, event)).success


@pytest.mark.parametrize("minutes", [0, 30, None])
def test_verifies_custom_or_disabled_reminder(store, minutes):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18),
                            reminder_minutes=minutes)
    request = {"title": "회의", "start": "2026-10-25T17:00", "reminder_minutes": minutes}
    assert verify(request, result(store, event)).success


def test_rejects_foreign_path_before_reading_a_store(store, tmp_path, monkeypatch):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    def unexpected_read(*args, **kwargs):
        pytest.fail("A foreign evidence path must be rejected before any database read")
    monkeypatch.setattr(LocalCalendarStore, "get_event", unexpected_read)

    assert not verify({"title": "회의", "start": "2026-10-25T17:00"},
                      result(store, event, db_path=tmp_path / "foreign.db")).success


def test_rejects_storage_failure_without_exposing_error(store, monkeypatch):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    def failed_read(*args, **kwargs):
        raise sqlite3.DatabaseError("synthetic-private-data-marker")
    monkeypatch.setattr(LocalCalendarStore, "get_event", failed_read)

    checked = verify({"title": "회의", "start": "2026-10-25T17:00"}, result(store, event))
    assert not checked.success
    assert "synthetic-private-data-marker" not in checked.message


def test_rejects_missing_evidence_and_malformed_event(store):
    request = {"title": "회의", "start": "2026-10-25T17:00"}
    assert not verify(request, "등록했습니다.").success
    assert not verify(request, result(store, {"id": 1})).success


def deletion_result(store, event, *, db_path=None):
    return ToolRunResult.successful(
        tool_name="local_calendar_delete_event", raw_output="내부 캘린더에서 삭제했습니다.",
        evidence=[Evidence("local_calendar_deleted_event", "실제 삭제 일정", {
            "db_path": str(store.db_path if db_path is None else db_path), "event": event,
        })],
    )


def verify_deletion(request, output):
    return ToolVerifier().verify("local_calendar_delete_event", request, output)


def test_verifies_deleted_event_by_title_and_korean_start_date(store):
    event = store.add_event("치과", datetime.fromisoformat("2026-10-25T08:00:00+00:00"),
                            datetime.fromisoformat("2026-10-25T09:00:00+00:00"))
    deleted = store.delete_event(event["id"])

    checked = verify_deletion({"title": " 치과 ", "date": "2026-10-25"}, deletion_result(store, deleted))
    assert checked.success and checked.verified
    assert checked.details == {"event_id": event["id"], "method": "local_calendar_delete_readback"}


def test_verifies_specific_start_minute_and_preserves_other_time(store):
    selected = store.add_event("회의", datetime(2026, 10, 25, 17, 0, 30), datetime(2026, 10, 25, 18))
    retained = store.add_event("회의", datetime(2026, 10, 25, 19), datetime(2026, 10, 25, 20))

    deleted = store.delete_event(selected["id"])
    assert verify_deletion({"title": "회의", "date": "2026-10-25", "time": "17:00"},
                           deletion_result(store, deleted)).success
    assert store.get_event(retained["id"]) == retained


def test_verifies_deleted_multiday_all_day_by_start_date(store):
    event = store.add_event("출장", datetime(2026, 10, 25), datetime(2026, 10, 27),
                            all_day=True, reminder_minutes=None)
    deleted = store.delete_event(event["id"])
    assert verify_deletion({"title": "출장", "date": "2026-10-25"}, deletion_result(store, deleted)).success
    assert not verify_deletion({"title": "출장", "date": "2026-10-26"}, deletion_result(store, deleted)).success


def test_rejects_delete_claim_while_event_still_exists(store):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    assert not verify_deletion({"title": "회의", "date": "2026-10-25"}, deletion_result(store, event)).success


def test_rejects_delete_claim_when_an_ambiguous_candidate_remains(store):
    selected = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    retained = store.add_event("회의", datetime(2026, 10, 25, 19), datetime(2026, 10, 25, 20))
    deleted = store.delete_event(selected["id"])

    assert not verify_deletion({"title": "회의", "date": "2026-10-25"}, deletion_result(store, deleted)).success
    assert store.get_event(retained["id"]) == retained


@pytest.mark.parametrize("invalid", [
    {"title": "다른 제목"}, {"title": " "}, {"title": 1}, {"date": "2026-10-26"},
    {"date": "2026-2-25"}, {"date": "2026-02-30"}, {"date": None},
    {"time": "18:00"}, {"time": "5:00"}, {"time": "24:00"}, {"time": None},
    {"event_id": "an-id"}, {"unexpected": "value"},
])
def test_rejects_invalid_or_mismatched_delete_request(store, invalid):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    deleted = store.delete_event(event["id"])
    request = {"title": "회의", "date": "2026-10-25"}
    request.update(invalid)

    assert not verify_deletion(request, deletion_result(store, deleted)).success


@pytest.mark.parametrize("field, value", [
    ("id", ""), ("id", 1), ("title", "다른 제목"), ("title", None),
    ("start_at", True), ("start_at", 100), ("end_at", 0), ("end_at", "later"),
    ("all_day", True), ("all_day", 2), ("all_day", 1), ("location", None),
    ("description", 1), ("reminder_minutes", True), ("reminder_minutes", -1),
    ("reminder_minutes", 10081), ("reminder_notified_at", 100),
])
def test_rejects_invalid_deleted_event_evidence(store, field, value):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    deleted = store.delete_event(event["id"])
    deleted[field] = value
    assert not verify_deletion({"title": "회의", "date": "2026-10-25"},
                               deletion_result(store, deleted)).success


def test_rejects_missing_deleted_snapshot(store):
    request = {"title": "회의", "date": "2026-10-25"}
    assert not verify_deletion(request, "삭제했습니다.").success
    assert not verify_deletion(request, deletion_result(store, None)).success


def test_rejects_foreign_delete_path_before_database_access(store, tmp_path, monkeypatch):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    deleted = store.delete_event(event["id"])
    def unexpected_read(*args, **kwargs):
        pytest.fail("A foreign delete evidence path must not read any store")
    monkeypatch.setattr(LocalCalendarStore, "get_event", unexpected_read)

    assert not verify_deletion({"title": "회의", "date": "2026-10-25"},
                               deletion_result(store, deleted, db_path=tmp_path / "foreign.db")).success


@pytest.mark.parametrize("method", ["get_event", "list_events"])
def test_rejects_delete_storage_read_failure_without_exposing_error(store, monkeypatch, method):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    deleted = store.delete_event(event["id"])
    def failed_read(*args, **kwargs):
        raise sqlite3.DatabaseError("synthetic-private-delete-marker")
    monkeypatch.setattr(LocalCalendarStore, method, failed_read)

    checked = verify_deletion({"title": "회의", "date": "2026-10-25"}, deletion_result(store, deleted))
    assert not checked.success
    assert "synthetic-private-delete-marker" not in checked.message
