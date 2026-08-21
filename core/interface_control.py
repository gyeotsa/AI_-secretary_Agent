"""Runtime bridge between declarative tools and the live Qt interface.

Plugins must not import ``main_qt`` or GUI classes.  The application registers
the concrete handlers during startup and plugins call this small boundary.
"""
from __future__ import annotations

from threading import RLock
from typing import Any, Callable


class InterfaceControlBridge:
    def __init__(self):
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._lock = RLock()

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        if not callable(handler):
            raise TypeError("인터페이스 제어 핸들러는 호출 가능해야 합니다.")
        with self._lock:
            self._handlers[str(name)] = handler

    def unregister(self, name: str) -> None:
        with self._lock:
            self._handlers.pop(str(name), None)

    def available(self, name: str) -> bool:
        with self._lock:
            return str(name) in self._handlers

    def call(self, name: str, *args, **kwargs):
        with self._lock:
            handler = self._handlers.get(str(name))
        if handler is None:
            raise RuntimeError("실행 중인 UI에 해당 제어 기능이 연결되지 않았습니다.")
        return handler(*args, **kwargs)


_bridge: InterfaceControlBridge | None = None


def get_interface_control_bridge() -> InterfaceControlBridge:
    global _bridge
    if _bridge is None:
        _bridge = InterfaceControlBridge()
    return _bridge
