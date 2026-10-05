"""Local work continuity: immutable evidence plus a rebuildable plan projection.

External transcripts are evidence, never instructions.  Only structured plans and
explicit user actions become tasks; unstructured conversations remain suggestions.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_UNSET = object()
_STATUSES = {"suggested", "pending", "in_progress", "blocked", "done", "cancelled"}
_ALIASES = {"completed": "done", "complete": "done", "todo": "pending",
            "not_started": "pending", "confirmed": "pending", "running": "in_progress",
            "in-progress": "in_progress", "canceled": "cancelled", "deleted": "cancelled",
            "queued": "pending", "skipped": "cancelled", "expired": "cancelled",
            "awaiting_user": "blocked", "awaiting_approval": "blocked", "paused": "blocked",
            "interrupted": "blocked", "failed": "blocked", "unverified": "blocked", "partial": "blocked"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(*parts: Any) -> str:
    return hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()[:32]


def _time(value: Any = None) -> str:
    if value is None or value == "":
        value = datetime.now(timezone.utc)
    elif isinstance(value, (float, int)):
        value = datetime.fromtimestamp(value / 1000 if value > 100000000000 else value, timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Invalid timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _status(value: Any) -> str:
    value = str(value or "pending").lower().strip()
    value = _ALIASES.get(value, value)
    return value if value in _STATUSES else "pending"


def _title(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith(("<environment_context>", "<INSTRUCTIONS>", "# AGENTS.md instructions",
                        "<recommended_plugins>", "<permissions instructions>")):
        return ""
    if "## My request:" in text:
        text = text.split("## My request:", 1)[1].strip()
    return text.split("\n", 1)[0][:160] if text else ""


class ContinuityStore:
    """Thread-safe SQLite repository; reads never touch the source log files."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from config import Config
            db_path = Path(Config.DB_PATH).with_name("continuity.db")
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, path TEXT NOT NULL,
                enabled INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(provider,path));
            CREATE TABLE IF NOT EXISTS sync_cursors (
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                file_path TEXT NOT NULL, cursor_json TEXT NOT NULL,
                PRIMARY KEY(source_id,file_path));
            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                provider TEXT NOT NULL, session_id TEXT NOT NULL, title TEXT NOT NULL,
                workspace_path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(source_id,provider,session_id));
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
                timestamp TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL,
                text TEXT NOT NULL, payload_json TEXT NOT NULL, source_ref_json TEXT NOT NULL,
                UNIQUE(source_id,event_id));
            CREATE INDEX IF NOT EXISTS continuity_events_session ON events(session_key,timestamp);
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
                external_id TEXT NOT NULL, goal TEXT NOT NULL, updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0, UNIQUE(session_key,external_id));
            CREATE TABLE IF NOT EXISTS plan_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                event_key TEXT NOT NULL UNIQUE REFERENCES events(id) ON DELETE CASCADE,
                timestamp TEXT NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS steps (
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                external_id TEXT NOT NULL, position INTEGER NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL, verification TEXT NOT NULL,
                dependencies_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0, due_at TEXT, snoozed_until TEXT,
                active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
                UNIQUE(plan_id,external_id));
            CREATE TABLE IF NOT EXISTS step_overrides (
                step_id TEXT PRIMARY KEY REFERENCES steps(id) ON DELETE CASCADE,
                patch_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS user_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
                timestamp TEXT NOT NULL, patch_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reminder_claims (
                step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, bucket TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(step_id,kind,bucket));
        """)
        self._db.commit()
        self._repair_context_titles()

    def _repair_context_titles(self):
        """Upgrade derived labels from old collectors without rewriting receipts."""
        if self.get_setting("context_titles_v1", False):
            return
        with self._lock, self._db:
            for session in list(self._db.execute("SELECT session_key,title FROM sessions")):
                if _title(session["title"]):
                    continue
                for row in self._db.execute("SELECT text FROM events WHERE session_key=? AND kind='message' AND role='user' ORDER BY timestamp,rowid", (session["session_key"],)):
                    title = _title(row["text"])
                    if title:
                        self._db.execute("UPDATE sessions SET title=? WHERE session_key=?", (title, session["session_key"]))
                        self._db.execute("UPDATE plans SET goal=? WHERE session_key=? AND goal=?", (title, session["session_key"], session["title"]))
                        break
            self._db.execute("INSERT OR REPLACE INTO settings VALUES('context_titles_v1','true')")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def sources(self) -> list[dict]:
        with self._lock:
            return [dict(row, source_id=row["id"], enabled=bool(row["enabled"])) for row in self._db.execute(
                "SELECT * FROM sources ORDER BY provider,path")]

    def add_source(self, provider: str, path: str, enabled: bool = True) -> dict:
        provider = str(provider).strip().lower()
        path = str(path).strip()
        if not provider or not path:
            raise ValueError("Provider and source path are required")
        if "://" not in path:
            path = str(Path(path).expanduser().resolve())
        source_id, now = _id(provider, path), _time()
        with self._lock, self._db:
            self._db.execute("""INSERT INTO sources VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (source_id, provider, path, int(enabled), now, now))
            row = self._db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            return dict(row, source_id=row["id"], enabled=bool(row["enabled"]))

    def set_source_enabled(self, source_id: str, enabled: bool) -> None:
        with self._lock, self._db:
            if not self._db.execute("UPDATE sources SET enabled=?,updated_at=? WHERE id=?",
                                    (int(enabled), _time(), source_id)).rowcount:
                raise KeyError(source_id)

    def delete_source(self, source_id: str, purge: bool = True) -> None:
        """Delete imported copies and derived state, never the original files."""
        if not purge:
            self.set_source_enabled(source_id, False)
            return
        with self._lock, self._db:
            self._db.execute("DELETE FROM sources WHERE id=?", (source_id,))
            self._db.execute("DELETE FROM settings WHERE key IN (?,?)", ("scan:" + source_id, "source_report:" + source_id))

    def get_cursor(self, source_id: str, file_path: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT cursor_json FROM sync_cursors WHERE source_id=? AND file_path=?",
                                   (source_id, str(file_path))).fetchone()
            return json.loads(row[0]) if row else {}

    def ingest_batch(self, source_id: str, file_path: str, events: list[dict], cursor: dict) -> int:
        """Commit normalized events and their byte cursor in one transaction.

        Duplicate IDs are ignored, including after a restart or file rotation.
        Invalid batches roll back the cursor as well as every event in the batch.
        """
        with self._lock, self._db:
            source = self._db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise KeyError(source_id)
            if not source["enabled"]:
                return 0
            inserted = 0
            for event in events:
                event_id = str(event.get("event_id") or _id(event))
                event_key = _id(source_id, event_id)
                if self._db.execute("SELECT 1 FROM events WHERE id=?", (event_key,)).fetchone():
                    if event.get("kind") == "message" and event.get("role") == "user":
                        title = _title(event.get("text"))
                        existing = self._db.execute("SELECT s.session_key,s.title FROM sessions s JOIN events e ON e.session_key=s.session_key WHERE e.id=?", (event_key,)).fetchone()
                        if title and existing and not _title(existing["title"]):
                            self._db.execute("UPDATE sessions SET title=? WHERE session_key=?", (title, existing["session_key"]))
                    continue
                session_id = str(event.get("session_id") or file_path)
                session_key = _id(source_id, source["provider"], session_id)
                timestamp = _time(event.get("timestamp"))
                kind, role = str(event.get("kind", "message")), str(event.get("role", ""))
                text = str(event.get("text") or "")
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    raise ValueError("Event payload must be an object")
                source_ref = event.get("source_ref") or {"file_path": str(file_path), "event_id": event_id}
                workspace = str(event.get("workspace_path") or payload.get("workspace_path") or "")
                title = str(payload.get("title") or event.get("title") or "")
                if not title and kind == "message" and role == "user":
                    title = _title(text)
                self._db.execute("""INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(session_key) DO UPDATE SET
                    title=CASE WHEN sessions.title='' THEN excluded.title ELSE sessions.title END,
                    workspace_path=CASE WHEN excluded.workspace_path<>'' THEN excluded.workspace_path ELSE sessions.workspace_path END,
                    created_at=MIN(sessions.created_at,excluded.created_at),
                    updated_at=MAX(sessions.updated_at,excluded.updated_at)""",
                    (session_key, source_id, source["provider"], session_id, title, workspace, timestamp, timestamp))
                self._db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    event_key, event_id, source_id, session_key, timestamp, kind, role, text,
                    _json(payload), _json(source_ref)))
                inserted += 1
                evidence = {"event_id": event_id, "event_key": event_key, "source_id": source_id,
                            "source_ref": source_ref, "timestamp": timestamp}
                if kind in {"plan", "task"}:
                    self._reduce_plan(session_key, event_key, timestamp, kind, payload, evidence)
            self._db.execute("""INSERT INTO sync_cursors VALUES(?,?,?) ON CONFLICT(source_id,file_path)
                DO UPDATE SET cursor_json=excluded.cursor_json""", (source_id, str(file_path), _json(cursor)))
            return inserted

    def _reduce_plan(self, session_key: str, event_key: str, timestamp: str,
                     kind: str, payload: dict, evidence: dict) -> None:
        external_id = str(payload.get("plan_id") or ("tasks" if kind == "task" else "main"))
        plan_id = _id(session_key, external_id)
        prior = self._db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        session_title = self._db.execute("SELECT title FROM sessions WHERE session_key=?", (session_key,)).fetchone()[0]
        goal = str(payload.get("goal") or (prior["goal"] if prior else "") or session_title)
        self._db.execute("INSERT OR IGNORE INTO plans VALUES(?,?,?,?,?,0)",
                         (plan_id, session_key, external_id, goal, timestamp))
        self._db.execute("INSERT INTO plan_versions(plan_id,event_key,timestamp,payload_json) VALUES(?,?,?,?)",
                         (plan_id, event_key, timestamp, _json(payload)))
        if external_id != "candidate-plan" and payload.get("authoritative", True):
            self._db.execute("""UPDATE steps SET active=0 WHERE plan_id IN
                (SELECT id FROM plans WHERE session_key=? AND external_id='candidate-plan')
                AND id NOT IN (SELECT step_id FROM step_overrides)""", (session_key,))
        # Preserve history even when a delayed import precedes the current projection.
        if kind != "task" and prior and timestamp < prior["updated_at"]:
            return
        if external_id == "candidate-plan" and self._db.execute(
                "SELECT 1 FROM plans WHERE session_key=? AND external_id<>'candidate-plan' LIMIT 1", (session_key,)).fetchone():
            return
        old = {row["external_id"]: dict(row) for row in self._db.execute(
            "SELECT * FROM steps WHERE plan_id=?", (plan_id,))}
        if kind == "task":
            task = dict(payload)
            task_id = str(task.get("task_id") or task.get("id") or "")
            if not task_id:
                return
            task["id"] = task_id
            if payload.get("operation") == "delete":
                task["status"] = "cancelled"
            raw_steps = [task]
        else:
            raw_steps = payload.get("steps", [])
            if not isinstance(raw_steps, list):
                raise ValueError("Plan steps must be a list")
            self._db.execute("UPDATE steps SET active=0 WHERE plan_id=?", (plan_id,))
        seen: dict[str, int] = {}
        for position, step in enumerate(raw_steps):
            if isinstance(step, str):
                step = {"description": step}
            if not isinstance(step, dict):
                raise ValueError("Plan step must be an object or string")
            description = str(step.get("description") or step.get("step") or step.get("title") or "")
            identity = str(step.get("id") or step.get("task_id") or _id(description))
            seen[identity] = seen.get(identity, 0) + 1
            if seen[identity] > 1:
                identity = f"{identity}:{seen[identity]}"
            previous = old.get(identity, {})
            if kind == "task" and previous and timestamp < previous["updated_at"]:
                continue
            description = description or previous.get("description", "")
            if not description:
                description = f"작업 {identity}"
            status = _status(step.get("status", previous.get("status", "pending")))
            if payload.get("authoritative") is False:
                status = "suggested"
            verification = str(step.get("verification") or payload.get("verification") or "reported")
            if verification not in {"reported", "verified", "user_confirmed"}:
                verification = "reported"
            dependencies = step.get("dependencies", json.loads(previous.get("dependencies_json", "[]"))) or []
            if not isinstance(dependencies, list):
                raise ValueError("Step dependencies must be a list")
            dependencies = [str(dependency) for dependency in dependencies]
            if isinstance(step.get("add_dependencies"), list):
                dependencies = list(dict.fromkeys(dependencies + [str(d) for d in step["add_dependencies"]]))
            step_id = _id(plan_id, identity)
            priority = int(step.get("priority", previous.get("priority", 0)))
            priority = max(0, min(3, priority))
            due_at = step.get("due_at", previous.get("due_at"))
            due_at = _time(due_at) if due_at else None
            snoozed_until = previous.get("snoozed_until")
            if kind == "task":
                position = previous.get("position", len(old))
            self._db.execute("""INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET position=excluded.position,description=excluded.description,
                status=excluded.status,verification=excluded.verification,dependencies_json=excluded.dependencies_json,
                evidence_json=excluded.evidence_json,priority=excluded.priority,due_at=excluded.due_at,
                active=1,updated_at=excluded.updated_at""", (
                    step_id, plan_id, identity, position, description, status, verification,
                    _json(dependencies), _json({**evidence, "receipts": step.get("evidence", [])}),
                    priority, due_at, snoozed_until, 1, timestamp))
        self._db.execute("UPDATE plans SET goal=?,updated_at=MAX(updated_at,?),version=version+1 WHERE id=?",
                         (goal, timestamp, plan_id))

    def list_sessions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""SELECT s.*,src.enabled,
                (SELECT COUNT(*) FROM events e WHERE e.session_key=s.session_key) AS event_count
                FROM sessions s JOIN sources src ON src.id=s.source_id
                ORDER BY s.updated_at DESC,s.session_key LIMIT ?""", (max(1, min(int(limit), 10000)),))
            return [dict(row) for row in rows]

    def _step_rows(self, session_key: str | None = None, enabled_only: bool = False) -> list[dict]:
        query = """SELECT st.*,p.goal,p.session_key,p.external_id AS external_plan_id,
            s.provider,s.workspace_path,s.title AS session_title,s.source_id,src.enabled,
            o.patch_json AS override_json FROM steps st JOIN plans p ON p.id=st.plan_id
            JOIN sessions s ON s.session_key=p.session_key JOIN sources src ON src.id=s.source_id
            LEFT JOIN step_overrides o ON o.step_id=st.id WHERE st.active=1"""
        args = []
        if session_key:
            query += " AND p.session_key=?"
            args.append(session_key)
        if enabled_only:
            query += " AND src.enabled=1"
        result = []
        for raw in self._db.execute(query + " ORDER BY p.id,st.position", args):
            row = dict(raw)
            row["dependencies"] = json.loads(row.pop("dependencies_json"))
            row["evidence"] = json.loads(row.pop("evidence_json"))
            row["source_ref"] = row["evidence"].get("source_ref")
            override = json.loads(row.pop("override_json") or "{}")
            row["reported_status"] = row["status"]
            row.update(override)
            row["step_id"] = row["id"]
            row["title"] = row["description"]
            row["waiting_reason"] = ""
            result.append(row)
        return result

    def get_step(self, step_id: str) -> dict:
        with self._lock:
            for row in self._step_rows():
                if row["id"] == step_id:
                    return row
        raise KeyError(step_id)

    def session_detail(self, session_key: str, limit: int = 500) -> dict:
        with self._lock:
            session = self._db.execute("SELECT * FROM sessions WHERE session_key=?", (session_key,)).fetchone()
            if session is None:
                raise KeyError(session_key)
            events = []
            count = self._db.execute("SELECT COUNT(*) FROM events WHERE session_key=?", (session_key,)).fetchone()[0]
            selected = list(self._db.execute("SELECT * FROM events WHERE session_key=? ORDER BY timestamp DESC,rowid DESC LIMIT ?",
                                           (session_key, max(1, min(int(limit), 10000)))))
            for raw in reversed(selected):
                row = dict(raw)
                row["payload"] = json.loads(row.pop("payload_json"))
                row["source_ref"] = json.loads(row.pop("source_ref_json"))
                events.append(row)
            plans = []
            for raw in self._db.execute("SELECT * FROM plans WHERE session_key=? ORDER BY updated_at", (session_key,)):
                row = dict(raw)
                row["versions"] = [dict(version, payload=json.loads(version["payload_json"]))
                    for version in self._db.execute("SELECT * FROM plan_versions WHERE plan_id=? ORDER BY id", (row["id"],))]
                plans.append(row)
            history = [dict(row, patch=json.loads(row["patch_json"])) for row in self._db.execute(
                """SELECT h.* FROM user_history h JOIN steps st ON st.id=h.step_id
                JOIN plans p ON p.id=st.plan_id WHERE p.session_key=? ORDER BY h.id""", (session_key,))]
            return {"session": dict(session), "event_count": count, "has_more": count > len(events),
                    "messages": [e for e in events if e["kind"] == "message"],
                    "events": events, "plans": plans, "steps": self._step_rows(session_key), "history": history}

    def update_step(self, step_id: str, status: Any = _UNSET, due_at: Any = _UNSET,
                    priority: Any = _UNSET, snoozed_until: Any = _UNSET) -> dict:
        """Persist a user's explicit choice separately so imports cannot erase it."""
        patch = {}
        if status is not _UNSET and status is not None:
            normalized = _ALIASES.get(str(status), str(status))
            if normalized not in _STATUSES:
                raise ValueError("Unknown step status")
            patch.update(status=normalized, verification="user_confirmed")
        if priority is not _UNSET and priority is not None:
            if isinstance(priority, bool) or not 0 <= int(priority) <= 3:
                raise ValueError("Priority must be between 0 and 3")
            patch["priority"] = int(priority)
        for name, value in (("due_at", due_at), ("snoozed_until", snoozed_until)):
            if value is not _UNSET:
                patch[name] = _time(value) if value else None
        with self._lock, self._db:
            self.get_step(step_id)
            if patch:
                now = _time()
                patch["updated_at"] = now
                current = self._db.execute("SELECT patch_json FROM step_overrides WHERE step_id=?", (step_id,)).fetchone()
                merged = json.loads(current[0]) if current else {}
                merged.update(patch)
                self._db.execute("INSERT INTO step_overrides VALUES(?,?) ON CONFLICT(step_id) DO UPDATE SET patch_json=excluded.patch_json",
                                 (step_id, _json(merged)))
                self._db.execute("INSERT INTO user_history(step_id,timestamp,patch_json) VALUES(?,?,?)", (step_id, now, _json(patch)))
            return self.get_step(step_id)

    def confirm_suggestion(self, session_key: str, description: str | None = None) -> dict:
        with self._lock, self._db:
            session = self._db.execute("SELECT * FROM sessions WHERE session_key=?", (session_key,)).fetchone()
            if session is None:
                raise KeyError(session_key)
            source = self._db.execute("SELECT enabled FROM sources WHERE id=?", (session["source_id"],)).fetchone()
            if not source["enabled"]:
                raise ValueError("Disabled source cannot create next actions")
            payload = {"plan_id": "user-continuation", "goal": session["title"],
                       "steps": [{"id": "continue", "description": description or f"작업 이어서 진행: {session['title'] or session['session_id']}",
                                  "status": "pending", "verification": "user_confirmed"}]}
            event_id = "user-continuation:" + session_key
            event_key = _id(session["source_id"], event_id)
            plan_id = _id(session_key, "user-continuation")
            step_id = _id(plan_id, "continue")
            if not self._db.execute("SELECT 1 FROM events WHERE id=?", (event_key,)).fetchone():
                now = _time()
                ref = {"type": "user_confirmation", "session_key": session_key}
                self._db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    event_key, event_id, session["source_id"], session_key, now, "plan", "user", "", _json(payload), _json(ref)))
                self._reduce_plan(session_key, event_key, now, "plan", payload,
                                  {"event_id": event_id, "event_key": event_key, "source_ref": ref, "timestamp": now})
            return self.get_step(step_id)

    def dashboard(self, now: Any = None) -> dict:
        current = _time(now)
        with self._lock:
            rows = self._step_rows(enabled_only=True)
            lookup = {(row["plan_id"], row["external_id"]): row for row in rows}
            by_id = {row["id"]: row for row in rows}
            next_actions, blocked, recent, suggested = [], [], [], []
            for row in rows:
                row["overdue"] = bool(row["due_at"] and row["due_at"] <= current)
                row["snoozed"] = bool(row["snoozed_until"] and row["snoozed_until"] > current)
                row["reason"] = ("마감 시각 지남" if row["overdue"] else
                    "높은 우선순위" if row["priority"] >= 2 else
                    "마감 예정" if row["due_at"] else
                    "진행하던 단계 이어가기" if row["status"] == "in_progress" else "진행 가능한 미완료 단계")
                if row["status"] in {"done", "cancelled"}:
                    recent.append(row)
                    continue
                if row["status"] == "suggested":
                    suggested.append(row)
                    continue
                waiting = []
                for dependency in row["dependencies"]:
                    prerequisite = lookup.get((row["plan_id"], dependency))
                    if prerequisite is None:
                        prerequisite = by_id.get(dependency)
                    if prerequisite is None:
                        waiting.append(f"선행 작업 {dependency} 확인 필요")
                    elif prerequisite["status"] != "done":
                        waiting.append(f"선행 작업 대기: {prerequisite['description']}")
                    elif prerequisite["verification"] == "reported":
                        waiting.append(f"선행 작업 완료 확인 필요: {prerequisite['description']}")
                row["waiting_reason"] = "; ".join(waiting)
                if row["status"] == "blocked" or waiting:
                    blocked.append(row)
                elif not row["snoozed"]:
                    next_actions.append(row)
            structured_sessions = {row[0] for row in self._db.execute("SELECT DISTINCT session_key FROM plans")}
            for session in self.list_sessions(limit=10000):
                if not session["enabled"] or session["session_key"] in structured_sessions:
                    continue
                suggested.append({**session, "id": "session:" + session["session_key"], "step_id": None,
                    "description": f"작업 이어서 진행: {session['title'] or session['session_id']}",
                    "status": "suggested", "verification": "reported", "kind": "session_continuation",
                    "priority": 0, "due_at": None, "snoozed_until": None, "goal": session["title"],
                    "evidence": {"session_key": session["session_key"]}, "source_ref": None, "waiting_reason": ""})
            def rank(row):
                return (0 if row.get("overdue") else 1, -row["priority"], row.get("due_at") or "9999",
                        0 if row["status"] == "in_progress" else 1, row["updated_at"], row["id"])
            next_actions.sort(key=rank)
            blocked.sort(key=rank)
            recent.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
            suggested.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
            projects: dict[str, dict] = {}
            for row in rows:
                key = row["workspace_path"] or "미분류"
                project = projects.setdefault(key, {"workspace_path": key, "total": 0,
                    "done": 0, "confirmed_done": 0, "reported_done": 0, "active": 0})
                project["total"] += 1
                if row["status"] == "done":
                    project["done"] += 1
                    project["reported_done" if row["verification"] == "reported" else "confirmed_done"] += 1
                elif row["status"] not in {"cancelled", "suggested"}:
                    project["active"] += 1
            stats = {"steps": len(rows), "next_actions": len(next_actions), "blocked": len(blocked),
                     "suggested": len(suggested), "done": sum(r["status"] == "done" for r in rows),
                     "confirmed_done": sum(r["status"] == "done" and r["verification"] != "reported" for r in rows),
                     "reported_done": sum(r["status"] == "done" and r["verification"] == "reported" for r in rows)}
            return {"next_actions": next_actions, "blocked": blocked, "recent": recent,
                    "suggested": suggested, "projects": list(projects.values()), "stats": stats}

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (key, _json(value)))

    def claim_reminder(self, step_id: str, kind: str, bucket: str) -> bool:
        with self._lock, self._db:
            return bool(self._db.execute("INSERT OR IGNORE INTO reminder_claims VALUES(?,?,?,?)",
                                        (step_id, kind, bucket, _time())).rowcount)
