"""아니스 내부 캘린더의 로컬 저장소와 한국 날짜 기준 조회."""
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import sqlite3

from config import Config


# shortcut: 한국 시간 고정, 해외 시간대 일정 지원 시 사용자 시간대 설정으로 전환.
CALENDAR_TIMEZONE = timezone(timedelta(hours=9), "Asia/Seoul")


class LocalCalendarStore:
    def __init__(self, db_path=None):
        path = Path(Config.DB_PATH).with_name("calendar.db") if db_path is None else Path(db_path)
        self.db_path = path.expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id TEXT PRIMARY KEY NOT NULL,
                    title TEXT NOT NULL CHECK (length(trim(title, char(9) || char(10) ||
                        char(11) || char(12) || char(13) || ' ')) > 0),
                    start_at INTEGER NOT NULL CHECK (typeof(start_at) = 'integer'),
                    end_at INTEGER NOT NULL CHECK (typeof(end_at) = 'integer' AND end_at > start_at),
                    all_day INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
                    location TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    reminder_minutes INTEGER DEFAULT 60 CHECK (reminder_minutes IS NULL OR
                        (typeof(reminder_minutes) = 'integer' AND reminder_minutes BETWEEN 0 AND 10080)),
                    reminder_notified_at INTEGER CHECK (reminder_notified_at IS NULL OR
                        typeof(reminder_notified_at) = 'integer')
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)")}
            if "reminder_minutes" not in columns:
                conn.execute("""ALTER TABLE calendar_events ADD COLUMN reminder_minutes INTEGER DEFAULT 60
                    CHECK (reminder_minutes IS NULL OR
                        (typeof(reminder_minutes) = 'integer' AND reminder_minutes BETWEEN 0 AND 10080))""")
            if "reminder_notified_at" not in columns:
                conn.execute("""ALTER TABLE calendar_events ADD COLUMN reminder_notified_at INTEGER
                    CHECK (reminder_notified_at IS NULL OR typeof(reminder_notified_at) = 'integer')""")

    def list_events(self, start: date, end: date) -> list[dict]:
        """시작일을 포함하고 종료일을 제외하는 한국 날짜 범위의 일정을 반환한다."""
        if type(start) is not date or type(end) is not date:
            raise TypeError("조회 시작일과 종료일은 date여야 합니다.")
        if start >= end:
            raise ValueError("조회 종료일은 시작일보다 뒤여야 합니다.")
        range_start = int(datetime.combine(start, time.min, CALENDAR_TIMEZONE).timestamp())
        range_end = int(datetime.combine(end, time.min, CALENDAR_TIMEZONE).timestamp())
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, title, start_at, end_at, all_day, location, description, reminder_minutes
                FROM calendar_events
                WHERE start_at < ? AND end_at > ?
                ORDER BY start_at, id
            """, (range_end, range_start)).fetchall()
        return [dict(row) for row in rows]

    def get_event(self, event_id: str) -> dict | None:
        if not isinstance(event_id, str):
            raise TypeError("일정 ID는 문자열이어야 합니다.")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT id, title, start_at, end_at, all_day, location, description, reminder_minutes
                FROM calendar_events WHERE id = ?
            """, (event_id,)).fetchone()
        return dict(row) if row is not None else None

    def delete_event(self, event_id: str) -> dict | None:
        """정확히 지정한 ID의 일정만 삭제하고 실제 삭제한 일정을 반환한다."""
        if not isinstance(event_id, str):
            raise TypeError("일정 ID는 문자열이어야 합니다.")
        if not event_id.strip():
            raise ValueError("삭제할 일정 ID를 지정하세요.")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT id, title, start_at, end_at, all_day, location, description, reminder_minutes
                FROM calendar_events WHERE id = ?
            """, (event_id,)).fetchone()
            if row is None:
                return None
            deleted = conn.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
            remaining = conn.execute("SELECT 1 FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
            if deleted.rowcount != 1 or remaining is not None:
                raise sqlite3.DatabaseError("일정 삭제 결과를 확인할 수 없습니다.")
        return dict(row)

    def add_event(self, title: str, start: datetime, end: datetime, *,
                  all_day: bool = False, location: str = "", description: str = "",
                  reminder_minutes: int | None = 60) -> dict:
        for name, value in (("제목", title), ("장소", location), ("설명", description)):
            if not isinstance(value, str):
                raise TypeError(f"일정 {name}은 문자열이어야 합니다.")
        if not title.strip():
            raise ValueError("일정 제목을 입력하세요.")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise TypeError("일정 시작과 종료는 datetime이어야 합니다.")
        if not isinstance(all_day, bool):
            raise TypeError("종일 여부는 bool이어야 합니다.")
        if reminder_minutes is not None:
            if type(reminder_minutes) is not int:
                raise TypeError("알림 시간은 정수 분 또는 None이어야 합니다.")
            if not 0 <= reminder_minutes <= 10080:
                raise ValueError("알림 시간은 0분부터 10080분까지 설정할 수 있습니다.")
        try:
            start = start.replace(tzinfo=CALENDAR_TIMEZONE) if start.utcoffset() is None else start.astimezone(CALENDAR_TIMEZONE)
            end = end.replace(tzinfo=CALENDAR_TIMEZONE) if end.utcoffset() is None else end.astimezone(CALENDAR_TIMEZONE)
            start_at = int(start.replace(microsecond=0).timestamp())
            end_at = int(end.replace(microsecond=0).timestamp())
            datetime.fromtimestamp(start_at, CALENDAR_TIMEZONE)
            datetime.fromtimestamp(end_at, CALENDAR_TIMEZONE)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("지원하지 않는 일정 날짜 범위입니다.") from exc
        if end_at <= start_at:
            raise ValueError("종료 시간은 시작 시간보다 1초 이상 뒤여야 합니다.")
        if all_day and (start.time() != time.min or end.time() != time.min):
            raise ValueError("종일 일정은 한국 시간 자정에서 시작하고 종료해야 합니다.")
        event_id = str(uuid4())
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("""
                INSERT INTO calendar_events (id, title, start_at, end_at, all_day, location, description, reminder_minutes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (event_id, title.strip(), start_at, end_at, int(all_day), location, description, reminder_minutes))
            row = conn.execute("""
                SELECT id, title, start_at, end_at, all_day, location, description, reminder_minutes
                FROM calendar_events WHERE id = ?
            """, (event_id,)).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("저장된 일정을 확인할 수 없습니다.")
        return dict(row)

    def take_due_reminders(self, now: datetime, limit: int = 1) -> list[dict]:
        """아직 끝나지 않은 일정의 알림을 영속적으로 한 번만 가져온다."""
        if not isinstance(now, datetime):
            raise TypeError("알림 기준 시각은 datetime이어야 합니다.")
        if type(limit) is not int:
            raise TypeError("알림 조회 개수는 정수여야 합니다.")
        if limit < 1:
            raise ValueError("알림 조회 개수는 1 이상이어야 합니다.")
        try:
            now = now.replace(tzinfo=CALENDAR_TIMEZONE) if now.utcoffset() is None else now.astimezone(CALENDAR_TIMEZONE)
            now_at = int(now.replace(microsecond=0).timestamp())
            datetime.fromtimestamp(now_at, CALENDAR_TIMEZONE)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("지원하지 않는 알림 기준 날짜 범위입니다.") from exc
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
                SELECT id, title, start_at, end_at, all_day, location, description, reminder_minutes
                FROM calendar_events
                WHERE reminder_minutes IS NOT NULL AND reminder_notified_at IS NULL
                    AND start_at - reminder_minutes * 60 <= ? AND end_at > ?
                ORDER BY start_at, id LIMIT ?
            """, (now_at, now_at, limit)).fetchall()
            conn.executemany("""
                UPDATE calendar_events SET reminder_notified_at = ? WHERE id = ?
            """, [(now_at, row["id"]) for row in rows])
        return [dict(row) for row in rows]
