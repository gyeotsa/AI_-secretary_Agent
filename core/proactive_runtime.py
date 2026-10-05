"""Persistent interruption context, explainable notifications, and proactive proposals."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Config


@dataclass
class InterruptionContext:
    focus_mode: bool = False
    meeting: bool = False
    fullscreen: bool = False
    do_not_disturb: bool = False
    updated_at: str = ""

    @property
    def suppresses_noncritical(self) -> bool:
        return self.focus_mode or self.meeting or self.fullscreen or self.do_not_disturb

    def reasons(self) -> List[str]:
        labels = {"focus_mode": "집중 모드", "meeting": "회의 중",
                  "fullscreen": "전체화면", "do_not_disturb": "방해 금지"}
        return [label for key, label in labels.items() if getattr(self, key)]


class InterruptionContextManager:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or Path(Config.DB_PATH).with_name("interruption_context.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self, detect_fullscreen: bool = True) -> InterruptionContext:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            data = {}
        context = InterruptionContext(**{key: bool(data.get(key, False)) for key in
            ("focus_mode", "meeting", "fullscreen", "do_not_disturb")},
            updated_at=str(data.get("updated_at", "")))
        if detect_fullscreen:
            detected = self._detect_fullscreen()
            if detected is not None:
                context.fullscreen = detected
        return context

    def update(self, **changes: bool) -> InterruptionContext:
        allowed = {"focus_mode", "meeting", "fullscreen", "do_not_disturb"}
        if not changes or any(key not in allowed for key in changes):
            raise ValueError("지원하지 않는 방해 제어 상태입니다.")
        with self._lock:
            context = self.load(detect_fullscreen=False)
            for key, value in changes.items():
                setattr(context, key, bool(value))
            context.updated_at = datetime.now().astimezone().isoformat()
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(asdict(context), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        return context

    @staticmethod
    def _detect_fullscreen() -> Optional[bool]:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False
            monitor = user32.MonitorFromWindow(hwnd, 2)
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
            info = MonitorInfo(ctypes.sizeof(MonitorInfo))
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return False
            return (rect.left <= info.rcMonitor.left and rect.top <= info.rcMonitor.top and
                    rect.right >= info.rcMonitor.right and rect.bottom >= info.rcMonitor.bottom)
        except Exception:
            return None


class ProactiveStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or Path(Config.DB_PATH).with_name("proactive.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS notifications(
                notification_id TEXT PRIMARY KEY, dedupe_key TEXT, severity TEXT, title TEXT,
                message TEXT, why TEXT, evidence TEXT, status TEXT, created_at REAL, delivered_at REAL)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_notification_status
                            ON notifications(status, created_at)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS proposals(
                proposal_id TEXT PRIMARY KEY, description TEXT, tool_name TEXT, tool_input TEXT,
                reason TEXT, status TEXT, created_at REAL, approved_at REAL)""")

    @contextmanager
    def _session(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def enqueue(self, *, dedupe_key: str, severity: str, title: str, message: str,
                why: str, evidence: Dict[str, Any], debounce_seconds: float) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._session() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM notifications WHERE dedupe_key=? AND created_at>=? LIMIT 1",
                (dedupe_key, now - debounce_seconds)).fetchone()
            if duplicate:
                return None
            notification_id = uuid.uuid4().hex
            conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (notification_id, dedupe_key, severity, title, message, why,
                          json.dumps(evidence, ensure_ascii=False), "pending", now, 0.0))
        return self.get_notification(notification_id)

    def get_notification(self, notification_id: str) -> Optional[Dict[str, Any]]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM notifications WHERE notification_id=?",
                               (notification_id,)).fetchone()
        return self._notification(row) if row else None

    def pending(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM notifications WHERE status='pending' "
                                "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, created_at LIMIT ?",
                                (max(1, int(limit)),)).fetchall()
        return [self._notification(row) for row in rows]

    def mark_delivered(self, ids: List[str]) -> None:
        if not ids:
            return
        with self._session() as conn:
            conn.executemany("UPDATE notifications SET status='delivered',delivered_at=? "
                             "WHERE notification_id=? AND status='pending'",
                             [(time.time(), item) for item in ids])

    def dismiss(self, ids: List[str]) -> None:
        """Retire queued reminders whose source task is no longer actionable."""
        with self._session() as conn:
            conn.executemany("UPDATE notifications SET status='dismissed' WHERE notification_id=? AND status='pending'",
                             [(item,) for item in ids])

    @staticmethod
    def _notification(row) -> Dict[str, Any]:
        value = dict(row)
        value["evidence"] = json.loads(value["evidence"] or "{}")
        return value

    def propose(self, description: str, tool_name: str, tool_input: Dict[str, Any], reason: str) -> Dict[str, Any]:
        proposal_id, now = uuid.uuid4().hex, time.time()
        with self._session() as conn:
            conn.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'proposed',?,0)",
                         (proposal_id, description, tool_name,
                          json.dumps(tool_input, ensure_ascii=False), reason, now))
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["tool_input"] = json.loads(result["tool_input"])
        return result

    def approve(self, proposal_id: str) -> Dict[str, Any]:
        with self._session() as conn:
            cursor = conn.execute("UPDATE proposals SET status='approved',approved_at=? "
                                  "WHERE proposal_id=? AND status='proposed'", (time.time(), proposal_id))
            if cursor.rowcount != 1:
                raise ValueError("제안이 없거나 이미 처리되었습니다.")
        return self.get_proposal(proposal_id)
