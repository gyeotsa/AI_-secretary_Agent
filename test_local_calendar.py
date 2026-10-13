from contextlib import closing
from datetime import date, datetime
import sqlite3

import pytest

from config import Config
from core.local_calendar import CALENDAR_TIMEZONE, LocalCalendarStore


def _epoch(value):
    return int(datetime.fromisoformat(value).timestamp())


@pytest.fixture
def store(tmp_path):
    return LocalCalendarStore(tmp_path / "calendar.db")


@pytest.fixture
def seed(store):
    def insert(event_id, start, end, **fields):
        values = {
            "id": event_id, "title": fields.pop("title", event_id),
            "start_at": _epoch(start), "end_at": _epoch(end), **fields,
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with closing(sqlite3.connect(store.db_path)) as conn, conn:
            conn.execute(f"INSERT INTO calendar_events ({columns}) VALUES ({placeholders})",
                         tuple(values.values()))
    return insert


def test_default_path_and_empty_calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "nested" / "assistant.db"))
    store = LocalCalendarStore()

    assert store.db_path == tmp_path / "nested" / "calendar.db"
    assert store.db_path.is_absolute()
    assert store.list_events(date(2026, 10, 1), date(2026, 11, 1)) == []


def test_events_persist_on_reopen(store, seed):
    seed("dentist", "2026-10-25T17:00:00+09:00", "2026-10-25T18:00:00+09:00",
         title="치과", location="치과 의원", description="예약")

    reopened = LocalCalendarStore(store.db_path)
    assert reopened.list_events(date(2026, 10, 25), date(2026, 10, 26)) == [{
        "id": "dentist", "title": "치과",
        "start_at": _epoch("2026-10-25T08:00:00+00:00"),
        "end_at": _epoch("2026-10-25T09:00:00+00:00"),
        "all_day": 0, "location": "치과 의원", "description": "예약", "reminder_minutes": 60,
    }]


def test_overlap_boundaries_multiday_and_stable_order(store, seed):
    seed("ends_at_start", "2026-10-24T22:00:00+09:00", "2026-10-25T00:00:00+09:00")
    seed("multiday", "2026-10-24T00:00:00+09:00", "2026-10-27T00:00:00+09:00", all_day=1)
    seed("b", "2026-10-25T17:00:00+09:00", "2026-10-25T18:00:00+09:00")
    seed("a", "2026-10-25T17:00:00+09:00", "2026-10-26T00:00:00+09:00")
    seed("starts_at_end", "2026-10-26T00:00:00+09:00", "2026-10-26T01:00:00+09:00")

    events = store.list_events(date(2026, 10, 25), date(2026, 10, 26))
    assert [event["id"] for event in events] == ["multiday", "a", "b"]
    assert events[0]["all_day"] == 1
    assert [event["id"] for event in store.list_events(date(2026, 10, 26), date(2026, 10, 27))] == [
        "multiday", "starts_at_end",
    ]


def test_utc_timestamps_are_queried_by_korean_calendar_dates(store, seed):
    seed("crosses_midnight", "2026-10-09T14:30:00+00:00", "2026-10-09T15:30:00+00:00")
    seed("korean_midnight", "2026-10-09T15:00:00+00:00", "2026-10-09T16:00:00+00:00")

    assert CALENDAR_TIMEZONE.utcoffset(None).total_seconds() == 9 * 3600
    assert [event["id"] for event in store.list_events(date(2026, 10, 9), date(2026, 10, 10))] == [
        "crosses_midnight",
    ]
    assert [event["id"] for event in store.list_events(date(2026, 10, 10), date(2026, 10, 11))] == [
        "crosses_midnight", "korean_midnight",
    ]


