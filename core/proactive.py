"""Explainable proactive notification policy with suppression and digest delivery."""
from pathlib import Path
from typing import Callable, Dict, Optional
import threading
import time

from core.proactive_runtime import (InterruptionContextManager, ProactiveStore)
from core.runtime.event_bus import Event, get_event_bus


class ProactiveNotificationPolicy:
    IMPORTANT_SUFFIXES = {".py", ".md", ".txt", ".docx", ".xlsx", ".pptx", ".pdf"}
    IGNORED_PARTS = {".git", ".idea", ".vscode", "__pycache__", ".pytest_cache", ".pytest-tmp", ".venv"}

    def __init__(self, notify: Callable[[str], None], debounce_seconds: float = 30.0,
                 clock: Callable[[], float] = time.monotonic, store: Optional[ProactiveStore] = None,
                 context: Optional[InterruptionContextManager] = None):
        self.notify = notify
        self.debounce_seconds = debounce_seconds
        self.clock = clock
        self.store = store or ProactiveStore()
        self.context = context or InterruptionContextManager()
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._bus = None

    def start(self):
        if self._bus is not None:
            return
        self._bus = get_event_bus()
        for event_type in ("file_created", "file_modified", "file_deleted", "automation_failed",
                           "security_alert", "task_completed", "interruption_context_changed"):
            self._bus.subscribe(event_type, self.handle_event)

    def stop(self):
        if self._bus is None:
            return
        for event_type in ("file_created", "file_modified", "file_deleted", "automation_failed",
                           "security_alert", "task_completed", "interruption_context_changed"):
            self._bus.unsubscribe(event_type, self.handle_event)
        self._bus = None

    def handle_event(self, event: Event) -> Optional[str]:
        if event.type == "interruption_context_changed":
            return self.flush_digest()
        if event.type.startswith("file_"):
            return self._handle_file_event(event)
        severity = str(event.data.get("severity") or
                       ("critical" if event.type == "security_alert" else "high" if event.type == "automation_failed" else "normal"))
        title = str(event.data.get("title") or event.type.replace("_", " "))
        message = str(event.data.get("message") or title)
        why = str(event.data.get("why") or f"{event.type} 이벤트가 발생했습니다.")
        key = str(event.data.get("dedupe_key") or f"{event.type}:{title}")
        return self._queue_and_maybe_deliver(key, severity, title, message, why,
                                             {"event_id": event.id, "source": event.source, **event.data})

    def _handle_file_event(self, event: Event) -> Optional[str]:
        raw_path = event.data.get("path")
        if not raw_path:
            return None
        path = Path(raw_path)
        if path.suffix.lower() not in self.IMPORTANT_SUFFIXES or any(part in self.IGNORED_PARTS for part in path.parts):
            return None
        key = str(path.resolve())
        now = self.clock()
        with self._lock:
            if now - self._last_seen.get(key, float("-inf")) < self.debounce_seconds:
                return None
            self._last_seen[key] = now
        action = {"file_created": "생성", "file_modified": "변경", "file_deleted": "삭제"}.get(event.type)
        if not action:
            return None
        message = f"보스, 중요 파일이 {action}되었습니다: {path.name}"
        why = f"감시 중인 업무 파일 형식({path.suffix.lower()})에 {action} 이벤트가 발생했습니다."
        return self._queue_and_maybe_deliver(key, str(event.data.get("severity", "normal")),
                                             f"중요 파일 {action}", message, why,
                                             {"event_id": event.id, "path": str(path), "action": action})

    def _queue_and_maybe_deliver(self, key: str, severity: str, title: str, message: str,
                                 why: str, evidence: dict) -> Optional[str]:
        record = self.store.enqueue(dedupe_key=key, severity=severity, title=title, message=message,
                                    why=why, evidence=evidence, debounce_seconds=0)
        if record is None:
            return None
        context = self.context.load()
        if context.suppresses_noncritical and severity != "critical":
            return f"보류됨: {message} (이유: {', '.join(context.reasons())})"
        rendered = f"{message}\n알림 이유: {why}"
        self.notify(rendered)
        self.store.mark_delivered([record["notification_id"]])
        return rendered

    def flush_digest(self, limit: int = 20, force: bool = False) -> Optional[str]:
        context = self.context.load()
        if context.suppresses_noncritical and not force:
            return None
        records = self.store.pending(limit)
        if not records:
            return None
        lines = [f"보류된 알림 {len(records)}건을 묶어서 알려드립니다."]
        for item in records:
            lines.append(f"- [{item['severity']}] {item['message']} — {item['why']}")
        message = "\n".join(lines)
        self.notify(message)
        self.store.mark_delivered([item["notification_id"] for item in records])
        return message
