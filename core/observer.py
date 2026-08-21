
from pathlib import Path
from typing import Callable, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
import threading
import subprocess
import json

# Event Bus import
try:
    from core.runtime.event_bus import get_event_bus, Event
    EVENT_BUS_AVAILABLE = True
except ImportError:
    EVENT_BUS_AVAILABLE = False

# Watchdog 라이브러리 import (선택사항)
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False


@dataclass
class FileChangeEvent:
    """파일 변경 이벤트 데이터 클래스"""
    event_type: str  # 'created', 'modified', 'deleted'
    file_path: str
    timestamp: datetime = field(default_factory=datetime.now)


class FileChangeHandler(FileSystemEventHandler):
    """파일 시스템 변경 이벤트 핸들러"""
    
    def __init__(self, callback: Callable[[FileChangeEvent], None]):
        super().__init__()
        self.callback = callback
        
    def on_modified(self, event: FileModifiedEvent):
        if not event.is_directory:
            self.callback(FileChangeEvent(
                event_type='modified',
                file_path=event.src_path
            ))
            
    def on_created(self, event: FileCreatedEvent):
        if not event.is_directory:
            self.callback(FileChangeEvent(
                event_type='created',
                file_path=event.src_path
            ))
            
    def on_deleted(self, event: FileDeletedEvent):
        if not event.is_directory:
            self.callback(FileChangeEvent(
                event_type='deleted',
                file_path=event.src_path
            ))