@pytest.mark.parametrize("invalid", [
    {"id": None}, {"title": None}, {"title": " \t\r\n"},
    {"start_at": None}, {"start_at": "tomorrow"},
    {"end_at": 100}, {"end_at": 99}, {"end_at": "tomorrow"}, {"all_day": 2},
])
def test_schema_rejects_invalid_events(store, invalid):
    event = {"id": "invalid", "title": "일정", "start_at": 100, "end_at": 200, "all_day": 0}
    event.update(invalid)
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("""
                INSERT INTO calendar_events (id, title, start_at, end_at, all_day)
                VALUES (:id, :title, :start_at, :end_at, :all_day)
            """, event)
    assert store.list_events(date(1970, 1, 1), date(1970, 1, 2)) == []


@pytest.mark.parametrize("end", [date(2026, 10, 10), date(2026, 10, 9)])
def test_rejects_empty_or_reversed_range(store, end):
    with pytest.raises(ValueError, match="조회 종료일"):
        store.list_events(date(2026, 10, 10), end)


def test_rejects_datetime_range(store):
    with pytest.raises(TypeError, match="date"):
        store.list_events(datetime(2026, 10, 10), date(2026, 10, 11))


def test_add_event_is_saved_and_read_back_on_reopen(store):
    saved = store.add_event("  치과 예약  ", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18),
                            location="치과 의원", description="예약 메모")

    assert saved == {
        "id": saved["id"], "title": "치과 예약",
        "start_at": _epoch("2026-10-25T08:00:00+00:00"),
        "end_at": _epoch("2026-10-25T09:00:00+00:00"),
        "all_day": 0, "location": "치과 의원", "description": "예약 메모", "reminder_minutes": 60,
    }
    assert LocalCalendarStore(store.db_path).get_event(saved["id"]) == saved
    assert store.get_event("missing") is None
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == [saved]


def test_add_aware_utc_event_and_multiday_all_day(store):
    timed = store.add_event("밤 일정", datetime.fromisoformat("2026-10-24T14:30:00+00:00"),
                            datetime.fromisoformat("2026-10-24T16:00:00+00:00"))
    full_day = store.add_event("출장", datetime.fromisoformat("2026-10-24T15:00:00+00:00"),
                               datetime.fromisoformat("2026-10-26T15:00:00+00:00"), all_day=True)

    assert [event["id"] for event in store.list_events(date(2026, 10, 24), date(2026, 10, 25))] == [timed["id"]]
    assert [event["id"] for event in store.list_events(date(2026, 10, 25), date(2026, 10, 26))] == [timed["id"], full_day["id"]]
    assert full_day["all_day"] == 1
    assert store.list_events(date(2026, 10, 27), date(2026, 10, 28)) == []


def test_each_add_request_gets_a_new_id(store):
    first = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    second = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))

    assert first["id"] != second["id"]
    assert len(store.list_events(date(2026, 10, 25), date(2026, 10, 26))) == 2


@pytest.mark.parametrize("invalid, error", [
    ({"title": " \t\n\u3000"}, ValueError), ({"title": None}, TypeError),
    ({"location": None}, TypeError), ({"description": 42}, TypeError),
    ({"start": date(2026, 10, 25)}, TypeError), ({"end": "2026-10-25T18:00"}, TypeError),
    ({"all_day": 1}, TypeError), ({"all_day": "false"}, TypeError),
    ({"end": datetime(2026, 10, 25, 17)}, ValueError),
    ({"end": datetime(2026, 10, 25, 16)}, ValueError),
    ({"start": datetime(2026, 10, 25, 17, microsecond=100),
      "end": datetime(2026, 10, 25, 17, microsecond=200)}, ValueError),
    ({"all_day": True}, ValueError),
    ({"start": datetime(2026, 10, 25), "end": datetime(2026, 10, 26, microsecond=1),
      "all_day": True}, ValueError),
    ({"start": datetime.min, "end": datetime(1, 1, 2)}, ValueError),
    ({"start": datetime.fromisoformat("9999-12-31T22:00:00+00:00"),
      "end": datetime.fromisoformat("9999-12-31T23:00:00+00:00")}, ValueError),
])
def test_add_rejects_invalid_input_without_saving(store, invalid, error):
    values = {"title": "회의", "start": datetime(2026, 10, 25, 17), "end": datetime(2026, 10, 25, 18)}
    values.update(invalid)
    with pytest.raises(error):
        store.add_event(**values)
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == []


