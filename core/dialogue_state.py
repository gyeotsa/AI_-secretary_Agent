"""대화 중 사용자 응답을 기다리는 작업을 SQLite에 영속화한다."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import json
import sqlite3
import threading
import uuid

from config import Config


@dataclass
class StoredPendingRequest:
    task_id: str
    session_id: str
    original_goal: str
    question: str
    conversation_history: List[Dict[str, str]]
    created_at: str


@dataclass
class StoredAgentTask:
    task_id: str
    session_id: str
    goal: str
    status: str
    priority: int
    result: str
    created_at: str
    updated_at: str


class DialogueStateStore:
    def __init__(self, db_path: Optional[str] = None):
        default_path = Path(Config.DB_PATH).with_name("dialogue_state.db")
        self.db_path = str(Path(db_path) if db_path else default_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_requests (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    original_goal TEXT NOT NULL,
                    question TEXT NOT NULL,
                    conversation_history TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_requests(session_id, created_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(session_id, updated_at)")
            # 비정상 종료 당시 실행 중이던 작업은 자동 실행하지 않고 재개 가능한 상태로 둔다.
            conn.execute("UPDATE agent_tasks SET status = 'interrupted' WHERE status IN ('running', 'pausing')")

    def create(self, session_id: str, original_goal: str, question: str,
               conversation_history: List[Dict[str, str]], task_id: Optional[str] = None) -> StoredPendingRequest:
        item = StoredPendingRequest(
            task_id=task_id or uuid.uuid4().hex[:8], session_id=session_id,
            original_goal=original_goal, question=question,
            conversation_history=conversation_history,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO pending_requests VALUES (?, ?, ?, ?, ?, ?)",
                (item.task_id, item.session_id, item.original_goal, item.question,
                 json.dumps(item.conversation_history, ensure_ascii=False), item.created_at),
            )
        return item

    def list(self, session_id: str) -> List[StoredPendingRequest]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, original_goal, question, conversation_history, created_at "
                "FROM pending_requests WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [StoredPendingRequest(*row[:4], json.loads(row[4]), row[5]) for row in rows]

    def get(self, session_id: str, task_id: Optional[str] = None) -> Optional[StoredPendingRequest]:
        items = self.list(session_id)
        if not task_id:
            return items[0] if items else None
        return next((item for item in items if item.task_id == task_id), None)

    def delete(self, session_id: str, task_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pending_requests WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
        return cursor.rowcount > 0

    def create_task(self, session_id: str, goal: str, priority: int = 0,
                    task_id: Optional[str] = None) -> StoredAgentTask:
        now = datetime.now(timezone.utc).isoformat()
        item = StoredAgentTask(task_id or uuid.uuid4().hex[:8], session_id, goal,
                               "queued", priority, "", now, now)
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO agent_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        return item

    def update_task(self, task_id: str, *, status: Optional[str] = None,
                    priority: Optional[int] = None, result: Optional[str] = None) -> bool:
        fields, values = [], []
        for name, value in (("status", status), ("priority", priority), ("result", result)):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.extend([datetime.now(timezone.utc).isoformat(), task_id])
        with self._lock, self._connect() as conn:
            cursor = conn.execute(f"UPDATE agent_tasks SET {', '.join(fields)} WHERE task_id = ?", values)
        return cursor.rowcount > 0

    def get_task(self, session_id: str, task_id: str) -> Optional[StoredAgentTask]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, session_id, goal, status, priority, result, created_at, updated_at "
                "FROM agent_tasks WHERE session_id = ? AND task_id = ?", (session_id, task_id),
            ).fetchone()
        return StoredAgentTask(*row) if row else None

    def list_tasks(self, session_id: str, include_finished: bool = True) -> List[StoredAgentTask]:
        clause = "" if include_finished else "AND status NOT IN ('completed', 'failed', 'cancelled')"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, goal, status, priority, result, created_at, updated_at "
                f"FROM agent_tasks WHERE session_id = ? {clause} ORDER BY priority DESC, updated_at DESC",
                (session_id,),
            ).fetchall()
        return [StoredAgentTask(*row) for row in rows]


_dialogue_state_store = None


def get_dialogue_state_store() -> DialogueStateStore:
    global _dialogue_state_store
    if _dialogue_state_store is None:
        _dialogue_state_store = DialogueStateStore()
    return _dialogue_state_store
