"""
Event Bus for Jarvis Runtime
- 이벤트 중앙 전달 시스템
- 느슨한 결합 유지
"""

from dataclasses import dataclass, field
from typing import Dict, List, Callable, Any
from datetime import datetime
import threading
import uuid

@dataclass
class Event:
    type: str
    source: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[Event], None]]] = {}
        self._lock = threading.Lock()
        
    def subscribe(self, event_type: str, callback: Callable[[Event], None]):
        """이벤트 구독"""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)
            
    def unsubscribe(self, event_type: str, callback: Callable[[Event], None]):
        """이벤트 구독 취소"""
        with self._lock:
            if event_type in self._subscribers:
                if callback in self._subscribers[event_type]:
                    self._subscribers[event_type].remove(callback)
                    
    def publish(self, event: Event):
        """이벤트 발행 (비동기로 모든 구독자에게 전달)"""
        # 복사본을 만들어서 lock 밖에서 호출
        callbacks = []
        with self._lock:
            if event.type in self._subscribers:
                callbacks = self._subscribers[event.type].copy()
            # "*" type 구독자도 추가
            if "*" in self._subscribers:
                callbacks.extend(self._subscribers["*"].copy())
        # 콜백 호출
        for callback in callbacks:
            try:
                callback(event)
            except Exception as e:
                print(f"[EventBus] Callback error: {e}")
                
    def publish_sync(self, event: Event):
        """이벤트 발행 (동기로 모든 구독자에게 전달)"""
        callbacks = []
        with self._lock:
            if event.type in self._subscribers:
                callbacks = self._subscribers[event.type].copy()
            if "*" in self._subscribers:
                callbacks.extend(self._subscribers["*"].copy())
        for callback in callbacks:
            try:
                callback(event)
            except Exception as e:
                print(f"[EventBus] Callback error: {e}")

# Singleton
_event_bus = None

def get_event_bus() -> EventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