def test_save_failure_rolls_back_without_an_event(store):
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute("""
            CREATE TRIGGER reject_calendar_insert AFTER INSERT ON calendar_events
            BEGIN SELECT RAISE(ABORT, 'synthetic save failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic save failure"):
        store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    assert LocalCalendarStore(store.db_path).list_events(date(2026, 10, 25), date(2026, 10, 26)) == []


def test_missing_readback_rolls_back_insert(store):
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute("""
            CREATE TRIGGER remove_calendar_insert AFTER INSERT ON calendar_events
            BEGIN DELETE FROM calendar_events WHERE id = NEW.id; END
        """)
    with pytest.raises(sqlite3.DatabaseError, match="확인할 수 없습니다"):
        store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == []


def test_migration_preserves_existing_event_and_adds_default_reminder(tmp_path):
    path = tmp_path / "old-calendar.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("""
            CREATE TABLE calendar_events (
                id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, start_at INTEGER NOT NULL,
                end_at INTEGER NOT NULL, all_day INTEGER DEFAULT 0,
                location TEXT DEFAULT '', description TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            INSERT INTO calendar_events (id, title, start_at, end_at, location, description)
            VALUES ('old-event', '기존 일정', ?, ?, '기존 장소', '기존 메모')
        """, (_epoch("2026-10-25T17:00:00+09:00"), _epoch("2026-10-25T18:00:00+09:00")))

    store = LocalCalendarStore(path)
    saved = store.get_event("old-event")
    assert saved["title"] == "기존 일정"
    assert saved["location"] == "기존 장소"
    assert saved["description"] == "기존 메모"
    assert saved["reminder_minutes"] == 60
    assert "reminder_notified_at" not in saved
    assert LocalCalendarStore(path).get_event("old-event") == saved
    assert store.take_due_reminders(datetime(2026, 10, 25, 16)) == [saved]
    assert LocalCalendarStore(path).take_due_reminders(datetime(2026, 10, 25, 16, 1)) == []


def test_failed_migration_rolls_back_columns_and_preserves_existing_data(tmp_path, monkeypatch):
    path = tmp_path / "old-calendar.db"
    original_connect = sqlite3.connect
    with closing(original_connect(path)) as conn, conn:
        conn.execute("""CREATE TABLE calendar_events (
            id TEXT PRIMARY KEY, title TEXT, start_at INTEGER, end_at INTEGER,
            all_day INTEGER, location TEXT, description TEXT)""")
        conn.execute("INSERT INTO calendar_events VALUES ('old', '기존 일정', 100, 200, 0, '', '')")

    class FailingMigration(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            if "ADD COLUMN reminder_notified_at" in statement:
                raise sqlite3.OperationalError("synthetic migration failure")
            return super().execute(statement, parameters)

    def connect_for_migration(*args, **kwargs):
        return original_connect(*args, factory=FailingMigration, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect_for_migration)
    with pytest.raises(sqlite3.OperationalError, match="synthetic migration failure"):
        LocalCalendarStore(path)
    with closing(original_connect(path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)")}
        assert "reminder_minutes" not in columns
        assert "reminder_notified_at" not in columns
        assert conn.execute("SELECT * FROM calendar_events").fetchall() == [
            ("old", "기존 일정", 100, 200, 0, "", ""),
        ]


@pytest.mark.parametrize("minutes", [None, 0, 60, 10080])
def test_add_reminder_setting_is_saved_and_read_back(store, minutes):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18),
                            reminder_minutes=minutes)
    assert event["reminder_minutes"] == minutes
    assert "reminder_notified_at" not in event
    assert store.get_event(event["id"]) == event
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == [event]


@pytest.mark.parametrize("minutes, error", [
    (True, TypeError), (False, TypeError), (1.5, TypeError), ("60", TypeError),
    (-1, ValueError), (10081, ValueError),
])
def test_add_rejects_invalid_reminder_settings_without_saving(store, minutes, error):
    with pytest.raises(error):
        store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18),
                        reminder_minutes=minutes)
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == []


def test_reminder_is_not_early_and_is_claimed_once_across_restarts(store):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    assert store.take_due_reminders(datetime(2026, 10, 25, 15, 59, 59)) == []
    assert store.take_due_reminders(datetime(2026, 10, 25, 16)) == [event]
    assert store.take_due_reminders(datetime(2026, 10, 25, 16, 1)) == []
    assert LocalCalendarStore(store.db_path).take_due_reminders(datetime(2026, 10, 25, 16, 2)) == []
    with closing(sqlite3.connect(store.db_path)) as conn:
        marked_at = conn.execute("SELECT reminder_notified_at FROM calendar_events WHERE id = ?",
                                 (event["id"],)).fetchone()[0]
    assert marked_at == _epoch("2026-10-25T16:00:00+09:00")


def test_late_polling_zero_minutes_and_expired_end(store):
    disabled = store.add_event("알림 끔", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18),
                               reminder_minutes=None)
    expired = store.add_event("종료된 일정", datetime(2026, 10, 25, 15), datetime(2026, 10, 25, 16))
    expired_at_end = store.add_event("정각 종료", datetime(2026, 10, 25, 16), datetime(2026, 10, 25, 17))
    event = store.add_event("정각 알림", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18),
                            reminder_minutes=0)
    assert store.take_due_reminders(datetime(2026, 10, 25, 17, 0, 2)) == [event]
    assert store.take_due_reminders(datetime(2026, 10, 25, 17, 1)) == []
    with closing(sqlite3.connect(store.db_path)) as conn:
        remaining = conn.execute("SELECT id FROM calendar_events WHERE reminder_notified_at IS NULL").fetchall()
    assert {row[0] for row in remaining} == {disabled["id"], expired["id"], expired_at_end["id"]}


def test_reminder_due_after_restart_and_aware_clock(store):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    reopened = LocalCalendarStore(store.db_path)
    assert reopened.take_due_reminders(datetime.fromisoformat("2026-10-25T07:15:00+00:00")) == [event]


def test_limit_leaves_other_due_events_for_next_poll(store, seed):
    seed("b", "2026-10-25T17:00:00+09:00", "2026-10-25T18:00:00+09:00")
    seed("a", "2026-10-25T17:00:00+09:00", "2026-10-25T18:00:00+09:00")
    seed("c", "2026-10-25T17:30:00+09:00", "2026-10-25T18:00:00+09:00")
    now = datetime(2026, 10, 25, 16, 30)
    assert [row["id"] for row in store.take_due_reminders(now)] == ["a"]
    assert [row["id"] for row in store.take_due_reminders(now, limit=2)] == ["b", "c"]
    assert store.take_due_reminders(now) == []


def test_concurrent_stores_claim_each_event_only_once(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    second_store = LocalCalendarStore(store.db_path)
    barrier = Barrier(2)
    def claim(instance):
        barrier.wait(timeout=5)
        return instance.take_due_reminders(datetime(2026, 10, 25, 16))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, (store, second_store)))
    assert sorted(len(rows) for rows in results) == [0, 1]
    assert [row for rows in results for row in rows] == [event]


def test_failed_reminder_update_rolls_back_all_claims(store):
    first = store.add_event("첫 일정", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    second = store.add_event("둘째 일정", datetime(2026, 10, 25, 17, 30), datetime(2026, 10, 25, 18))
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute("""
            CREATE TRIGGER reject_reminder_update BEFORE UPDATE OF reminder_notified_at ON calendar_events
            WHEN NEW.title = '둘째 일정' BEGIN SELECT RAISE(ABORT, 'synthetic reminder failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic reminder failure"):
        store.take_due_reminders(datetime(2026, 10, 25, 16, 30), limit=2)
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        assert conn.execute("SELECT count(*) FROM calendar_events WHERE reminder_notified_at IS NOT NULL").fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_reminder_update")
    assert store.take_due_reminders(datetime(2026, 10, 25, 16, 30), limit=2) == [first, second]


@pytest.mark.parametrize("now, limit, error", [
    (date(2026, 10, 25), 1, TypeError), (datetime(2026, 10, 25), True, TypeError),
    (datetime(2026, 10, 25), 1.5, TypeError), (datetime(2026, 10, 25), 0, ValueError),
    (datetime(2026, 10, 25), -1, ValueError), (datetime.min, 1, ValueError),
])
def test_rejects_invalid_reminder_clock_or_limit(store, now, limit, error):
    with pytest.raises(error):
        store.take_due_reminders(now, limit=limit)


@pytest.mark.parametrize("column, value", [
    ("reminder_minutes", -1), ("reminder_minutes", 10081), ("reminder_minutes", "not minutes"),
    ("reminder_notified_at", "not a timestamp"),
])
def test_reminder_columns_reject_invalid_storage(store, column, value):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE calendar_events SET {column} = ? WHERE id = ?", (value, event["id"]))


def test_delete_exact_id_preserves_duplicate_and_persists_on_reopen(store):
    removed = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18),
                              location="회의실", description="삭제 대상 메모", reminder_minutes=30)
    retained = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))

    assert store.delete_event(removed["id"]) == removed
    reopened = LocalCalendarStore(store.db_path)
    assert reopened.get_event(removed["id"]) is None
    assert reopened.list_events(date(2026, 10, 25), date(2026, 10, 26)) == [retained]
    assert reopened.take_due_reminders(datetime(2026, 10, 25, 16, 30)) == [retained]
    assert reopened.delete_event(removed["id"]) is None


