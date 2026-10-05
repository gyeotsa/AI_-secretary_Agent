"""Local-first work continuity: incremental receipts, plan state and reminders.

Reading source databases never constructs their runtime managers: doing so can
change task state. External sources are connected explicitly by the user. This
service never calls an LLM, resumes a task, or writes to an external AI's files.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any

from core.continuity_store import ContinuityStore


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (stamp if stamp.tzinfo else stamp.astimezone()).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(value or "null")
        return parsed if isinstance(parsed, type(default)) else default
    except (ValueError, TypeError):
        return default


@contextmanager
def _read_database(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        yield connection
    finally:
        connection.close()


class ContinuityService:
    """One synchronizer shared by the dashboard, application and chat queries."""

    def __init__(self, store=None, *, memory_path=None, dialogue_path=None,
                 plan_path=None, journal_path=None, notification_policy=None,
                 interval_seconds=15):
        from config import Config
        self.store = store or ContinuityStore()
        base = Path(Config.DB_PATH).parent
        self.paths = {
            "messages": Path(memory_path or Config.DB_PATH),
            "tasks": Path(dialogue_path or base / "dialogue_state.db"),
            "plans": Path(plan_path or base / "plan_runtime.db"),
            "actions": Path(journal_path or base / "action_journal.db"),
        }
        self.notification_policy = notification_policy
        if notification_policy is not None:
            notification_policy.continuity_validator = self._notification_valid
        self.interval_seconds = max(1, float(interval_seconds))
        self._sync_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.last_sync = {"status": "not_started", "events": 0, "errors": []}
        internal_path = str(self.paths["messages"].resolve())
        existing = next((s for s in self.store.sources()
                         if s["provider"] == "internal" and s["path"] == internal_path), None)
        self.internal_source = existing or self.store.add_source("internal", internal_path)

    @staticmethod
    def discover_sources():
        from core.continuity_adapters import discover_sources
        return discover_sources()

    def sources(self):
        rows = self.store.sources()
        for row in rows:
            detail = self.store.get_setting("source_report:" + row["source_id"], {})
            row["last_error"] = "; ".join(detail.get("errors", [])[-3:])
            row["pending_bytes"] = detail.get("pending_bytes", 0)
            row["skipped_records"] = detail.get("skipped_records", 0)
        return rows

    def add_source(self, provider, path):
        if provider not in {"codex", "claude_code"}:
            raise ValueError("Codex 또는 Claude Code 로그만 외부 소스로 연결할 수 있습니다.")
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("세션 로그가 저장된 폴더를 선택하세요.")
        with self._sync_lock:
            return self.store.add_source(provider, str(root))

    def set_source_enabled(self, source_id, enabled):
        with self._sync_lock:
            return self.store.set_source_enabled(source_id, bool(enabled))

    def delete_source(self, source_id, purge=True):
        with self._sync_lock:
            source = next((item for item in self.store.sources() if item["source_id"] == source_id), None)
            self.store.delete_source(source_id, purge=purge)
            if source and source["provider"] == "internal" and purge:
                # A forgotten internal archive stays paused across app restarts.
                self.store.add_source("internal", source["path"], enabled=False)

    def list_sessions(self, **kwargs):
        return self.store.list_sessions(**kwargs)

    def session_detail(self, session_key, limit=500):
        return self.store.session_detail(session_key, limit=limit)

    def dashboard(self):
        result = self.store.dashboard()
        result["sync"] = dict(self.last_sync)
        return result

    def update_step(self, step_id, **changes):
        with self._sync_lock:
            return self.store.update_step(step_id, **changes)

    def confirm_suggestion(self, session_key, description=None):
        return self.store.confirm_suggestion(session_key, description)

    def create_manual_plan(self, session_key, goal, steps):
        detail = self.store.session_detail(session_key)
        session = detail.get("session", {})
        if not session:
            raise ValueError("세션을 찾을 수 없습니다.")
        clean_steps = [{"id": str(index + 1), "description": str(item.get("description", "")).strip(),
                        "status": "pending", "verification": "user_confirmed"}
                       for index, item in enumerate(steps) if isinstance(item, dict)
                       and str(item.get("description", "")).strip()]
        if not str(goal).strip() or not clean_steps:
            raise ValueError("계획 이름과 한 개 이상의 단계를 입력하세요.")
        identity = uuid.uuid4().hex
        event = {"event_id": f"manual:{identity}", "provider": session["provider"],
                 "session_id": session["session_id"], "timestamp": datetime.now(timezone.utc).isoformat(),
                 "kind": "plan", "role": "user", "text": str(goal).strip(),
                 "workspace_path": session.get("workspace_path", ""), "source_ref": "user:manual_plan",
                 "payload": {"plan_id": f"manual:{identity}", "goal": str(goal).strip(),
                             "steps": clean_steps, "verification": "user_confirmed", "authoritative": True}}
        with self._sync_lock:
            self.store.ingest_batch(session["source_id"], f"manual:{identity}", [event], {})
        return self.store.session_detail(session_key)

    @property
    def notifications_enabled(self):
        return bool(self.store.get_setting("notifications_enabled", False))

    def set_notifications_enabled(self, enabled):
        self.store.set_setting("notifications_enabled", bool(enabled))

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="anis-continuity", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception as exc:
                self.last_sync = {"status": "error", "events": 0,
                                  "errors": [f"{type(exc).__name__}: {exc}"]}
            self._stop.wait(self.interval_seconds)

    def sync(self):
        # Manual refresh joins the same critical section as periodic ingestion.
        # Cursor writes and source toggles therefore cannot race ingestion.
        with self._sync_lock:
            report = {"status": "ok", "events": 0, "errors": [], "sources": [],
                      "synced_at": datetime.now(timezone.utc).isoformat()}
            for source in self.store.sources():
                if not source.get("enabled") or self._stop.is_set():
                    continue
                detail = {"source_id": source["source_id"], "events": 0, "errors": []}
                try:
                    if source["provider"] == "internal":
                        self._sync_internal(source, detail)
                    else:
                        self._sync_external(source, detail)
                except Exception as exc:
                    detail["errors"].append(f"{type(exc).__name__}: {exc}")
                report["events"] += detail["events"]
                report["errors"].extend(detail["errors"])
                report["sources"].append(detail)
                self.store.set_setting("source_report:" + source["source_id"], detail)
            report["pending_bytes"] = sum(s.get("pending_bytes", 0) for s in report["sources"])
            report["skipped_records"] = sum(s.get("skipped_records", 0) for s in report["sources"])
            if report["pending_bytes"]:
                report["status"] = "syncing"
            if report["errors"]:
                report["status"] = "partial"
            elif report["skipped_records"]:
                report["status"] = "partial"
                report["errors"].append(f"대형 또는 손상 레코드 {report['skipped_records']}개는 원본 파일에 남아 있습니다.")
            self.last_sync = report
            self._notify()
            return report

    def _ingest(self, source, path, events, cursor):
        from core.productization import SensitiveDataRedactor
        # Persist sanitized receipts and retain source locations for inspection.
        safe_events = [SensitiveDataRedactor.redact(event) for event in events]
        # Cursor hashes/IDs are control data, not text. A phone-like digit run
        # inside a digest must never change, or the next scan resets forever.
        safe_cursor = dict(cursor)
        if "state" in cursor:
            state = dict(cursor["state"])
            state["pending_calls"] = {
                call_id: {**request, "input": SensitiveDataRedactor.redact(request.get("input", {}))}
                for call_id, request in state.get("pending_calls", {}).items()
            }
            safe_cursor["state"] = state
        return self.store.ingest_batch(source["source_id"], str(path), safe_events, safe_cursor)

    def _sync_external(self, source, detail):
        from core.continuity_adapters import adapter_for, PARSER_VERSION
        adapter = adapter_for(source["provider"])
        if not Path(source["path"]).is_dir():
            raise FileNotFoundError(f"연결한 로그 폴더를 찾을 수 없습니다: {source['path']}")
        files = adapter.iter_files(Path(source["path"]))
        if not files:
            detail["errors"].append("연결한 폴더에 지원하는 JSONL 세션 로그가 없습니다.")
            return
        scan_key = "scan:" + source["source_id"]
        start = int(self.store.get_setting(scan_key, 0)) % len(files)
        ordered = files[start:] + files[:start]
        began, processed = time.monotonic(), 0
        for path in ordered:
            if self._stop.is_set():
                break
            try:
                cursor = self.store.get_cursor(source["source_id"], str(path))
                events, next_cursor, diagnostics = adapter.read_incremental(path, cursor)
                detail["events"] += self._ingest(source, path, events, next_cursor)
                detail["errors"].extend(str(item) for item in diagnostics)
            except (OSError, ValueError) as exc:
                detail["errors"].append(f"{path.name}: {type(exc).__name__}: {exc}")
            processed += 1
            if time.monotonic() - began >= 2:
                break
        self.store.set_setting(scan_key, (start + processed) % len(files))
        pending, skipped = 0, 0
        for path in files:
            cursor = self.store.get_cursor(source["source_id"], str(path))
            try:
                offset = int(cursor.get("offset", 0)) if cursor.get("parser_version") == PARSER_VERSION else 0
                pending += max(0, path.stat().st_size - offset)
            except OSError:
                continue
            skipped += int(cursor.get("skipped", 0))
        detail.update(files=len(files), pending_bytes=pending, skipped_records=skipped,
                      scan_complete=processed >= len(files))

    def _event(self, kind, session_id, stamp, payload, *, text="", role="system",
               workspace="", ref="", identity=""):
        return {"event_id": identity or _digest([kind, session_id, stamp, payload]),
                "provider": "internal", "session_id": session_id,
                "timestamp": _timestamp(stamp), "kind": kind, "role": role,
                "text": text, "workspace_path": workspace,
                "payload": payload, "source_ref": ref}

    def _sync_internal(self, source, detail):
        for name, reader in (("messages", self._read_messages), ("tasks", self._read_tasks),
                             ("actions", self._read_actions)):
            path = self.paths[name]
            if not path.is_file():
                continue
            try:
                cursor = self.store.get_cursor(source["source_id"], str(path))
                events, next_cursor = reader(path, cursor)
                detail["events"] += self._ingest(source, path, events, next_cursor)
            except (sqlite3.Error, OSError, ValueError) as exc:
                detail["errors"].append(f"{path.name}: {type(exc).__name__}: {exc}")

    def _read_messages(self, path, cursor):
        with _read_database(path) as conn:
            rows = conn.execute("SELECT e.*, s.title, s.workspace_namespace FROM episodes e "
                                "LEFT JOIN conversation_sessions s ON s.session_id=e.session_id "
                                "WHERE e.id>? ORDER BY e.id LIMIT 2000",
                                (int(cursor.get("last_id", 0)),)).fetchall()
        events = []
        for row in rows:
            metadata = _json(row["metadata"], {})
            workspace = metadata.get("workspace_namespace") or row["workspace_namespace"] or ""
            events.append(self._event("message", row["session_id"], row["timestamp"],
                {"title": row["title"] or "", "message_id": row["id"]},
                text=row["content"], role=row["role"], workspace=workspace,
                ref=f"{path}#episodes/{row['id']}", identity=f"episode:{row['id']}"))
        return events, {"last_id": rows[-1]["id"] if rows else cursor.get("last_id", 0)}

    def _read_tasks(self, path, cursor):
        with _read_database(path) as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM agent_tasks ORDER BY created_at,task_id")]
        plans, attempts = {}, {}
        if self.paths["plans"].is_file():
            with _read_database(self.paths["plans"]) as conn:
                plans = {row["plan_id"]: dict(row) for row in conn.execute("SELECT * FROM plan_runs")}
                for row in conn.execute("SELECT * FROM step_attempts ORDER BY id"):
                    attempts[(row["plan_id"], row["step_id"])] = dict(row)
        previous = cursor.get("tasks", {})
        seen, events = {}, []
        for row in rows:
            task_id = row["task_id"]
            plan = plans.get(row.get("plan_id"), {})
            signature = _digest([row, plan])
            seen[task_id] = signature
            if previous.get(task_id) == signature:
                continue
            data = _json(plan.get("payload"), {})
            steps = data.get("steps") or _json(row.get("plan"), [])
            if not steps:
                steps = [{"id": task_id, "description": row["goal"], "status": row["status"]}]
            normalized = []
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                step_id = str(step.get("id") or index + 1)
                attempt = attempts.get((row.get("plan_id"), step_id), {})
                receipt = _json(attempt.get("observation"), {})
                proof = receipt.get("evidence") or []
                status = step.get("status", "pending")
                verified = (attempt.get("result_status") == "succeeded" and bool(proof))
                if not row.get("plan_id"):
                    proof = _json(row.get("evidence"), [])
                    verified = (row.get("verification_status") in {"success", "succeeded", "verified", "passed", "completed"}
                                and bool(proof))
                if row["status"] in {"cancelled", "expired"} and status not in {"completed", "done"}:
                    status = "cancelled"
                elif row["status"] in {"awaiting_user", "awaiting_approval", "paused", "interrupted", "failed", "partial", "unverified"} and status not in {"completed", "done", "cancelled", "skipped"}:
                    status = "blocked"
                    proof = list(proof) + [{"kind": "task_blocker", "status": row["status"],
                                           "text": row.get("pending_question") or "작업 재개 또는 확인이 필요합니다."}]
                normalized.append({"id": step_id, "description": step.get("description") or step.get("step") or row["goal"],
                    "status": status, "dependencies": step.get("dependencies", []),
                    "verification": "verified" if verified else "reported", "evidence": proof})
            stamp = max(_timestamp(plan.get("updated_at")), _timestamp(row["updated_at"]))
            events.append(self._event("plan", row["session_id"], stamp,
                {"plan_id": task_id, "goal": row["goal"], "steps": normalized,
                 "verification": "reported", "authoritative": True,
                 "task_status": row["status"], "pending_question": row.get("pending_question", "")},
                text=row["goal"], workspace=row.get("workspace_path", ""),
                ref=f"{path}#agent_tasks/{task_id}", identity=f"task:{task_id}:{signature}"))
        return events, {"tasks": seen}

    def _read_actions(self, path, cursor):
        with _read_database(path) as conn:
            rows = conn.execute("SELECT rowid AS sequence,* FROM actions WHERE rowid>? ORDER BY rowid LIMIT 2000",
                                (int(cursor.get("last_id", 0)),)).fetchall()
        events = []
        for row in rows:
            data = _json(row["data"], {})
            events.append(self._event("tool", str(data.get("session_id") or "unassigned-tools"),
                row["timestamp"], {**data, "success": bool(row["success"]), "error": row["error"]},
                text=row["description"], role="tool", workspace=data.get("workspace_path", ""),
                ref=f"{path}#actions/{row['id']}", identity=f"action:{row['id']}"))
        return events, {"last_id": rows[-1]["sequence"] if rows else cursor.get("last_id", 0)}

    def _notify(self):
        policy = self.notification_policy
        if policy is None or not self.notifications_enabled or self._stop.is_set():
            return
        now = datetime.now(timezone.utc)
        board = self.store.dashboard()
        for step in (board["next_actions"] + board["blocked"])[:20]:
            if step.get("snoozed"):
                continue
            due = _timestamp(step.get("due_at"))
            updated = _timestamp(step.get("updated_at"))
            kind, reason = "", ""
            if due and datetime.fromisoformat(due) <= now:
                kind, reason = "deadline", "설정한 마감 시각이 지났습니다."
            elif updated and datetime.fromisoformat(updated) < now - timedelta(days=7):
                kind, reason = "stale", "7일 이상 진행 상태가 갱신되지 않았습니다."
            if not kind:
                continue
            key = f"continuity:{step['step_id']}:{kind}:{now.date()}"
            record = policy.store.enqueue(dedupe_key=key, severity="normal", title="통합 리마인더",
                message=f"{step.get('goal', '')}: {step['description']}", why=reason,
                evidence={"step_id": step["step_id"], "session_key": step["session_key"],
                          "continuity": True, "kind": kind,
                          "source_ref": step.get("source_ref", "")}, debounce_seconds=86400)
            if record and not policy.context.load().suppresses_noncritical:
                policy.notify(f"{record['message']}\n알림 이유: {reason}")
                policy.store.mark_delivered([record["notification_id"]])

    def _notification_valid(self, record):
        if not self.notifications_enabled or self._stop.is_set():
            return False
        try:
            step = self.store.get_step(record["evidence"]["step_id"])
        except KeyError:
            return False
        now = datetime.now(timezone.utc)
        if not step.get("enabled") or step["status"] in {"done", "cancelled", "suggested"}:
            return False
        snooze = _timestamp(step.get("snoozed_until"))
        if snooze and datetime.fromisoformat(snooze) > now:
            return False
        if record["evidence"].get("kind") == "deadline":
            due = _timestamp(step.get("due_at"))
            return bool(due and datetime.fromisoformat(due) <= now)
        updated = _timestamp(step.get("updated_at"))
        return bool(updated and datetime.fromisoformat(updated) < now - timedelta(days=7))

    def answer_now(self, limit=5):
        board = self.dashboard()
        items = board["next_actions"][:max(1, min(int(limit), 20))]
        if not items:
            text = "현재 바로 진행할 확정 작업이 없습니다. 통합 리마인더에서 대화의 작업 후보와 대기 항목을 확인하세요."
        else:
            lines = ["지금 이어서 할 작업입니다."]
            for index, item in enumerate(items, 1):
                why = item.get("reason") or item.get("ranking_reason") or "진행 가능한 미완료 단계"
                lines.append(f"{index}. {item.get('goal', '')} — {item['description']} ({why})")
            text = "\n".join(lines)
        if board.get("sync", {}).get("status") in {"partial", "error", "not_started", "syncing"}:
            text += "\n일부 로그가 아직 동기화되지 않았습니다. 통합 리마인더의 수집 상태를 확인하세요."
        return text


def is_continuity_query(text):
    """Exact convenience commands, without taking over general task control."""
    normalized = " ".join(str(text).strip().rstrip("?？.!。 ").split()).casefold()
    return normalized in {"지금 뭐 해야 돼", "지금 뭐 해야 해", "지금 무엇을 해야 해",
                          "다음 할 일", "통합 리마인더", "통합 리마인더 보여줘", "내가 어디까지 했지",
                          "/now", "/reminders"}
