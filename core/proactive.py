"""런타임 이벤트를 사용자에게 보고할지 결정하는 선제 알림 정책."""
from pathlib import Path
from typing import Callable, Dict, Optional
import threading
import time

from core.runtime.event_bus import Event, get_event_bus


class ProactiveNotificationPolicy:
    IMPORTANT_SUFFIXES = {".py", ".md", ".txt", ".docx", ".xlsx", ".pptx", ".pdf"}
    IGNORED_PARTS = {".git", ".idea", ".vscode", "__pycache__", ".pytest_cache", ".pytest-tmp", ".venv"}

    def __init__(self, notify: Callable[[str], None], debounce_seconds: float = 30.0,
                 clock: Callable[[], float] = time.monotonic):
        self.notify = notify
        self.debounce_seconds = debounce_seconds
        self.clock = clock
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._bus = None

    def start(self):
        if self._bus is not None:
            return
        self._bus = get_event_bus()
        for event_type in ("file_created", "file_modified", "file_deleted"):
            self._bus.subscribe(event_type, self.handle_event)

    def stop(self):
        if self._bus is None:
            return
        for event_type in ("file_created", "file_modified", "file_deleted"):
            self._bus.unsubscribe(event_type, self.handle_event)
        self._bus = None

    def handle_event(self, event: Event) -> Optional[str]:
        raw_path = event.data.get("path")
        if not raw_path:
            return None
        path = Path(raw_path)
        if path.suffix.lower() not in self.IMPORTANT_SUFFIXES or any(part in self.IGNORED_PARTS for part in path.parts):
            return None

        # 생성 직후 수정 이벤트가 연달아 오는 일반적인 편집기 동작도 한 번으로 묶는다.
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
        self.notify(message)
        return message
