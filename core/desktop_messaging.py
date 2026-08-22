"""검증 가능한 Windows 데스크톱 메신저 전송 런타임."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import subprocess
import time

from core.windows_automation import WindowInfo, WindowsAutomationRuntime


_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _InputUnion(ctypes.Union):
    # INPUT is a union of all three official Win32 payloads.  Defining only
    # KEYBDINPUT makes sizeof(INPUT) too small on 64-bit Windows and SendInput
    # rejects the entire array (the observed 0/N failure).
    _fields_ = [
        ("mi", _MouseInput),
        ("ki", _KeyboardInput),
        ("hi", _HardwareInput),
    ]


class _Input(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


_USER32 = None
if os.name == "nt":
    try:
        _USER32 = ctypes.WinDLL("user32", use_last_error=True)
        _USER32.SendInput.argtypes = (
            wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int,
        )
        _USER32.SendInput.restype = wintypes.UINT
    except (AttributeError, OSError):
        _USER32 = None


def _dispatch_inputs(array) -> int:
    """Call SendInput with the ABI-correct INPUT structure and useful errors."""
    if _USER32 is None:
        raise OSError("Windows SendInput API를 사용할 수 없습니다.")
    expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    actual_size = ctypes.sizeof(_Input)
    if actual_size != expected_size:
        raise RuntimeError(
            f"Win32 INPUT 구조 크기가 올바르지 않습니다: {actual_size} (예상 {expected_size})"
        )
    ctypes.set_last_error(0)
    sent = int(_USER32.SendInput(len(array), array, actual_size))
    if sent != len(array):
        error_code = ctypes.get_last_error()
        detail = ctypes.FormatError(error_code).strip() if error_code else "원인 코드 없음"
        raise RuntimeError(
            f"Unicode 키 입력 일부만 전달됨: {sent}/{len(array)} "
            f"(WinError {error_code}: {detail})"
        )
    return sent


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
        """Match only provider main windows, never ``<recipient> - KakaoTalk`` chats."""
        expected = {
            re.sub(r"\s+", "", str(title).casefold())
            for title in titles if str(title).strip()
        }
        return [window for window in windows
                if re.sub(r"\s+", "", window.title.casefold()) in expected]

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
        array = (_Input * 1)(_Input(
            type=cls.INPUT_KEYBOARD,
            ki=_KeyboardInput(
                virtual_key, 0, cls.KEYEVENTF_KEYUP if up else 0, 0, 0,
            ),
        ))
        _dispatch_inputs(array)

    @classmethod
    def _hotkey(cls, keys: Iterable[str]) -> None:
        values = [cls.VK[str(key).upper()] for key in keys]
        inputs = [
            _Input(type=cls.INPUT_KEYBOARD, ki=_KeyboardInput(value, 0, 0, 0, 0))
            for value in values
        ]
        inputs.extend(
            _Input(
                type=cls.INPUT_KEYBOARD,
                ki=_KeyboardInput(value, 0, cls.KEYEVENTF_KEYUP, 0, 0),
            )
            for value in reversed(values)
        )
        if inputs:
            _dispatch_inputs((_Input * len(inputs))(*inputs))
        time.sleep(0.08)

    @classmethod
    def _press(cls, key: str) -> None:
        value = cls.VK[key.upper()]
        inputs = [
            _Input(type=cls.INPUT_KEYBOARD, ki=_KeyboardInput(value, 0, 0, 0, 0)),
            _Input(
                type=cls.INPUT_KEYBOARD,
                ki=_KeyboardInput(value, 0, cls.KEYEVENTF_KEYUP, 0, 0),
            ),
        ]
        _dispatch_inputs((_Input * len(inputs))(*inputs))
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
                    0, unit, cls.KEYEVENTF_UNICODE, 0, 0)),
                _Input(type=cls.INPUT_KEYBOARD, ki=_KeyboardInput(
                    0, unit, cls.KEYEVENTF_UNICODE | cls.KEYEVENTF_KEYUP, 0, 0)),
            ])
        if inputs:
            array = (_Input * len(inputs))(*inputs)
            _dispatch_inputs(array)

    @staticmethod
    def _normal_title(value: str) -> str:
        text = re.sub(r"\s*[-–—|]\s*(?:카카오톡|kakaotalk).*$", "", value, flags=re.I)
        return re.sub(r"\s+", "", text).casefold()

    @staticmethod
    def _recipient_name_candidates(recipient: str) -> List[str]:
        """Return only the exact user-confirmed contact display name.

        Korean colloquial forms can be ambiguous: ``형택이`` may mean a
        contact named ``형택`` plus a particle, or it may be the literal contact
        name.  Guessing by removing the final syllable is unsafe for an external
        send, so any alias resolution must happen through an explicit contact ID
        or a separate user confirmation rather than here.
        """
        value = str(recipient or "").strip()
        return [value] if value else []

    def _forbidden_provider_windows(self, process_id: int, config: Dict[str, Any]) -> List[WindowInfo]:
        forbidden = [self._normal_title(item)
                     for item in config.get("forbidden_dialog_titles") or []]
        if not forbidden:
            return []
        return [window for window in self.automation.list_windows()
                if window.process_id == int(process_id)
                and any(item and item in self._normal_title(window.title) for item in forbidden)]

    def _close_forbidden_provider_windows(self, process_id: int, config: Dict[str, Any]) -> None:
        blocked = self._forbidden_provider_windows(process_id, config)
        for window in blocked:
            self.automation.close_window(window.handle)
        if blocked:
            titles = ", ".join(window.title for window in blocked)
            raise RuntimeError(
                f"메시지 검색 대신 금지된 카카오톡 창({titles})이 열려 입력 전에 중단했습니다."
            )

    def _focus_contact_search(self, main: WindowInfo, config: Dict[str, Any]) -> Dict[str, Any]:
        """Focus KakaoTalk's verified search edit without global shortcut guesses."""
        excluded = config.get("forbidden_control_names") or ["친구 추가", "ID로 추가", "연락처로 추가"]
        # The friends tab is optional.  Activating it first reduces ambiguity between
        # message-body search and contact search, but older builds may not expose it.
        try:
            self.automation.activate_accessibility_control(
                main.handle,
                names=config.get("contact_tab_accessibility_names") or ["친구"],
                control_types=["TabItem", "ListItem", "Button"],
                excluded_names=excluded, exact_name=True, invoke=True,
            )
            time.sleep(0.15)
        except (LookupError, RuntimeError):
            pass
        self._close_forbidden_provider_windows(main.process_id, config)

        names = config.get("contact_search_accessibility_names") or [
            "검색", "친구 검색", "친구검색", "이름 검색", "친구 이름 검색",
        ]
        id_keywords = config.get("contact_search_automation_id_keywords") or ["search"]
        selector = dict(
            names=names, automation_id_keywords=id_keywords,
            control_types=["Edit", "Button"], excluded_names=excluded,
            exact_name=False,
        )
        target = self.automation.activate_accessibility_control(
            main.handle, **selector, invoke=False,
        )
        if str(target.get("control_type", "")).casefold() == "edit":
            return target

        self.automation.activate_accessibility_control(main.handle, **selector, invoke=True)
        time.sleep(0.2)
        self._close_forbidden_provider_windows(main.process_id, config)
        try:
            return self.automation.activate_accessibility_control(
                main.handle, names=names, automation_id_keywords=id_keywords,
                control_types=["Edit"], excluded_names=excluded,
                exact_name=False, invoke=False,
            )
        except LookupError:
            # Some KakaoTalk versions expose the opened search field without a name.
            # Use it only when there is exactly one visible Edit control.
            return self.automation.activate_accessibility_control(
                main.handle, control_types=["Edit"], excluded_names=excluded,
                allow_type_only=True, require_unique=True, invoke=False,
            )

    def _activate_recipient_result(self, main: WindowInfo, recipient: str,
                                   config: Dict[str, Any]) -> Dict[str, Any]:
        excluded = config.get("forbidden_control_names") or ["친구 추가", "ID로 추가", "연락처로 추가"]
        last_candidate = recipient
        for candidate in self._recipient_name_candidates(recipient):
            last_candidate = candidate
            self._hotkey(["CTRL", "A"])
            self._type_unicode(candidate)
            time.sleep(0.8)
            self._close_forbidden_provider_windows(main.process_id, config)
            try:
                result = self.automation.activate_accessibility_control(
                    main.handle, names=[candidate], excluded_names=excluded,
                    exact_name=True, invoke=True,
                )
                if result.get("activation") != "invoke":
                    self._press("ENTER")
                return {**result, "matched_recipient": candidate}
            except LookupError:
                continue
        # The first keyboard-selected result may be opened, but the message is never
        # typed until a separate exact-title chat window is verified below.
        self._press("ENTER")
        return {"strategy": "verified-keyboard-selection", "activation": "enter",
                "matched_recipient": last_candidate}

    def _focus_message_input(self, chat: WindowInfo,
                             config: Dict[str, Any]) -> Dict[str, Any]:
        """Focus the chat composer after the exact recipient window is verified.

        Merely foregrounding a chat window does not guarantee that Unicode input is
        delivered to its composer; focus may remain on the message list.  Prefer a
        named/identified UIA Edit and accept a type-only fallback only when exactly
        one visible Edit exists in the already-verified recipient window.
        """
        names = config.get("message_input_accessibility_names") or [
            "메시지 입력", "채팅 입력", "대화 입력", "메시지를 입력하세요",
        ]
        id_keywords = config.get("message_input_automation_id_keywords") or [
            "message", "chat", "input", "edit",
        ]
        try:
            return self.automation.activate_accessibility_control(
                chat.handle, names=names, automation_id_keywords=id_keywords,
                control_types=["Edit"], exact_name=False, invoke=False,
            )
        except LookupError:
            return self.automation.activate_accessibility_control(
                chat.handle, control_types=["Edit"], allow_type_only=True,
                require_unique=True, invoke=False,
            )

    @staticmethod
    def _message_input_selector(config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "names": config.get("message_input_accessibility_names") or [
                "메시지 입력", "채팅 입력", "대화 입력", "메시지를 입력하세요",
            ],
            "automation_id_keywords": config.get(
                "message_input_automation_id_keywords"
            ) or ["message", "chat", "input", "edit"],
            "control_types": ["Edit"],
            "exact_name": False,
            # Multiple matching edits (search + composer, hidden legacy control,
            # etc.) are unsafe.  The runtime must prove one unambiguous composer.
            "require_unique": True,
        }

    @staticmethod
    def _control_identity(state: Dict[str, Any], window_handle: int) -> Dict[str, Any]:
        identity = state.get("control_identity")
        if isinstance(identity, dict) and identity:
            result = dict(identity)
            # Third-party/mock adapters written before the ``stable`` field may
            # still expose RuntimeId/native handle.  Derive, but never assume,
            # stability so a selector-only identity cannot authorize Enter.
            if "stable" not in result:
                result["stable"] = bool(
                    result.get("runtime_id") or result.get("native_handle")
                )
            return result
        # Compatibility metadata is useful for diagnostics, but is deliberately
        # marked unstable: selectors/bounds cannot prove that the same UIA element
        # survived between set/read operations.
        payload = {
            "window_handle": int(window_handle),
            "name": str(state.get("name", "") or ""),
            "automation_id": str(state.get("automation_id", "") or ""),
            "control_type": str(state.get("control_type", "") or ""),
            "rectangle": list(state.get("rectangle") or []),
            "stable": False,
            "identity_basis": "selector_fallback",
        }
        payload["token"] = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"))
        return payload

    @staticmethod
    def _identity_token(identity: Dict[str, Any]) -> str:
        token = str(identity.get("token", "") or "")
        if token:
            return token
        return json.dumps({key: value for key, value in identity.items() if key != "token"},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _set_and_verify_message_input(
        self, chat: WindowInfo, config: Dict[str, Any], message: str,
    ) -> Dict[str, Any]:
        """Put text in the verified composer and prove that the value matches."""
        selector = self._message_input_selector(config)
        try:
            state = self.automation.set_accessibility_text(
                chat.handle, message, **selector,
            )
        except LookupError:
            # Some KakaoTalk releases expose an unnamed Edit.  It is safe only when
            # the exact recipient chat has one and only one visible Edit control.
            state = self.automation.set_accessibility_text(
                chat.handle, message, control_types=["Edit"],
                allow_type_only=True, require_unique=True,
            )
        identity = self._control_identity(state, chat.handle)
        if not state.get("value_verified"):
            raise RuntimeError("메시지 입력창 본문을 정확히 다시 읽어 검증하지 못했습니다.")
        if not identity.get("stable"):
            raise RuntimeError(
                "메시지 입력창의 안정적인 UIA RuntimeId 또는 native handle을 확인하지 못했습니다."
            )
        return {**state, "control_identity": identity}

    def _read_same_message_input(
        self, chat: WindowInfo, config: Dict[str, Any], identity: Dict[str, Any],
        *, require_keyboard_focus: bool = False,
    ) -> Dict[str, Any]:
        selector = self._message_input_selector(config)
        try:
            state = self.automation.read_accessibility_text(
                chat.handle, expected_identity=identity,
                require_keyboard_focus=require_keyboard_focus, **selector,
            )
        except LookupError:
            state = self.automation.read_accessibility_text(
                chat.handle, control_types=["Edit"], allow_type_only=True,
                require_unique=True, expected_identity=identity,
                require_keyboard_focus=require_keyboard_focus,
            )
        actual_identity = self._control_identity(state, chat.handle)
        if self._identity_token(actual_identity) != self._identity_token(identity):
            raise RuntimeError("메시지 입력 Control이 전송 과정에서 다른 UIA 요소로 바뀌었습니다.")
        if require_keyboard_focus and not state.get("keyboard_focus_verified"):
            raise RuntimeError("Enter 직전 동일 메시지 입력창의 키보드 포커스를 검증하지 못했습니다.")
        return {**state, "control_identity": actual_identity}

    def _wait_until_message_input_clears(
        self, chat: WindowInfo, config: Dict[str, Any], identity: Dict[str, Any], timeout: float,
    ) -> bool:
        """Verify that KakaoTalk accepted Enter by observing the composer clear."""
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            state = self._read_same_message_input(chat, config, identity)
            if not str(state.get("value", "")):
                return True
            time.sleep(0.08)
        return False

    def _verify_exact_chat_foreground(
        self, chat: WindowInfo, expected_titles: Set[str],
    ) -> WindowInfo:
        foreground = [item for item in self.automation.list_windows()
                      if item.foreground and item.handle == chat.handle
                      and item.process_id == chat.process_id
                      and self._normal_title(item.title) in expected_titles]
        if len(foreground) != 1:
            raise RuntimeError("전송 직전 수신자 대화창 포커스 검증에 실패했습니다.")
        return foreground[0]

    @staticmethod
    def _occurrence_token(occurrence: Dict[str, Any]) -> str:
        token = str(occurrence.get("identity_token", "") or "")
        if token:
            return token
        identity = occurrence.get("control_identity")
        if isinstance(identity, dict):
            return DesktopMessagingRuntime._identity_token(identity)
        return ""

    @staticmethod
    def _is_outgoing_occurrence(occurrence: Dict[str, Any], config: Dict[str, Any]) -> bool:
        semantic_outgoing = occurrence.get("outgoing") is True
        direction = str(occurrence.get("direction", "") or "").casefold()
        if direction in {"outgoing", "sent", "mine", "self"}:
            semantic_outgoing = True
        keywords = [str(item).casefold() for item in
                    config.get("outgoing_message_direction_keywords") or []]
        metadata_values = [
            occurrence.get("automation_id", ""), occurrence.get("class_name", ""),
            *(occurrence.get("ancestor_names") or []),
            *(occurrence.get("ancestor_automation_ids") or []),
            *(occurrence.get("ancestor_class_names") or []),
        ]
        haystack = " ".join(str(item).casefold() for item in metadata_values)
        if keywords and any(keyword and keyword in haystack for keyword in keywords):
            semantic_outgoing = True
        # A right-aligned text node is not proof that KakaoTalk created an
        # outgoing message bubble.  Production UIA trees can expose timestamps,
        # buttons, or virtualized stale nodes on the same side.  Require an
        # explicit semantic outgoing marker and use geometry only as a sanity
        # check when it is available.
        if not semantic_outgoing:
            return False
        try:
            x_ratio = float(occurrence.get("x_ratio"))
            minimum = float(config.get("outgoing_message_min_x_ratio", 0.55))
        except (TypeError, ValueError):
            return True
        return x_ratio >= minimum

    def _outgoing_message_occurrences(
        self, chat: WindowInfo, config: Dict[str, Any], message: str,
    ) -> List[Dict[str, Any]]:
        try:
            occurrences = self.automation.accessibility_text_occurrences(
                chat.handle, message,
                control_types=config.get("outgoing_message_control_types") or
                ["Text", "ListItem", "Document"],
            )
        except (AttributeError, LookupError, RuntimeError):
            return []
        return [item for item in occurrences
                if self._is_outgoing_occurrence(item, config)
                and self._occurrence_token(item)]

    def _wait_for_new_outgoing_message(
        self, chat: WindowInfo, config: Dict[str, Any], message: str,
        previous_tokens: Set[str], previous_count: int, timeout: float,
    ) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + max(0.2, float(timeout))
        while time.monotonic() < deadline:
            occurrences = self._outgoing_message_occurrences(chat, config, message)
            # UIA RuntimeIds may be regenerated when KakaoTalk virtualizes an
            # existing bubble.  A changed token alone is therefore insufficient;
            # the number of exact outgoing occurrences must also increase.
            if len(occurrences) <= previous_count:
                time.sleep(0.08)
                continue
            for occurrence in occurrences:
                if self._occurrence_token(occurrence) not in previous_tokens:
                    return occurrence
            time.sleep(0.08)
        return None

    def send(self, provider: str, recipient: str, message: str,
             *, launch_timeout: float = 12.0, chat_timeout: float = 6.0,
             send_verify_timeout: float = 2.0) -> Dict[str, Any]:
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
        self._close_forbidden_provider_windows(main.process_id, config)
        self.automation.focus(main.handle)
        self._focus_contact_search(main, config)
        self._close_forbidden_provider_windows(main.process_id, config)
        self._activate_recipient_result(main, recipient, config)
        self._close_forbidden_provider_windows(main.process_id, config)

        expected_titles = {
            self._normal_title(item) for item in self._recipient_name_candidates(recipient)
        }
        chat_windows = self._wait_for_windows(
            lambda item: item.process_id == main.process_id
            and self._normal_title(item.title) in expected_titles,
            chat_timeout,
        )
        if len(chat_windows) != 1:
            self._press("ESC")
            raise RuntimeError(
                "수신자 이름과 정확히 일치하는 대화창을 하나로 검증하지 못해 전송을 중단했습니다."
            )
        chat = self.automation.focus(chat_windows[0].handle)
        self._verify_exact_chat_foreground(chat, expected_titles)

        # A foreground chat window is not proof that keyboard input will reach its
        # composer.  Fail closed unless the exact Unicode value can be set and read
        # back through the identified UIA Edit control.
        message_input_focus = self._set_and_verify_message_input(
            chat, config, message,
        )
        composer_identity = message_input_focus["control_identity"]
        previous_occurrences = self._outgoing_message_occurrences(
            chat, config, message,
        )
        previous_bubbles = {
            self._occurrence_token(item) for item in previous_occurrences
        }
        # Nothing that can move focus is allowed between these two checks and Enter.
        self._verify_exact_chat_foreground(chat, expected_titles)
        composer_state = self._read_same_message_input(
            chat, config, composer_identity, require_keyboard_focus=True,
        )
        if str(composer_state.get("value", "")) != message:
            raise RuntimeError("Enter 직전 동일 입력창의 본문이 요청한 메시지와 일치하지 않습니다.")
        # Reading a UIA control can pump messages and another window may steal
        # foreground focus.  Re-check after the read, immediately before Enter.
        self._verify_exact_chat_foreground(chat, expected_titles)
        self._press("ENTER")
        if not self._wait_until_message_input_clears(
            chat, config, composer_identity, send_verify_timeout,
        ):
            raise RuntimeError(
                "Enter 입력 뒤 메시지 입력창이 비워지지 않아 실제 전송을 확인하지 못했습니다."
            )
        outgoing_bubble = self._wait_for_new_outgoing_message(
            chat, config, message, previous_bubbles,
            len(previous_occurrences), send_verify_timeout,
        )
        send_verified = outgoing_bubble is not None
        return {
            "provider": provider_id,
            "recipient": recipient,
            "message_length": len(message),
            "window_title": chat.title,
            "window_handle": chat.handle,
            "process_id": chat.process_id,
            "launched": launched,
            "message_input_focus": message_input_focus,
            "input_dispatched_at": datetime_now_iso(),
            "send_accepted_verified": send_verified,
            "outgoing_message_verified": send_verified,
            "outgoing_message_evidence": outgoing_bubble or {},
            "send_verification": (
                "new_exact_outgoing_message_bubble"
                if send_verified else "unverified_no_new_exact_outgoing_message_bubble"
            ),
            "verification_status": "verified" if send_verified else "unverified",
            "unverified_reason": "" if send_verified else (
                "Enter 뒤 입력창은 비워졌지만 동일 본문의 새 보낸 메시지 말풍선을 UIA에서 확인하지 못했습니다."
            ),
            "delivery_receipt_verified": False,
        }


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat()