class ObserverLayer:
    """
    파일 시스템 변경을 감지하는 Observer Layer
    
    주요 기능:
    - 지정된 폴더의 파일 변경 감지
    - 이벤트 큐에 변경 사항 저장
    - 나중에 Knowledge Updater와 연결할 수 있음
    """
    
    def __init__(self):
        self.observer: Optional[Observer] = None
        self._running: bool = False
        self._event_history: List[FileChangeEvent] = []
        self._lock = threading.Lock()
        self._watched_paths: List[str] = []
        
    def is_available(self) -> bool:
        """Watchdog 라이브러리가 사용 가능한지 확인"""
        return WATCHDOG_AVAILABLE
        
    def start_watching(self, path: str, callback: Optional[Callable[[FileChangeEvent], None]] = None) -> str:
        """
        지정된 경로의 파일 변경 감지를 시작합니다.
        
        Args:
            path: 감지할 폴더 경로
            callback: 이벤트 발생 시 호출될 함수 (선택사항)
            
        Returns:
            성공/실패 메시지
        """
        if not self.is_available():
            return "오류: watchdog 라이브러리가 설치되지 않았습니다."
            
        if not Path(path).exists() or not Path(path).is_dir():
            return f"오류: 유효하지 않은 경로입니다: {path}"
            
        try:
            # 내부 콜백 함수 (이력 저장 + Event Bus 발행 + 사용자 콜백 호출)
            def internal_callback(event: FileChangeEvent):
                with self._lock:
                    self._event_history.append(event)
                    if len(self._event_history) > 100:
                        self._event_history.pop(0)
                # Event Bus로 발행
                if EVENT_BUS_AVAILABLE:
                    try:
                        bus = get_event_bus()
                        bus.publish(Event(
                            type=f"file_{event.event_type}",
                            source="observer_layer",
                            data={"path": event.file_path}
                        ))
                    except Exception as e:
                        pass
                if callback:
                    callback(event)
                    
            # Observer 초기화 (필요시)
            if self.observer is None:
                self.observer = Observer()
                
            # 핸들러 등록
            event_handler = FileChangeHandler(internal_callback)
            self.observer.schedule(event_handler, path, recursive=True)
            
            # 시작 (아직 실행 중이지 않은 경우)
            if not self._running:
                self.observer.start()
                self._running = True
                
            self._watched_paths.append(path)
            return f"파일 감지가 시작되었습니다: {path}"
            
        except Exception as e:
            return f"감지 시작 오류: {str(e)}"
            
    def stop_watching(self, path: Optional[str] = None) -> str:
        """
        파일 변경 감지를 중지합니다.
        
        Args:
            path: 특정 경로만 중지 (None이면 전체 중지)
            
        Returns:
            성공 메시지
        """
        if not self._running or self.observer is None:
            return "감지가 실행 중이지 않습니다."
            
        try:
            if path:
                # 특정 경로만 중지 (watchdog는 스케줄을 직접 제거하기 어려움)
                # 간단한 구현: 전체 중지 후 다른 경로 재시작
                return "특정 경로 중지는 현재 지원되지 않습니다. 전체 중지하려면 매개변수 없이 호출하세요."
            else:
                # 전체 중지
                self.observer.stop()
                self.observer.join(timeout=5)
                self.observer = None
                self._running = False
                self._watched_paths.clear()
                return "모든 파일 감지가 중지되었습니다."
                
        except Exception as e:
            return f"감지 중지 오류: {str(e)}"
            
    def get_event_history(self, limit: int = 20) -> List[FileChangeEvent]:
        """
        최근 파일 변경 이력을 가져옵니다.
        
        Args:
            limit: 가져올 이벤트 수 (기본 20개)
            
        Returns:
            FileChangeEvent 리스트
        """
        with self._lock:
            return list(self._event_history[-limit:])
            
    def clear_event_history(self) -> str:
        """이벤트 이력을 초기화합니다."""
        with self._lock:
            self._event_history.clear()
        return "이벤트 이력이 초기화되었습니다."
        
    def get_watched_paths(self) -> List[str]:
        """현재 감지 중인 경로 목록을 반환합니다."""
        return list(self._watched_paths)
    
    def check_git_status(self, repo_path: str) -> Optional[dict]:
        """Git 리포지토리의 상태를 확인하고 이벤트 발행"""
        try:
            if not Path(repo_path).exists():
                return None
            # git status
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return None
            changes = result.stdout.strip().split("\n") if result.stdout else []
            changes = [c.strip() for c in changes if c.strip()]
            # git log -1
            log_result = subprocess.run(
                ["git", "log", "-1", "--pretty=format:%H|%s|%an"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            last_commit = log_result.stdout.strip() if log_result.returncode == 0 else None
            event_data = {
                "repo_path": repo_path,
                "changes": changes,
                "last_commit": last_commit
            }
            if EVENT_BUS_AVAILABLE:
                try:
                    bus = get_event_bus()
                    bus.publish(Event(
                        type="git_status",
                        source="observer_layer",
                        data=event_data
                    ))
                except Exception as e:
                    pass
            return event_data
        except Exception:
            return None
    
    def check_processes(self) -> dict:
        """실행 중인 프로세스 확인 (Chrome, VSCode)"""
        try:
            # Windows에서 tasklist로 프로세스 확인
            result = subprocess.run(
                ["tasklist", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return {}
            lines = result.stdout.strip().split("\n")
            processes = []
            for line in lines[1:]:  # 첫 줄은 헤더
                try:
                    # CSV 파싱
                    parts = line.strip().split('","')
                    if len(parts) >= 1:
                        name = parts[0].strip('"').lower()
                        processes.append(name)
                except Exception:
                    pass
            chrome_running = any("chrome" in p for p in processes)
            vscode_running = any("code" in p for p in processes)
            event_data = {
                "chrome_running": chrome_running,
                "vscode_running": vscode_running,
                "processes": processes[:20]  # 최근 20개만
            }
            if EVENT_BUS_AVAILABLE:
                try:
                    bus = get_event_bus()
                    bus.publish(Event(
                        type="process_status",
                        source="observer_layer",
                        data=event_data
                    ))
                except Exception as e:
                    pass
            return event_data
        except Exception:
            return {}


# Singleton 인스턴스
_observer_layer = None


def get_observer_layer() -> ObserverLayer:
    """ObserverLayer 싱글톤 인스턴스 반환"""
    global _observer_layer
    if _observer_layer is None:
        _observer_layer = ObserverLayer()
    return _observer_layer

