"""Resource-aware event pipeline for durable memory, habits and retrieval telemetry."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from config import Config


class MemoryEventPipeline:
    """Keep capture cheap and defer LLM/embedding work to idle batches."""

    NOISE = re.compile(
        r"(?:\[DEBUG\]|\[TTS\]|\[STT\]|\[Plugin\]|PID\s*\d+|대기\s*작업\s*ID|"
        r"Loading weights|임베딩\s*장치|HTTPConnectionPool|Traceback)", re.I,
    )
    ACTION = re.compile(
        r"(?:실행|열어|켜|꺼|생성|작성|수정|변경|저장|검색|찾아|예약|알람|만들어|삭제)"
    )

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or Path(Config.DB_PATH).with_name("memory_events.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_events (
              event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, session_id TEXT NOT NULL,
              workspace TEXT NOT NULL, user_text TEXT NOT NULL, assistant_text TEXT NOT NULL,
              payload TEXT NOT NULL DEFAULT '{}', important INTEGER NOT NULL DEFAULT 0,
              processed INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_memory_events_pending
              ON memory_events(processed, important, created_at);
            CREATE TABLE IF NOT EXISTS habit_stats (
              signature TEXT PRIMARY KEY, action TEXT NOT NULL, example TEXT NOT NULL,
              count INTEGER NOT NULL, session_count INTEGER NOT NULL,
              sessions TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS retrieval_usage (
              trace_id TEXT NOT NULL, chunk_id TEXT NOT NULL, query TEXT NOT NULL,
              source TEXT NOT NULL, retrieved INTEGER NOT NULL DEFAULT 1,
              included INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0,
              positive INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
              PRIMARY KEY(trace_id, chunk_id));
            """)

    @classmethod
    def _clean(cls, text: str) -> str:
        lines = [line.strip() for line in str(text or "").splitlines()
                 if line.strip() and not cls.NOISE.search(line)]
        return "\n".join(dict.fromkeys(lines))[:4000]

    @staticmethod
    def _signature(text: str) -> str:
        value = str(text).casefold()
        value = re.sub(r"[\"'].*?[\"']", " <text> ", value)
        value = re.sub(r"\b\d+(?:\.\d+)?\b", " <number> ", value)
        return re.sub(r"[^0-9a-z가-힣<>]+", " ", value).strip()[:260]

    def record_exchange(self, *, session_id: str, workspace: str,
                        user_text: str, assistant_text: str) -> str:
        user, assistant = self._clean(user_text), self._clean(assistant_text)
        if not user:
            return ""
        important = int(bool(re.search(
            r"(?:기억해|앞으로|항상|설정|변경|선호|저장|완료|반드시|규칙)", user,
        )))
        event_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?,?,?,?,?,?)", (
                event_id, "exchange", session_id or "unknown", workspace or "global",
                user, assistant, "{}", important, 0, time.time(),
            ))
            if self.ACTION.search(user):
                self._update_habit(db, session_id or "unknown", user)
        return event_id

    def record_approved_result(self, *, workspace: str, instruction: str,
                               result: dict, needs_consolidation: bool = True) -> str:
        event_id = uuid.uuid4().hex
        compact = {key: result.get(key) for key in (
            "renderer", "profile_id", "applied_edit_fields", "output"
        ) if result.get(key)}
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO memory_events VALUES (?,?,?,?,?,?,?,?,?,?)", (
                event_id, "approved_result", "workspace", workspace or "global",
                self._clean(instruction), "", json.dumps(compact, ensure_ascii=False),
                1, 0 if needs_consolidation else 1, time.time(),
            ))
        return event_id

    def _update_habit(self, db, session_id: str, text: str):
        signature = self._signature(text)
        if len(signature) < 4:
            return
        row = db.execute("SELECT * FROM habit_stats WHERE signature=?", (signature,)).fetchone()
        sessions = set(json.loads(row["sessions"])) if row else set()
        sessions.add(session_id)
        now = time.time()
        db.execute("""INSERT INTO habit_stats VALUES (?,?,?,?,?,?,?,?)
          ON CONFLICT(signature) DO UPDATE SET count=excluded.count,
          session_count=excluded.session_count,sessions=excluded.sessions,
          example=excluded.example,last_seen=excluded.last_seen""", (
            signature, "action", text[:500], int(row["count"]) + 1 if row else 1,
            len(sessions), json.dumps(sorted(sessions), ensure_ascii=False),
            float(row["first_seen"]) if row else now, now,
        ))

    def pending_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM memory_events WHERE processed=0").fetchone()[0])

    def consolidate_pending(self, consolidator, *, limit: int = 24) -> dict:
        """Run only durable candidates; mark the entire cheap batch as reviewed."""
        with self._lock, self._connect() as db:
            rows = db.execute("""SELECT * FROM memory_events WHERE processed=0
              ORDER BY important DESC, created_at ASC LIMIT ?""", (max(1, limit),)).fetchall()
        records, processed, errors = [], [], []
        for row in rows:
            try:
                if row["event_type"] == "exchange" and consolidator.should_consider(row["user_text"]):
                    records.extend(consolidator.consolidate(
                        row["user_text"], session_id=row["session_id"],
                        workspace_namespace=row["workspace"],
                    ))
                elif row["event_type"] == "approved_result":
                    text = f"승인된 작업: {row['user_text']} 결과={row['payload']}"
                    records.extend(consolidator.consolidate(
                        "기억해 " + text, session_id="approved-workspace",
                        workspace_namespace=row["workspace"],
                    ))
            except Exception as exc:
                errors.append({"event_id": row["event_id"], "error": str(exc)})
                continue
            else:
                processed.append(row["event_id"])
        if processed:
            with self._lock, self._connect() as db:
                db.executemany("UPDATE memory_events SET processed=1 WHERE event_id=?",
                               [(item,) for item in processed])
        return {"processed": len(processed), "records": records, "errors": errors}

    def retrieval_summary(self) -> dict:
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) total, COALESCE(SUM(included),0) included,
              COALESCE(SUM(used),0) used FROM retrieval_usage""").fetchone()
        return dict(row)

    def record_retrieval(self, query: str, results: list[dict]) -> str:
        trace_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            for index, item in enumerate(results):
                chunk_id = str(item.get("chunk_id") or item.get("doc_id") or f"rank-{index}")
                db.execute("INSERT OR REPLACE INTO retrieval_usage VALUES (?,?,?,?,1,0,0,0,?)", (
                    trace_id, chunk_id, str(query)[:1000], str(item.get("source", ""))[:1000], time.time(),
                ))
        return trace_id

    def mark_included(self, trace_id: str, chunk_ids: list[str]):
        if not trace_id or not chunk_ids:
            return
        with self._lock, self._connect() as db:
            db.executemany("UPDATE retrieval_usage SET included=1 WHERE trace_id=? AND chunk_id=?",
                           [(trace_id, str(item)) for item in chunk_ids])

    def mark_used(self, trace_id: str, chunk_ids: list[str]):
        """Mark only evidence explicitly cited by the generated answer as used."""
        if not trace_id or not chunk_ids:
            return
        with self._lock, self._connect() as db:
            db.executemany(
                "UPDATE retrieval_usage SET used=1 WHERE trace_id=? AND chunk_id=? AND included=1",
                [(trace_id, str(item)) for item in chunk_ids],
            )

    def habits(self, *, minimum_count: int = 3, minimum_sessions: int = 2) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM habit_stats WHERE count>=? AND session_count>=?
              ORDER BY count DESC,last_seen DESC""", (minimum_count, minimum_sessions)).fetchall()
        return [dict(row) for row in rows]


_pipeline = None


def get_memory_event_pipeline() -> MemoryEventPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = MemoryEventPipeline()
    return _pipeline
