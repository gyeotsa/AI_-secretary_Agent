"""대화 중 사용자 응답을 기다리는 작업을 SQLite에 영속화한다."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
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
    workspace_path: str = ""


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
    workspace_path: str = ""
    intent_name: str = ""
    slots: Dict[str, Any] = None
    pending_question: str = ""
    conversation_history: List[Dict[str, str]] = None
    artifacts: List[Dict[str, Any]] = None
    evidence: List[Dict[str, Any]] = None
    last_tool: str = ""
    retry_count: int = 0
    expires_at: str = ""
    plan: List[Dict[str, Any]] = None
    verification_status: str = ""
    context_confidence: float = 0.0

    def __post_init__(self):
        self.slots = dict(self.slots or {})
        self.conversation_history = list(self.conversation_history or [])
        self.artifacts = list(self.artifacts or [])
        self.evidence = list(self.evidence or [])
        self.plan = list(self.plan or [])


class DialogueStateStore:
    ALLOWED_TRANSITIONS = {
        "queued": {"running", "awaiting_user", "cancelled", "expired"},
        "awaiting_user": {"running", "cancelled", "expired"},
        "running": {"paused", "completed", "partial", "failed", "unverified", "cancelled", "interrupted"},
        "paused": {"running", "cancelled", "interrupted"},
        "interrupted": {"running", "cancelled", "expired"},
        "completed": set(),
        "partial": set(),
        "failed": set(),
        "unverified": set(),
        "cancelled": set(),
        "expired": set(),
    }

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
                    created_at TEXT NOT NULL,
                    workspace_path TEXT NOT NULL DEFAULT ''
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
                    updated_at TEXT NOT NULL,
                    workspace_path TEXT NOT NULL DEFAULT '',
                    intent_name TEXT NOT NULL DEFAULT '',
                    slots TEXT NOT NULL DEFAULT '{}',
                    pending_question TEXT NOT NULL DEFAULT '',
                    conversation_history TEXT NOT NULL DEFAULT '[]',
                    artifacts TEXT NOT NULL DEFAULT '[]',
                    evidence TEXT NOT NULL DEFAULT '[]',
                    last_tool TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL DEFAULT '',
                    plan TEXT NOT NULL DEFAULT '[]',
                    verification_status TEXT NOT NULL DEFAULT '',
                    context_confidence REAL NOT NULL DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(session_id, updated_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS intent_states (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    intent_name TEXT NOT NULL,
                    slots TEXT NOT NULL,
                    original_request TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recent_intents (
                    session_id TEXT PRIMARY KEY,
                    intent_name TEXT NOT NULL,
                    slots TEXT NOT NULL,
                    original_request TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self._ensure_column(conn, "pending_requests", "workspace_path", "TEXT NOT NULL DEFAULT ''")
            for name, definition in (
                ("workspace_path", "TEXT NOT NULL DEFAULT ''"),
                ("intent_name", "TEXT NOT NULL DEFAULT ''"),
                ("slots", "TEXT NOT NULL DEFAULT '{}'"),
                ("pending_question", "TEXT NOT NULL DEFAULT ''"),
                ("conversation_history", "TEXT NOT NULL DEFAULT '[]'"),
                ("artifacts", "TEXT NOT NULL DEFAULT '[]'"),
                ("evidence", "TEXT NOT NULL DEFAULT '[]'"),
                ("last_tool", "TEXT NOT NULL DEFAULT ''"),
                ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("expires_at", "TEXT NOT NULL DEFAULT ''"),
                ("plan", "TEXT NOT NULL DEFAULT '[]'"),
                ("verification_status", "TEXT NOT NULL DEFAULT ''"),
                ("context_confidence", "REAL NOT NULL DEFAULT 0"),
            ):
                self._ensure_column(conn, "agent_tasks", name, definition)
            # 비정상 종료 당시 실행 중이던 작업은 자동 실행하지 않고 재개 가능한 상태로 둔다.
            conn.execute("UPDATE agent_tasks SET status = 'interrupted' WHERE status IN ('running', 'pausing')")

    @staticmethod
    def _ensure_column(conn, table: str, name: str, definition: str):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def create(self, session_id: str, original_goal: str, question: str,
               conversation_history: List[Dict[str, str]], task_id: Optional[str] = None,
               workspace_path: str = "") -> StoredPendingRequest:
        item = StoredPendingRequest(
            task_id=task_id or uuid.uuid4().hex[:8], session_id=session_id,
            original_goal=original_goal, question=question,
            conversation_history=conversation_history,
            created_at=datetime.now(timezone.utc).isoformat(),
            workspace_path=workspace_path,
        )
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO pending_requests "
                "(task_id, session_id, original_goal, question, conversation_history, created_at, workspace_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.task_id, item.session_id, item.original_goal, item.question,
                 json.dumps(item.conversation_history, ensure_ascii=False), item.created_at,
                 item.workspace_path),
            )
            conn.execute(
                "UPDATE agent_tasks SET status='awaiting_user', pending_question=?, "
                "conversation_history=?, workspace_path=?, expires_at=?, updated_at=? WHERE task_id=?",
                (question, json.dumps(conversation_history, ensure_ascii=False),
                 workspace_path, expires_at, item.created_at, item.task_id),
            )
        return item

    def list(self, session_id: str, workspace_path: Optional[str] = None) -> List[StoredPendingRequest]:
        self.expire_stale_pending()
        workspace_clause = "" if workspace_path is None else " AND workspace_path = ?"
        params = (session_id,) if workspace_path is None else (session_id, workspace_path)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, original_goal, question, conversation_history, "
                "created_at, workspace_path FROM pending_requests WHERE session_id = ?"
                f"{workspace_clause} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [StoredPendingRequest(*row[:4], json.loads(row[4]), row[5], row[6]) for row in rows]

    def get(self, session_id: str, task_id: Optional[str] = None,
            workspace_path: Optional[str] = None) -> Optional[StoredPendingRequest]:
        items = self.list(session_id, workspace_path)
        if not task_id:
            return items[0] if items else None
        return next((item for item in items if item.task_id == task_id), None)

    def delete(self, session_id: str, task_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pending_requests WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
            conn.execute(
                "UPDATE agent_tasks SET pending_question='', updated_at=? WHERE task_id=?",
                (datetime.now(timezone.utc).isoformat(), task_id),
            )
        return cursor.rowcount > 0

    def create_task(self, session_id: str, goal: str, priority: int = 0,
                    task_id: Optional[str] = None, workspace_path: str = "") -> StoredAgentTask:
        now = datetime.now(timezone.utc).isoformat()
        item = StoredAgentTask(
            task_id or uuid.uuid4().hex[:8], session_id, goal,
            "queued", priority, "", now, now, workspace_path,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_tasks "
                "(task_id,session_id,goal,status,priority,result,created_at,updated_at,workspace_path) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (item.task_id, item.session_id, item.goal, item.status, item.priority,
                 item.result, item.created_at, item.updated_at, item.workspace_path),
            )
        return item

    def update_task(self, task_id: str, *, status: Optional[str] = None,
                    priority: Optional[int] = None, result: Optional[str] = None,
                    workspace_path: Optional[str] = None,
                    intent_name: Optional[str] = None, slots: Optional[Dict[str, Any]] = None,
                    pending_question: Optional[str] = None,
                    conversation_history: Optional[List[Dict[str, str]]] = None,
                    artifacts: Optional[List[Dict[str, Any]]] = None,
                    evidence: Optional[List[Dict[str, Any]]] = None,
                    last_tool: Optional[str] = None, retry_count: Optional[int] = None,
                    expires_at: Optional[str] = None,
                    plan: Optional[List[Dict[str, Any]]] = None,
                    verification_status: Optional[str] = None,
                    context_confidence: Optional[float] = None) -> bool:
        fields, values = [], []
        json_fields = {
            "slots": slots, "conversation_history": conversation_history,
            "artifacts": artifacts, "evidence": evidence,
            "plan": plan,
        }
        for name, value in (
            ("status", status), ("priority", priority), ("result", result),
            ("workspace_path", workspace_path), ("intent_name", intent_name),
            ("pending_question", pending_question), ("last_tool", last_tool),
            ("retry_count", retry_count), ("expires_at", expires_at),
            ("verification_status", verification_status),
            ("context_confidence", context_confidence),
        ):
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(value)
        for name, value in json_fields.items():
            if value is not None:
                fields.append(f"{name} = ?")
                values.append(json.dumps(value, ensure_ascii=False))
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.extend([datetime.now(timezone.utc).isoformat(), task_id])
        with self._lock, self._connect() as conn:
            cursor = conn.execute(f"UPDATE agent_tasks SET {', '.join(fields)} WHERE task_id = ?", values)
        return cursor.rowcount > 0

    def transition_task(self, task_id: str, status: str, **changes) -> bool:
        """Reject impossible task transitions while allowing idempotent updates."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if not row:
            return False
        current = row[0]
        if status != current and status not in self.ALLOWED_TRANSITIONS.get(current, set()):
            return False
        return self.update_task(task_id, status=status, **changes)

    def get_task(self, session_id: str, task_id: str,
                 workspace_path: Optional[str] = None) -> Optional[StoredAgentTask]:
        workspace_clause = "" if workspace_path is None else " AND workspace_path = ?"
        params = ((session_id, task_id) if workspace_path is None
                  else (session_id, task_id, workspace_path))
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, session_id, goal, status, priority, result, created_at, updated_at, "
                "workspace_path, intent_name, slots, pending_question, conversation_history, "
                "artifacts, evidence, last_tool, retry_count, expires_at, plan, verification_status, "
                "context_confidence FROM agent_tasks WHERE session_id = ? AND task_id = ?"
                f"{workspace_clause}", params,
            ).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(self, session_id: str, include_finished: bool = True,
                   workspace_path: Optional[str] = None) -> List[StoredAgentTask]:
        clause = (
            "" if include_finished
            else "AND status NOT IN ('completed', 'partial', 'failed', 'unverified', 'cancelled', 'expired')"
        )
        workspace_clause = "" if workspace_path is None else " AND workspace_path = ?"
        params = (session_id,) if workspace_path is None else (session_id, workspace_path)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, session_id, goal, status, priority, result, created_at, updated_at, "
                "workspace_path, intent_name, slots, pending_question, conversation_history, "
                "artifacts, evidence, last_tool, retry_count, expires_at, plan, verification_status, "
                "context_confidence "
                f"FROM agent_tasks WHERE session_id = ? {clause}{workspace_clause} "
                "ORDER BY priority DESC, updated_at DESC",
                params,
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _task_from_row(row) -> StoredAgentTask:
        values = list(row)
        for index, fallback in ((10, {}), (12, []), (13, []), (14, []), (18, [])):
            try:
                values[index] = json.loads(values[index]) if values[index] else fallback
            except (TypeError, json.JSONDecodeError):
                values[index] = fallback
        return StoredAgentTask(*values)

    def expire_stale_pending(self) -> int:
        """만료된 확인 질문은 새 요청을 가로채지 않도록 종료한다."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            task_ids = [
                row[0] for row in conn.execute(
                    "SELECT task_id FROM agent_tasks WHERE status='awaiting_user' "
                    "AND expires_at != '' AND expires_at < ?",
                    (now,),
                ).fetchall()
            ]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(
                f"UPDATE agent_tasks SET status='expired', result='확인 응답 대기 시간 만료', "
                f"pending_question='', updated_at=? WHERE task_id IN ({placeholders})",
                (now, *task_ids),
            )
            conn.execute(
                f"DELETE FROM pending_requests WHERE task_id IN ({placeholders})",
                task_ids,
            )
        return len(task_ids)

    def save_intent_state(self, task_id: str, session_id: str, intent_name: str,
                          slots: Dict[str, Any], original_request: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO intent_states VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, session_id, intent_name, json.dumps(slots, ensure_ascii=False), original_request, now),
            )
            conn.execute(
                "UPDATE agent_tasks SET intent_name=?, slots=?, updated_at=? WHERE task_id=?",
                (intent_name, json.dumps(slots, ensure_ascii=False), now, task_id),
            )

    def get_intent_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            task_row = conn.execute(
                "SELECT session_id, intent_name, slots, goal FROM agent_tasks "
                "WHERE task_id = ? AND intent_name != ''",
                (task_id,),
            ).fetchone()
        if task_row:
            return {
                "session_id": task_row[0],
                "intent_name": task_row[1],
                "slots": json.loads(task_row[2]) if task_row[2] else {},
                "original_request": task_row[3],
            }
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT session_id, intent_name, slots, original_request FROM intent_states WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        return {"session_id": row[0], "intent_name": row[1], "slots": json.loads(row[2]), "original_request": row[3]}

    def delete_intent_state(self, task_id: str):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM intent_states WHERE task_id = ?", (task_id,))
            conn.execute(
                "UPDATE agent_tasks SET intent_name='', slots='{}', pending_question='', "
                "updated_at=? WHERE task_id=?",
                (datetime.now(timezone.utc).isoformat(), task_id),
            )

    def save_recent_intent(self, session_id: str, intent_name: str,
                           slots: Dict[str, Any], original_request: str,
                           task_id: str = "", workspace_path: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO recent_intents VALUES (?, ?, ?, ?, ?)",
                (session_id, intent_name, json.dumps(slots, ensure_ascii=False), original_request, now),
            )
            if task_id:
                conn.execute(
                    "UPDATE agent_tasks SET intent_name=?, slots=?, workspace_path=?, "
                    "updated_at=? WHERE task_id=? AND session_id=?",
                    (intent_name, json.dumps(slots, ensure_ascii=False), workspace_path,
                     now, task_id, session_id),
                )

    def get_recent_intent(self, session_id: str,
                          workspace_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        workspace_clause = "" if workspace_path is None else " AND workspace_path = ?"
        params = (session_id,) if workspace_path is None else (session_id, workspace_path)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT intent_name, slots, goal, task_id FROM agent_tasks "
                "WHERE session_id = ? AND intent_name != ''"
                f"{workspace_clause} ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
        if row:
            return {
                "intent_name": row[0],
                "slots": json.loads(row[1]) if row[1] else {},
                "original_request": row[2],
                "task_id": row[3],
            }
        if workspace_path not in (None, ""):
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT intent_name, slots, original_request FROM recent_intents WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {"intent_name": row[0], "slots": json.loads(row[1]), "original_request": row[2]}

    def restore_interrupted(self, session_id: str,
                            workspace_path: Optional[str] = None) -> List[StoredAgentTask]:
        """재시작 후 자동 실행하지 않고 명시적 재개 가능한 작업만 반환한다."""
        return [
            task for task in self.list_tasks(session_id, include_finished=False,
                                             workspace_path=workspace_path)
            if task.status in {"interrupted", "paused", "awaiting_user", "queued"}
        ]

    def clear_session(self, session_id: str):
        """대화 리셋/삭제 시 연결된 대기 작업과 Intent 문맥도 제거한다."""
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM pending_requests WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM agent_tasks WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM intent_states WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM recent_intents WHERE session_id = ?", (session_id,))


_dialogue_state_store = None


def get_dialogue_state_store() -> DialogueStateStore:
    global _dialogue_state_store
    if _dialogue_state_store is None:
        _dialogue_state_store = DialogueStateStore()
    return _dialogue_state_store