def test_delete_missing_id_leaves_calendar_unchanged(store):
    retained = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    assert store.delete_event("missing") is None
    assert store.get_event(retained["id"]) == retained


@pytest.mark.parametrize("event_id, error", [(None, TypeError), (1, TypeError),
                                             (False, TypeError), ("", ValueError), (" \t\n", ValueError)])
def test_delete_rejects_invalid_id_without_changing_events(store, event_id, error):
    retained = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    with pytest.raises(error):
        store.delete_event(event_id)
    assert store.get_event(retained["id"]) == retained


def test_delete_treats_sql_syntax_as_literal_id(store, seed):
    literal_id = "' OR 1=1 --"
    seed(literal_id, "2026-10-25T17:00:00+09:00", "2026-10-25T18:00:00+09:00")
    retained = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))

    assert store.delete_event(literal_id)["id"] == literal_id
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == [retained]


def test_failed_delete_rolls_back_and_preserves_event(store):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute("""
            CREATE TRIGGER reject_calendar_delete AFTER DELETE ON calendar_events
            BEGIN SELECT RAISE(ABORT, 'synthetic delete failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic delete failure"):
        store.delete_event(event["id"])
    assert LocalCalendarStore(store.db_path).get_event(event["id"]) == event


def test_delete_readback_failure_rolls_back_all_changes(store):
    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute("""
            CREATE TRIGGER restore_calendar_delete AFTER DELETE ON calendar_events
            BEGIN INSERT INTO calendar_events (id, title, start_at, end_at)
                VALUES (OLD.id, '변경된 제목', OLD.start_at, OLD.end_at); END
        """)
    with pytest.raises(sqlite3.DatabaseError, match="삭제 결과"):
        store.delete_event(event["id"])
    assert LocalCalendarStore(store.db_path).get_event(event["id"]) == event


def test_concurrent_deletes_return_deleted_event_only_once(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    event = store.add_event("회의", datetime(2026, 10, 25, 17), datetime(2026, 10, 25, 18))
    second_store = LocalCalendarStore(store.db_path)
    barrier = Barrier(2)
    def remove(instance):
        barrier.wait(timeout=5)
        return instance.delete_event(event["id"])
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(remove, (store, second_store)))
    assert results.count(None) == 1
    assert [row for row in results if row is not None] == [event]
    assert store.get_event(event["id"]) is None
