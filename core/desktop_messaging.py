"""수신자 대화창을 검증하는 Windows 데스크톱 메신저 읽기 런타임."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Set
import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import subprocess
import time

from core.windows_automation import WindowInfo, WindowsAutomationRuntime, windows_com_apartment


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
    """Provider 설정과 창 제목 검증을 이용해 요청한 대화방의 메시지를 읽는다."""

    VK = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "ENTER": 0x0D, "ESC": 0x1B,
          "BACKSPACE": 0x08, "TAB": 0x09, "F": 0x46, "A": 0x41, "V": 0x56}
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
        """Type through SendInput without replacing the user's clipboard."""
        encoded = text.encode("utf-16-le")
        inputs = []
        for offset in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[offset:offset + 2], "little")
            inputs.extend([
                _Input(type=cls.INPUT_KEYBOARD, ki=_KeyboardInput(
                    0, unit, cls.KEYEVENTF_UNICODE, 0, 0)),
                _Input(type=cls.INPUT_KEYBOARD, ki=_KeyboardInput(
                    0, unit, cls.KEYEVENTF_UNICODE | cls.KEYEVENTF_KEYUP, 0, 0)),
            ])
        if inputs:
            _dispatch_inputs((_Input * len(inputs))(*inputs))


    @staticmethod
    def _normal_title(value: str) -> str:
        text = re.sub(r"\s+[-–—|]\s*(?:카카오톡|kakaotalk)\s*$", "", value, flags=re.I)
        return text.strip().casefold()

    @staticmethod
    def _verify_provider_process(process_id: int, config: Dict[str, Any]) -> None:
        import psutil

        try:
            executable = Path(psutil.Process(process_id).exe()).name.casefold()
        except (psutil.Error, OSError) as exc:
            raise RuntimeError("메신저 창의 실행 파일을 확인하지 못했습니다.") from exc
        expected = {str(name).casefold() for name in config.get("executable_names") or []}
        if not executable or executable not in expected:
            raise RuntimeError("창 제목과 실제 메신저 프로세스가 일치하지 않아 작업을 중단했습니다.")

    @staticmethod
    def _recipient_name_candidates(recipient: str) -> List[str]:
        """Return only the exact user-confirmed contact display name.

        Korean colloquial forms can be ambiguous: ``형택이`` may mean a
        contact named ``형택`` plus a particle, or it may be the literal contact
        name. Guessing by removing the final syllable can open the wrong chat,
        so any alias resolution must happen through an explicit contact ID
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
        view_prefix = config.get("contact_view_accessibility_name_prefix")
        if view_prefix:
            for _ in range(3):
                controls = self.automation.accessibility_controls(main.handle)
                if any(str(item.get("name", "")).startswith(view_prefix) for item in controls):
                    break
                self._verify_provider_foreground(main)
                self._hotkey(["CTRL", "SHIFT", "TAB"])
                time.sleep(0.2)
            else:
                raise LookupError("친구 목록을 확인하지 못했습니다.")
            self._verify_provider_foreground(main)
            self._hotkey(["CTRL", "F"])
            for _ in range(10):
                time.sleep(0.1)
                self._close_forbidden_provider_windows(main.process_id, config)
                self._verify_provider_foreground(main)
                try:
                    return self.automation.activate_accessibility_control(
                        main.handle, **self._contact_search_selector(config),
                        invoke=False,
                        require_keyboard_focus=True, require_foreground=True,
                    )
                except LookupError:
                    continue
            raise LookupError("친구 검색 입력창을 확인하지 못했습니다.")
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

        selector = self._contact_search_selector(config)
        names = selector["names"]
        id_keywords = selector["automation_id_keywords"]
        button_selector = dict(
            names=names, automation_id_keywords=id_keywords,
            control_types=["Edit", "Button"], excluded_names=excluded,
            exact_name=False,
        )
        target = self.automation.activate_accessibility_control(
            main.handle, **button_selector, invoke=False,
            require_keyboard_focus=False, require_foreground=True,
        )
        if str(target.get("control_type", "")).casefold() == "edit":
            return self.automation.activate_accessibility_control(
                main.handle, **selector, invoke=False,
                expected_identity=self._control_identity(target, main.handle),
                require_keyboard_focus=True, require_foreground=True,
            )

        button_identity = self._control_identity(target, main.handle)
        self.automation.activate_accessibility_control(
            main.handle, **button_selector, invoke=True,
            expected_identity=button_identity, require_foreground=True,
        )
        time.sleep(0.2)
        self._close_forbidden_provider_windows(main.process_id, config)
        try:
            return self.automation.activate_accessibility_control(
                main.handle, **selector, invoke=False,
                require_keyboard_focus=True, require_foreground=True,
            )
        except LookupError:
            # Some KakaoTalk versions expose the opened search field without a name.
            # Use it only when there is exactly one visible Edit control.
            return self.automation.activate_accessibility_control(
                main.handle, control_types=["Edit"], excluded_names=excluded,
                allow_type_only=True, require_unique=True, invoke=False,
                require_keyboard_focus=True, require_foreground=True,
            )

    @staticmethod
    def _contact_search_selector(config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "names": config.get("contact_search_accessibility_names") or [
                "검색", "친구 검색", "친구검색", "이름 검색", "친구 이름 검색",
            ],
            "automation_id_keywords": (
                config.get("contact_search_automation_id_keywords") or ["search"]
            ),
            "control_types": ["Edit"],
            "excluded_names": config.get("forbidden_control_names") or [
                "친구 추가", "ID로 추가", "연락처로 추가",
            ],
            "exact_name": False,
            "require_unique": True,
        }

    def _verify_provider_foreground(self, main: WindowInfo) -> WindowInfo:
        expected_title = re.sub(r"\s+", "", str(main.title).casefold())
        foreground = [
            item for item in self.automation.list_windows()
            if item.foreground and item.handle == main.handle
            and item.process_id == main.process_id
            and re.sub(r"\s+", "", str(item.title).casefold()) == expected_title
        ]
        if len(foreground) != 1:
            raise RuntimeError("검색 입력 직전 메신저 기본 창 포커스 검증에 실패했습니다.")
        return foreground[0]

    def _set_and_verify_contact_search(
        self, main: WindowInfo, config: Dict[str, Any], search_control: Dict[str, Any],
        value: str,
    ) -> Dict[str, Any]:
        """Write one recipient name to the same focused search Edit via UIA only."""
        selector = self._contact_search_selector(config)
        identity = self._control_identity(search_control, main.handle)
        if not identity.get("stable"):
            raise RuntimeError("친구 검색창의 안정적인 UIA identity를 확인하지 못했습니다.")
        self._verify_provider_foreground(main)
        state = self.automation.set_accessibility_text(
            main.handle, value, expected_identity=identity,
            require_keyboard_focus=True, require_foreground=True, **selector,
        )
        actual_identity = self._control_identity(state, main.handle)
        if self._identity_token(actual_identity) != self._identity_token(identity):
            raise RuntimeError("친구 검색 중 입력창이 다른 UIA 요소로 바뀌었습니다.")
        if not state.get("value_verified"):
            raise RuntimeError("친구 검색어를 UIA로 입력한 뒤 값을 검증하지 못했습니다.")
        self._verify_provider_foreground(main)
        verified = self.automation.read_accessibility_text(
            main.handle, expected_identity=identity,
            require_keyboard_focus=True, require_foreground=True, **selector,
        )
        verified_identity = self._control_identity(verified, main.handle)
        if (self._identity_token(verified_identity) != self._identity_token(identity)
                or str(verified.get("value", "")) != str(value)):
            raise RuntimeError("수신자 검색 직전 동일 UIA 검색창의 값을 재검증하지 못했습니다.")
        self._verify_provider_foreground(main)
        return {**state, **verified, "control_identity": verified_identity}

    def _activate_recipient_result(self, main: WindowInfo, recipient: str,
                                   config: Dict[str, Any],
                                   search_control: Dict[str, Any]) -> Dict[str, Any]:
        excluded = config.get("forbidden_control_names") or ["친구 추가", "ID로 추가", "연락처로 추가"]
        for candidate in self._recipient_name_candidates(recipient):
            self._set_and_verify_contact_search(main, config, search_control, candidate)
            time.sleep(0.8)
            self._close_forbidden_provider_windows(main.process_id, config)
            self._verify_provider_foreground(main)
            try:
                result = self.automation.activate_accessibility_control(
                    main.handle, names=[candidate], excluded_names=excluded,
                    exact_name=True, require_unique=True, invoke=False, require_foreground=True,
                )
                if str(result.get("name") or "").strip().casefold() != candidate.casefold():
                    raise RuntimeError("검색 결과의 표시 이름이 요청한 수신자와 정확히 일치하지 않습니다.")
                result_identity = self._control_identity(result, main.handle)
                if not result_identity.get("stable"):
                    raise RuntimeError("수신자 검색 결과의 안정적인 UIA identity가 없습니다.")
                self._verify_provider_foreground(main)
                result = self.automation.activate_accessibility_control(
                    main.handle, names=[candidate], excluded_names=excluded,
                    exact_name=True, require_unique=True, invoke=True,
                    expected_identity=result_identity,
                )
                if result.get("activation") != "invoke":
                    def verify_result_target():
                        self._verify_provider_foreground(main)
                        self.automation.verify_accessibility_control(
                            main.handle, names=[candidate], excluded_names=excluded,
                            exact_name=True, require_unique=True, expected_identity=result_identity,
                            require_keyboard_focus=True, require_foreground=True,
                        )
                        self._verify_provider_foreground(main)

                    self._press_after_verification("ENTER", verify_result_target)
                return {**result, "matched_recipient": candidate}
            except LookupError:
                continue
        # Never press Enter on an unverified "first result".  That can open a
        # different contact when ranking changes or another window steals focus.
        if config.get("contact_view_accessibility_name_prefix"):
            from core.kakao_vision import open_recipient_chat
            result = open_recipient_chat(main, recipient, search_prepared=True)
            return {**result, "matched_recipient": recipient}
        raise RuntimeError(
            "수신자 이름과 정확히 일치하는 UIA 검색 결과를 찾지 못해 "
            "대화방 열기를 중단했습니다."
        )

    def _press_after_verification(self, key: str, verifier) -> None:
        """Dispatch a global key only after a fail-closed target proof."""
        verifier()
        self._press(key)


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


    def _verify_exact_chat_foreground(
        self, chat: WindowInfo, expected_titles: Set[str],
    ) -> WindowInfo:
        foreground = [item for item in self.automation.list_windows()
                      if item.foreground and item.handle == chat.handle
                      and item.process_id == chat.process_id
                      and self._normal_title(item.title) in expected_titles]
        if len(foreground) != 1:
            raise RuntimeError("조회 직전 수신자 대화창 포커스 검증에 실패했습니다.")
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
            *(occurrence.get("ancestor_automation_ids") or []),
            *(occurrence.get("ancestor_class_names") or []),
        ]
        haystack = " ".join(re.sub(r"([a-z])([A-Z])", r"\1 \2", str(item)).casefold()
                            for item in metadata_values)
        if keywords and any(keyword and re.search(
            r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", haystack,
        ) for keyword in keywords):
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


    def _visible_message_items(self, chat: WindowInfo, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        controls = self.automation.accessibility_controls(chat.handle)
        incoming_keywords = config.get("incoming_message_direction_keywords") or [
            "incoming", "received", "받은 메시지", "상대 메시지",
        ]
        incoming_config = {
            **config, "outgoing_message_direction_keywords": incoming_keywords,
            "outgoing_message_min_x_ratio": 0.0,
        }
        messages = []
        seen = set()
        for control in controls:
            if str(control.get("control_type", "")).casefold() not in {"text", "listitem", "document"}:
                continue
            if str(control.get("class_name", "")).casefold().startswith("richedit"):
                continue
            metadata = " ".join(str(control.get(key, "")) for key in ("automation_id", "class_name"))
            if re.search(r"timestamp|receipt|unread|sender|composer", metadata, re.I):
                continue
            if self._is_outgoing_occurrence(control, config):
                direction = "outgoing"
            elif (str(control.get("direction", "")).casefold() in {"incoming", "received"}
                  or self._is_outgoing_occurrence(control, incoming_config)):
                direction = "incoming"
            else:
                continue
            text = str(control.get("value", control.get("name", "")) or "")
            token = self._occurrence_token(control)
            if not text.strip() or not token or token in seen:
                continue
            seen.add(token)
            item = {"text": text, "direction": direction, "control_identity": control["control_identity"]}
            if control.get("sender"):
                item["sender"] = str(control["sender"])
            rectangle = control.get("rectangle") or []
            item["rectangle"] = rectangle
            messages.append(item)
        messages.sort(key=lambda item: (
            item["rectangle"][1], item["rectangle"][0],
        ) if len(item["rectangle"]) == 4 else (0, 0))
        return messages

    def _capture_chat_observation(self, chat: WindowInfo):
        from core.kakao_vision import capture_chat
        from core.plugin import ToolCancelledError

        try:
            self._verify_exact_chat_foreground(chat, {self._normal_title(chat.title)})
            observation = capture_chat(chat)
            self._verify_exact_chat_foreground(chat, {self._normal_title(chat.title)})
            return observation
        except ToolCancelledError:
            raise
        except Exception:
            return None

    @windows_com_apartment()
    def read(self, provider: str, recipient: str, *, limit: int = 20,
             launch_timeout: float = 12.0, chat_timeout: float = 6.0) -> Dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("읽을 메시지 수는 1~100 사이의 정수여야 합니다.")
        provider_id, config, recipient, chat, launched, expected_titles = self._open_chat(
            provider, recipient, launch_timeout=launch_timeout, chat_timeout=chat_timeout,
        )
        self._verify_exact_chat_foreground(chat, expected_titles)
        messages = self._visible_message_items(chat, config)[-limit:]
        self._verify_exact_chat_foreground(chat, expected_titles)
        read_method = "uia"
        observation = None
        if not messages:
            observation = self._capture_chat_observation(chat)
            if observation is not None:
                from core.kakao_vision import read_messages
                from core.plugin import ToolCancelledError
                try:
                    messages = read_messages(observation, recipient, limit)
                    read_method = "vision_visible_chat_bubbles"
                except ToolCancelledError:
                    raise
                except Exception:
                    pass
        verified = bool(messages)
        return {
            "provider": provider_id, "recipient": recipient, "messages": messages,
            "window_title": chat.title, "window_handle": chat.handle,
            "process_id": chat.process_id, "launched": launched,
            "visible_only": True, "history_complete": False, "unread_only": False,
            "read_method": read_method,
            "screenshot_sha256": observation.image_hash if observation is not None else "",
            "read_at": datetime_now_iso(), "read_verified": verified,
            "verification_status": "verified" if verified else "unverified",
            "unverified_reason": "" if verified else (
                "접근성 및 화면 분석에서 현재 대화창의 메시지 본문을 확인하지 못했습니다. "
                "메시지가 없다는 뜻은 아닙니다."
            ),
        }

    def _open_chat(self, provider: str, recipient: str,
                   *, launch_timeout: float, chat_timeout: float):
        if os.name != "nt":
            raise OSError("데스크톱 메신저는 Windows에서만 지원합니다.")
        provider_id, config = self.resolve_provider(provider)
        recipient = str(recipient).strip()
        if not recipient or any(ord(char) < 32 for char in recipient):
            raise ValueError("카카오톡에 표시된 수신자 이름을 입력해야 합니다.")

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
        for process_id in {window.process_id for window in main_windows}:
            self._verify_provider_process(process_id, config)

        expected_titles = {
            item.casefold() for item in self._recipient_name_candidates(recipient)
        }
        provider_process_ids = {window.process_id for window in main_windows}
        # Reuse an already-open exact recipient chat before touching provider search.
        # This is both safer and more compatible with KakaoTalk builds whose main
        # list/search controls are custom-drawn and absent from the UIA tree.
        chat_windows = [
            item for item in windows
            if item.process_id in provider_process_ids
            and self._normal_title(item.title) in expected_titles
        ]
        if not chat_windows:
            if len(main_windows) != 1:
                raise RuntimeError("메신저 기본 창이 여러 개여서 사용할 계정을 하나로 확인하지 못했습니다.")
            main = main_windows[0]
            self._close_forbidden_provider_windows(main.process_id, config)
            main = self.automation.focus(main.handle)
            self._verify_provider_foreground(main)
            try:
                search_control = self._focus_contact_search(main, config)
            except LookupError:
                from core.kakao_vision import open_recipient_chat
                open_recipient_chat(main, recipient)
            else:
                self._close_forbidden_provider_windows(main.process_id, config)
                self._activate_recipient_result(main, recipient, config, search_control)
            self._close_forbidden_provider_windows(main.process_id, config)
            chat_windows = self._wait_for_windows(
                lambda item: item.process_id == main.process_id
                and self._normal_title(item.title) in expected_titles,
                chat_timeout,
            )
        if len(chat_windows) != 1:
            raise RuntimeError(
                "수신자 이름과 정확히 일치하는 대화창을 하나로 검증하지 못해 대화방 열기를 중단했습니다."
            )
        chat = self.automation.focus(chat_windows[0].handle)
        self._verify_exact_chat_foreground(chat, expected_titles)
        return provider_id, config, recipient, chat, launched, expected_titles


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat()
