"""검증 가능한 Windows 데스크톱 메신저 전송 런타임."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List
import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import subprocess
import time

from core.windows_automation import WindowInfo, WindowsAutomationRuntime


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


class DesktopMessagingRuntime:
    """Provider 설정과 창 제목 검증을 이용해 잘못된 수신자 전송을 차단한다."""

    VK = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "ENTER": 0x0D, "ESC": 0x1B,
          "F": 0x46, "A": 0x41}
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    def __init__(self, provider_path: str | None = None):
        path = Path(provider_path) if provider_path else (
            Path(__file__).resolve().parent.parent / "config" / "desktop_messaging_providers.json"
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.providers = payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            self.providers = {}
        self.automation = WindowsAutomationRuntime()

    def resolve_provider(self, value: str) -> tuple[str, Dict[str, Any]]:
        needle = re.sub(r"\s+", "", str(value).casefold())
        for provider_id, config in self.providers.items():
            aliases = [provider_id, *(config.get("aliases") or [])]
            if needle in {re.sub(r"\s+", "", str(item).casefold()) for item in aliases}:
                return provider_id, config
        raise LookupError(f"지원되는 데스크톱 메신저를 찾지 못했습니다: {value}")

    @staticmethod
    def _expanded_candidates(config: Dict[str, Any]) -> Iterable[Path]:
        for value in config.get("executable_candidates") or []:
            expanded = os.path.expandvars(str(value))
            if expanded and "%" not in expanded:
                yield Path(expanded)

    def _resolve_executable(self, config: Dict[str, Any]) -> str:
        for candidate in self._expanded_candidates(config):
            if candidate.is_file():
                return str(candidate.resolve())
        for executable_name in config.get("executable_names") or []:
            if resolved := shutil.which(str(executable_name)):
                return resolved
        # 기존 Windows 앱 검색/캐시를 단일 발견 경로로 재사용한다.
        try:
            from plugins.windows_control import WindowsControlPlugin
            for executable_name in config.get("executable_names") or []:
                if resolved := WindowsControlPlugin._resolve_target(str(executable_name)):
                    return resolved
        except Exception:
            pass
        return ""

    @staticmethod
    def _matching_windows(windows: Iterable[WindowInfo], titles: Iterable[str]) -> List[WindowInfo]:
        needles = [str(title).casefold().strip() for title in titles if str(title).strip()]
        return [window for window in windows if any(needle in window.title.casefold() for needle in needles)]

    def _wait_for_windows(self, predicate, timeout: float) -> List[WindowInfo]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [window for window in self.automation.list_windows() if predicate(window)]
            if found:
                return found
            time.sleep(0.2)
        return []

    @classmethod
    def _key(cls, virtual_key: int, up: bool = False) -> None:
        ctypes.windll.user32.keybd_event(virtual_key, 0, cls.KEYEVENTF_KEYUP if up else 0, 0)

    @classmethod
    def _hotkey(cls, keys: Iterable[str]) -> None:
        values = [cls.VK[str(key).upper()] for key in keys]
        for value in values:
            cls._key(value)
        for value in reversed(values):
            cls._key(value, True)
        time.sleep(0.08)

    @classmethod
    def _press(cls, key: str) -> None:
        value = cls.VK[key.upper()]
        cls._key(value)
        cls._key(value, True)
        time.sleep(0.08)

    @classmethod
    def _type_unicode(cls, text: str) -> None:
        """Clipboard를 덮어쓰지 않고 UTF-16 code unit을 SendInput으로 입력한다."""
        units = [int.from_bytes(text.encode("utf-16-le")[index:index + 2], "little")
                 for index in range(0, len(text.encode("utf-16-le")), 2)]
        inputs = []
        for unit in units:
            inputs.extend([
                _Input(type=cls.INPUT_KEYBOARD, ki=_KeyboardInput(
                    0, unit, cls.KEYEVENTF_UNICODE, 0, None)),
                _Input(type=cls.INPUT_KEYBOARD, ki=_KeyboardInput(
                    0, unit, cls.KEYEVENTF_UNICODE | cls.KEYEVENTF_KEYUP, 0, None)),
            ])
        if inputs:
            array = (_Input * len(inputs))(*inputs)
            sent = ctypes.windll.user32.SendInput(len(array), array, ctypes.sizeof(_Input))
            if sent != len(array):
                raise RuntimeError(f"Unicode 키 입력 일부만 전달됨: {sent}/{len(array)}")

    @staticmethod
    def _normal_title(value: str) -> str:
        text = re.sub(r"\s*[-–—|]\s*(?:카카오톡|kakaotalk).*$", "", value, flags=re.I)
        return re.sub(r"\s+", "", text).casefold()

    def send(self, provider: str, recipient: str, message: str,
             *, launch_timeout: float = 12.0, chat_timeout: float = 6.0) -> Dict[str, Any]:
        if os.name != "nt":
            raise OSError("데스크톱 메신저 전송은 Windows에서만 지원합니다.")
        provider_id, config = self.resolve_provider(provider)
        recipient, message = recipient.strip(), message.strip()
        if not recipient or not message:
            raise ValueError("수신자와 보낼 내용을 모두 입력해야 합니다.")

        windows = self.automation.list_windows()
        main_windows = self._matching_windows(windows, config.get("main_window_titles") or [])
        launched = False
        if not main_windows:
            executable = self._resolve_executable(config)
            if not executable:
                raise FileNotFoundError(f"{provider_id} 실행 파일을 찾지 못했습니다.")
            subprocess.Popen([executable], cwd=str(Path(executable).parent))
            launched = True
            main_windows = self._wait_for_windows(
                lambda item: bool(self._matching_windows([item], config.get("main_window_titles") or [])),
                launch_timeout,
            )
        if not main_windows:
            raise RuntimeError(f"{provider_id} 기본 창이 준비되지 않았습니다.")

        main = next((window for window in main_windows if window.foreground), main_windows[0])
        self.automation.focus(main.handle)
        self._hotkey(config.get("contact_search_hotkey") or ["CTRL", "F"])
        self._hotkey(["CTRL", "A"])
        self._type_unicode(recipient)
        time.sleep(0.8)
        self._press("ENTER")

        expected = self._normal_title(recipient)
        chat_windows = self._wait_for_windows(
            lambda item: item.process_id == main.process_id and self._normal_title(item.title) == expected,
            chat_timeout,
        )
        if len(chat_windows) != 1:
            self._press("ESC")
            raise RuntimeError(
                "수신자 이름과 정확히 일치하는 대화창을 하나로 검증하지 못해 전송을 중단했습니다."
            )
        chat = self.automation.focus(chat_windows[0].handle)
        foreground = next((
            item for item in self.automation.list_windows()
            if item.foreground and item.handle == chat.handle
        ), None)
        if foreground is None or foreground.handle != chat.handle or self._normal_title(foreground.title) != expected:
            raise RuntimeError("전송 직전 수신자 대화창 포커스 검증에 실패했습니다.")

        self._type_unicode(message)
        self._press("ENTER")
        return {
            "provider": provider_id,
            "recipient": recipient,
            "message_length": len(message),
            "window_title": chat.title,
            "window_handle": chat.handle,
            "process_id": chat.process_id,
            "launched": launched,
            "input_dispatched_at": datetime_now_iso(),
            "delivery_receipt_verified": False,
        }


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat()
