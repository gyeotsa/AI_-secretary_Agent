"""Handle and UI Automation based Windows control; coordinates are an explicit fallback only."""
from __future__ import annotations

import ctypes
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


user32 = ctypes.windll.user32 if os.name == "nt" else None


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process_id: int
    visible: bool
    enabled: bool
    foreground: bool


class WindowsAutomationRuntime:
    STRATEGY_ORDER = ("api", "cli", "com", "uia", "coordinate")

    def list_windows(self) -> List[WindowInfo]:
        if user32 is None:
            return []
        result = []
        foreground = int(user32.GetForegroundWindow())
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                result.append(WindowInfo(int(hwnd), buffer.value, int(pid.value), True,
                                         bool(user32.IsWindowEnabled(hwnd)), int(hwnd) == foreground))
            return True

        user32.EnumWindows(callback_type(callback), 0)
        return result

    def find_window(self, *, title: str = "", handle: int = 0) -> WindowInfo:
        windows = self.list_windows()
        if handle:
            found = next((item for item in windows if item.handle == int(handle)), None)
        else:
            needle = title.casefold().strip()
            matches = [item for item in windows if needle and needle in item.title.casefold()]
            if len(matches) > 1:
                exact = [item for item in matches if item.title.casefold() == needle]
                matches = exact or matches
            found = matches[0] if matches else None
        if not found:
            raise LookupError("대상 창 Handle을 찾지 못했습니다.")
        return found

    def focus(self, handle: int) -> WindowInfo:
        if user32 is None or not user32.IsWindow(int(handle)):
            raise LookupError("유효하지 않은 창 Handle입니다.")
        user32.ShowWindow(int(handle), 9)  # SW_RESTORE
        user32.SetForegroundWindow(int(handle))
        time.sleep(0.05)
        foreground = int(user32.GetForegroundWindow())
        if foreground != int(handle):
            raise RuntimeError(f"창 포커스 검증 실패: foreground={foreground}")
        return self.find_window(handle=int(handle))

    def verify_foreground(self, handle: int) -> WindowInfo:
        """Prove that ``handle`` still owns global keyboard input.

        UIA keyboard focus is local to one accessibility tree.  It is therefore
        insufficient by itself: another top-level application can become the
        foreground window while the old UIA element still reports focus.  Every
        operation that could lead to global keyboard input uses this independent
        top-level-window proof immediately before dispatch.
        """
        expected = int(handle)
        if expected <= 0:
            raise LookupError("검증할 창 Handle이 올바르지 않습니다.")
        if user32 is not None:
            foreground = int(user32.GetForegroundWindow())
            if foreground != expected:
                raise RuntimeError(
                    f"전역 키보드 입력 직전 창 포커스가 변경되었습니다: "
                    f"foreground={foreground}, expected={expected}"
                )
        window = self.find_window(handle=expected)
        if not window.visible or not window.enabled or not window.foreground:
            raise RuntimeError("전역 키보드 입력 직전 대상 창의 활성 상태를 검증하지 못했습니다.")
        return window

    @staticmethod
    def _normal_accessibility_value(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "")).casefold()

    @staticmethod
    def _rectangle_value(value) -> List[int]:
        """Return a JSON-safe UIA rectangle without depending on one wrapper type."""
        if value is None:
            return []
        try:
            return [int(value.left), int(value.top), int(value.right), int(value.bottom)]
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            items = list(value)
            return [int(item) for item in items[:4]] if len(items) >= 4 else []
        except (TypeError, ValueError):
            return []

    @classmethod
    def _control_identity(cls, handle: int, wrapper, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Build a stable identity for one UIA element.

        RuntimeId is the primary key.  Native handle is the next strongest key;
        selector metadata and bounds are retained as a fail-closed fallback for UIA
        providers that omit RuntimeId.  The window handle is part of every identity
        so an Edit from another chat can never compare equal.
        """
        info = getattr(wrapper, "element_info", None)
        runtime_id: List[Any] = []
        native_handle = 0
        class_name = str(metadata.get("class_name", "") or "")
        process_id = int(metadata.get("process_id", 0) or 0)
        rectangle = list(metadata.get("rectangle") or [])
        if info is not None:
            try:
                raw_runtime_id = getattr(info, "runtime_id", None)
                if callable(raw_runtime_id):
                    raw_runtime_id = raw_runtime_id()
                if raw_runtime_id:
                    runtime_id = [int(item) if isinstance(item, (int, float)) else str(item)
                                  for item in list(raw_runtime_id)]
            except (TypeError, ValueError):
                runtime_id = []
            try:
                native_handle = int(getattr(info, "handle", 0) or 0)
            except (TypeError, ValueError):
                native_handle = 0
            class_name = str(getattr(info, "class_name", class_name) or class_name)
            try:
                process_id = int(getattr(info, "process_id", process_id) or process_id)
            except (TypeError, ValueError):
                pass
            if not rectangle:
                rectangle = cls._rectangle_value(getattr(info, "rectangle", None))

        identity = {
            "window_handle": int(handle),
            "runtime_id": runtime_id,
            "native_handle": native_handle,
            "process_id": process_id,
            "automation_id": str(metadata.get("automation_id", "") or ""),
            "control_type": str(metadata.get("control_type", "") or ""),
            "class_name": class_name,
            "name": str(metadata.get("name", "") or ""),
            "rectangle": rectangle,
        }
        # Do not put mutable presentation attributes (name/bounds) in the primary
        # token when UIA gives us a real element identity.  A window move or layout
        # resize must not make the same composer look like a different control.
        # Conversely, selector metadata alone cannot prove that an Edit wasn't
        # destroyed and recreated, so callers can explicitly fail closed on the
        # ``stable`` flag.
        if runtime_id:
            token_payload = {
                "window_handle": int(handle), "process_id": process_id,
                "runtime_id": runtime_id,
            }
            identity_basis = "runtime_id"
            stable = True
        elif native_handle:
            token_payload = {
                "window_handle": int(handle), "process_id": process_id,
                "native_handle": native_handle,
            }
            identity_basis = "native_handle"
            stable = True
        else:
            token_payload = dict(identity)
            identity_basis = "selector_fallback"
            stable = False
        identity["identity_basis"] = identity_basis
        identity["stable"] = stable
        identity["token"] = json.dumps(token_payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"))
        return identity

    @staticmethod
    def _identity_token(identity: Optional[Dict[str, Any]]) -> str:
        if not isinstance(identity, dict):
            return ""
        token = str(identity.get("token", "") or "")
        if token:
            return token
        payload = {key: value for key, value in identity.items() if key != "token"}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _accessibility_root(handle: int):
        """Return a pywinauto UIA root without falling back to screen coordinates."""
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise RuntimeError("Windows 접근성 자동화에 pywinauto가 필요합니다.") from exc
        try:
            return Desktop(backend="uia").window(handle=int(handle)).wrapper_object()
        except Exception as exc:
            raise RuntimeError(f"Windows UI Automation 연결 실패: {exc}") from exc

    def _accessibility_candidates(
        self, handle: int, *, names=(), automation_id_keywords=(), control_types=(),
        excluded_names=(), exact_name: bool = False, allow_type_only: bool = False,
    ):
        root = self._accessibility_root(handle)
        wrappers = [root, *root.descendants()]
        wanted_names = [self._normal_accessibility_value(item) for item in names if str(item).strip()]
        id_keywords = [self._normal_accessibility_value(item)
                       for item in automation_id_keywords if str(item).strip()]
        wanted_types = {str(item).casefold() for item in control_types if str(item).strip()}
        excluded = [self._normal_accessibility_value(item)
                    for item in excluded_names if str(item).strip()]
        ranked = []
        for wrapper in wrappers:
            try:
                info = wrapper.element_info
                name = str(info.name or "")
                automation_id = str(info.automation_id or "")
                control_type = str(info.control_type or "")
                normalized_name = self._normal_accessibility_value(name)
                normalized_id = self._normal_accessibility_value(automation_id)
                if excluded and any(item and item in normalized_name for item in excluded):
                    continue
                if wanted_types and control_type.casefold() not in wanted_types:
                    continue
                exact_match = bool(wanted_names and normalized_name in wanted_names)
                partial_match = bool(wanted_names and any(
                    item in normalized_name or normalized_name in item
                    for item in wanted_names if normalized_name
                ))
                id_match = bool(id_keywords and any(item in normalized_id for item in id_keywords))
                if exact_name and not exact_match and not id_match:
                    continue
                if not exact_name and not (exact_match or partial_match or id_match or allow_type_only):
                    continue
                if not wrapper.is_visible() or not wrapper.is_enabled():
                    continue
                score = (120 if exact_match else 0) + (70 if partial_match else 0) + (80 if id_match else 0)
                try:
                    if wrapper.has_keyboard_focus():
                        score += 15
                except Exception:
                    pass
                metadata = {
                    "name": name, "automation_id": automation_id,
                    "control_type": control_type,
                    "class_name": str(getattr(info, "class_name", "") or ""),
                    "process_id": int(getattr(info, "process_id", 0) or 0),
                    "rectangle": self._rectangle_value(getattr(info, "rectangle", None)),
                }
                metadata["control_identity"] = self._control_identity(handle, wrapper, metadata)
                ranked.append((score, wrapper, metadata))
            except Exception:
                continue
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked

    def accessibility_controls(self, handle: int) -> List[Dict[str, Any]]:
        """List visible controls as metadata only; native wrapper objects never escape."""
        return [metadata for _score, _wrapper, metadata in self._accessibility_candidates(
            handle, allow_type_only=True,
        )]

    def activate_accessibility_control(
        self, handle: int, *, names=(), automation_id_keywords=(), control_types=(),
        excluded_names=(), exact_name: bool = False, allow_type_only: bool = False,
        require_unique: bool = False, invoke: bool = True,
        expected_identity: Optional[Dict[str, Any]] = None,
        require_keyboard_focus: bool = False, require_foreground: bool = False,
    ) -> Dict[str, Any]:
        """Focus and optionally invoke one verified UIA control.

        No mouse coordinate or global shortcut fallback is used.  Ambiguous type-only
        matches are rejected so a messaging command cannot activate an unrelated UI.
        """
        if require_foreground:
            self.verify_foreground(handle)
        ranked = self._accessibility_candidates(
            handle, names=names, automation_id_keywords=automation_id_keywords,
            control_types=control_types, excluded_names=excluded_names,
            exact_name=exact_name, allow_type_only=allow_type_only,
        )
        if not ranked:
            raise LookupError("접근성 트리에서 요청한 Control을 찾지 못했습니다.")
        expected_token = self._identity_token(expected_identity)
        if expected_token:
            ranked = [item for item in ranked if self._identity_token(
                item[2].get("control_identity") or self._control_identity(handle, item[1], item[2])
            ) == expected_token]
            if not ranked:
                raise LookupError("이전에 검증한 Control과 동일한 UIA 요소를 찾지 못했습니다.")
        if require_unique and len(ranked) != 1:
            raise LookupError(f"접근성 Control 후보가 {len(ranked)}개라 안전하게 선택할 수 없습니다.")
        _score, wrapper, metadata = ranked[0]
        identity = metadata.get("control_identity") or self._control_identity(handle, wrapper, metadata)
        metadata = {**metadata, "control_identity": identity}
        method = "focus"
        try:
            wrapper.set_focus()
            if require_foreground:
                self.verify_foreground(handle)
            if require_keyboard_focus:
                try:
                    focused = bool(wrapper.has_keyboard_focus())
                except Exception as exc:
                    raise RuntimeError(f"접근성 Control 포커스를 확인할 수 없습니다: {exc}") from exc
                if not focused:
                    raise RuntimeError("검증한 UIA Control이 키보드 포커스를 얻지 못했습니다.")
            if invoke:
                if hasattr(wrapper, "invoke"):
                    wrapper.invoke()
                    method = "invoke"
                elif hasattr(wrapper, "select"):
                    wrapper.select()
                    method = "select"
        except Exception as exc:
            raise RuntimeError(f"접근성 Control 활성화 실패: {exc}") from exc
        time.sleep(0.08)
        if require_foreground:
            self.verify_foreground(handle)
        if require_keyboard_focus:
            # Re-query the tree after focus message pumping.  A wrapper instance
            # can outlive the underlying UIA element, so its old focus flag alone
            # must not authorize subsequent keyboard input.
            _wrapper, verified = self._accessibility_text_control(
                handle, names=names, automation_id_keywords=automation_id_keywords,
                control_types=control_types, excluded_names=excluded_names,
                exact_name=exact_name, allow_type_only=allow_type_only,
                require_unique=require_unique, expected_identity=identity,
                require_keyboard_focus=True, require_foreground=require_foreground,
            )
            metadata = {**metadata, **verified}
        return {**metadata, "strategy": "uia", "activation": method,
                "coordinate_fallback_used": False}

    def _accessibility_text_control(
        self, handle: int, *, names=(), automation_id_keywords=(), control_types=("Edit",),
        excluded_names=(), exact_name: bool = False, allow_type_only: bool = False,
        require_unique: bool = False, expected_identity: Optional[Dict[str, Any]] = None,
        require_keyboard_focus: bool = False, require_foreground: bool = False,
    ):
        """Select one editable UIA wrapper without exposing it to callers."""
        if require_foreground:
            self.verify_foreground(handle)
        ranked = self._accessibility_candidates(
            handle, names=names, automation_id_keywords=automation_id_keywords,
            control_types=control_types, excluded_names=excluded_names,
            exact_name=exact_name, allow_type_only=allow_type_only,
        )
        if not ranked:
            raise LookupError("접근성 트리에서 입력 Control을 찾지 못했습니다.")
        expected_token = self._identity_token(expected_identity)
        if expected_token:
            ranked = [item for item in ranked if self._identity_token(
                item[2].get("control_identity") or self._control_identity(handle, item[1], item[2])
            ) == expected_token]
            if not ranked:
                raise LookupError("이전에 검증한 입력 Control과 동일한 UIA 요소를 찾지 못했습니다.")
        if require_unique and len(ranked) != 1:
            raise LookupError(f"접근성 입력 Control 후보가 {len(ranked)}개라 안전하게 선택할 수 없습니다.")
        _score, wrapper, metadata = ranked[0]
        identity = metadata.get("control_identity") or self._control_identity(handle, wrapper, metadata)
        metadata = {**metadata, "control_identity": identity}
        if require_foreground:
            self.verify_foreground(handle)
        if require_keyboard_focus:
            try:
                focused = bool(wrapper.has_keyboard_focus())
            except Exception as exc:
                raise RuntimeError(f"접근성 입력 Control 포커스를 확인할 수 없습니다: {exc}") from exc
            if not focused:
                raise RuntimeError("이전에 검증한 메시지 입력창이 Enter 직전 키보드 포커스를 잃었습니다.")
            metadata["keyboard_focus_verified"] = True
        return wrapper, metadata

    def verify_accessibility_control(
        self, handle: int, *, expected_identity: Dict[str, Any], names=(),
        automation_id_keywords=(), control_types=(), excluded_names=(),
        exact_name: bool = False, allow_type_only: bool = False,
        require_unique: bool = False, require_keyboard_focus: bool = True,
        require_foreground: bool = True,
    ) -> Dict[str, Any]:
        """Re-resolve and verify one previously captured UIA element."""
        if not self._identity_token(expected_identity):
            raise RuntimeError("검증할 UIA Control identity가 없습니다.")
        _wrapper, metadata = self._accessibility_text_control(
            handle, names=names, automation_id_keywords=automation_id_keywords,
            control_types=control_types, excluded_names=excluded_names,
            exact_name=exact_name, allow_type_only=allow_type_only,
            require_unique=require_unique, expected_identity=expected_identity,
            require_keyboard_focus=require_keyboard_focus,
            require_foreground=require_foreground,
        )
        return {
            **metadata, "strategy": "uia", "activation": "verify",
            "coordinate_fallback_used": False,
        }

    @staticmethod
    def _read_wrapper_value(wrapper) -> str:
        """Read an Edit value through UIA ValuePattern, never from screen OCR."""
        try:
            if hasattr(wrapper, "get_value"):
                return str(wrapper.get_value())
        except Exception:
            pass
        try:
            iface = getattr(wrapper, "iface_value", None)
            if iface is not None:
                return str(iface.CurrentValue)
        except Exception:
            pass
        raise RuntimeError("접근성 입력 Control의 현재 값을 읽을 수 없습니다.")

    @staticmethod
    def _canonical_edit_value(value: str, class_name: str = "") -> str:
        """Normalize only line-ending behavior owned by a native RichEdit.

        KakaoTalk's RICHEDIT50W exposes a mandatory trailing carriage return even
        when the visible composer is empty.  Treat that one terminator as control
        metadata, while preserving every user-visible character and internal line.
        """
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        if str(class_name or "").casefold().startswith("richedit") and text.endswith("\n"):
            text = text[:-1]
        return text

    def set_accessibility_text(
        self, handle: int, value: str, *, names=(), automation_id_keywords=(),
        control_types=("Edit",), excluded_names=(), exact_name: bool = False,
        allow_type_only: bool = False, require_unique: bool = False,
        expected_identity: Optional[Dict[str, Any]] = None,
        require_keyboard_focus: bool = False, require_foreground: bool = False,
    ) -> Dict[str, Any]:
        """Set and verify Unicode text in one identified UIA Edit control."""
        wrapper, metadata = self._accessibility_text_control(
            handle, names=names, automation_id_keywords=automation_id_keywords,
            control_types=control_types, excluded_names=excluded_names,
            exact_name=exact_name, allow_type_only=allow_type_only,
            require_unique=require_unique, expected_identity=expected_identity,
            require_keyboard_focus=require_keyboard_focus,
            require_foreground=require_foreground,
        )
        try:
            wrapper.set_focus()
            if require_foreground:
                self.verify_foreground(handle)
            if require_keyboard_focus:
                try:
                    focused = bool(wrapper.has_keyboard_focus())
                except Exception as exc:
                    raise RuntimeError(f"접근성 입력 Control 포커스를 확인할 수 없습니다: {exc}") from exc
                if not focused:
                    raise RuntimeError("텍스트 입력 직전 UIA Control이 키보드 포커스를 잃었습니다.")
            if hasattr(wrapper, "set_edit_text"):
                wrapper.set_edit_text(str(value))
                method = "set_edit_text"
            else:
                iface = getattr(wrapper, "iface_value", None)
                if iface is None:
                    raise RuntimeError("UIA ValuePattern을 지원하지 않습니다.")
                iface.SetValue(str(value))
                method = "value_pattern"
        except Exception as exc:
            raise RuntimeError(f"접근성 입력 Control 텍스트 설정 실패: {exc}") from exc
        time.sleep(0.08)
        if require_foreground:
            self.verify_foreground(handle)
        if require_keyboard_focus:
            # Writing can recreate a virtualized Edit.  Re-resolve the captured
            # identity before accepting the write as belonging to the target.
            wrapper, verified = self._accessibility_text_control(
                handle, names=names, automation_id_keywords=automation_id_keywords,
                control_types=control_types, excluded_names=excluded_names,
                exact_name=exact_name, allow_type_only=allow_type_only,
                require_unique=require_unique, expected_identity=metadata["control_identity"],
                require_keyboard_focus=True, require_foreground=require_foreground,
            )
            metadata = {**metadata, **verified}
        actual = self._read_wrapper_value(wrapper)
        actual_canonical = self._canonical_edit_value(actual, metadata.get("class_name", ""))
        # Requested text has no control-owned sentinel.  Preserve an intentional
        # final newline; strip one terminator only from the value read from RichEdit.
        expected_canonical = self._canonical_edit_value(str(value))
        if actual_canonical != expected_canonical:
            raise RuntimeError(
                f"접근성 입력 검증 실패: 입력 길이 {len(actual)}/{len(str(value))}"
            )
        return {
            **metadata, "strategy": "uia", "activation": "focus",
            "text_method": method, "value_verified": True, "value": actual,
            "coordinate_fallback_used": False,
        }

    def read_accessibility_text(
        self, handle: int, *, names=(), automation_id_keywords=(),
        control_types=("Edit",), excluded_names=(), exact_name: bool = False,
        allow_type_only: bool = False, require_unique: bool = False,
        expected_identity: Optional[Dict[str, Any]] = None,
        require_keyboard_focus: bool = False, require_foreground: bool = False,
    ) -> Dict[str, Any]:
        """Read one identified UIA Edit value for post-action verification."""
        wrapper, metadata = self._accessibility_text_control(
            handle, names=names, automation_id_keywords=automation_id_keywords,
            control_types=control_types, excluded_names=excluded_names,
            exact_name=exact_name, allow_type_only=allow_type_only,
            require_unique=require_unique, expected_identity=expected_identity,
            require_keyboard_focus=require_keyboard_focus,
            require_foreground=require_foreground,
        )
        return {**metadata, "value": self._read_wrapper_value(wrapper)}

    @staticmethod
    def _wrapper_text_values(wrapper, metadata: Dict[str, Any]) -> List[str]:
        values = [str(metadata.get("name", "") or "")]
        try:
            if hasattr(wrapper, "window_text"):
                values.append(str(wrapper.window_text() or ""))
        except Exception:
            pass
        try:
            values.append(WindowsAutomationRuntime._read_wrapper_value(wrapper))
        except RuntimeError:
            pass
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _ancestor_accessibility_metadata(wrapper, max_depth: int = 4) -> Dict[str, List[str]]:
        names: List[str] = []
        automation_ids: List[str] = []
        class_names: List[str] = []
        current = wrapper
        for _ in range(max(0, int(max_depth))):
            try:
                current = current.parent()
            except Exception:
                break
            if current is None:
                break
            info = getattr(current, "element_info", None)
            if info is None:
                continue
            names.append(str(getattr(info, "name", "") or ""))
            automation_ids.append(str(getattr(info, "automation_id", "") or ""))
            class_names.append(str(getattr(info, "class_name", "") or ""))
        return {
            "ancestor_names": [item for item in names if item],
            "ancestor_automation_ids": [item for item in automation_ids if item],
            "ancestor_class_names": [item for item in class_names if item],
        }

    def accessibility_text_occurrences(
        self, handle: int, value: str, *, control_types=("Text", "ListItem", "Document"),
    ) -> List[Dict[str, Any]]:
        """Return visible UIA elements whose exposed text exactly equals ``value``.

        This is intentionally an observation-only API.  It is used to snapshot
        existing bubbles before Enter and then prove that a *new* exact-body bubble
        appeared afterwards.
        """
        # Line-ending conventions are equivalent; whitespace in the body is
        # not. Trimming would certify a different message as an exact send.
        target = self._canonical_edit_value(value)
        if not target.strip():
            return []
        root = self._accessibility_root(handle)
        root_rectangle = self._rectangle_value(getattr(root.element_info, "rectangle", None))
        allowed_types = {str(item).casefold() for item in control_types if str(item).strip()}
        matches: List[Dict[str, Any]] = []
        for wrapper in [root, *root.descendants()]:
            try:
                info = wrapper.element_info
                control_type = str(info.control_type or "")
                if allowed_types and control_type.casefold() not in allowed_types:
                    continue
                if not wrapper.is_visible():
                    continue
                metadata = {
                    "name": str(info.name or ""),
                    "automation_id": str(info.automation_id or ""),
                    "control_type": control_type,
                    "class_name": str(getattr(info, "class_name", "") or ""),
                    "process_id": int(getattr(info, "process_id", 0) or 0),
                    "rectangle": self._rectangle_value(getattr(info, "rectangle", None)),
                }
                if target not in [self._canonical_edit_value(item)
                                  for item in self._wrapper_text_values(wrapper, metadata)]:
                    continue
                identity = self._control_identity(handle, wrapper, metadata)
                rectangle = metadata["rectangle"]
                x_ratio = None
                if len(rectangle) == 4 and len(root_rectangle) == 4:
                    root_width = root_rectangle[2] - root_rectangle[0]
                    if root_width > 0:
                        center_x = (rectangle[0] + rectangle[2]) / 2.0
                        x_ratio = (center_x - root_rectangle[0]) / root_width
                matches.append({
                    **metadata,
                    **self._ancestor_accessibility_metadata(wrapper),
                    "control_identity": identity,
                    "identity_token": self._identity_token(identity),
                    "value": target,
                    "x_ratio": x_ratio,
                })
            except Exception:
                continue
        return matches

    def close_window(self, handle: int) -> None:
        if user32 is None or not user32.IsWindow(int(handle)):
            return
        user32.PostMessageW(int(handle), 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.08)

    def accessibility_tree(self, handle: int, max_depth: int = 4, max_nodes: int = 300) -> Dict[str, Any]:
        if user32 is None or not user32.IsWindow(int(handle)):
            raise LookupError("유효하지 않은 창 Handle입니다.")
        root = self._accessibility_root(handle)
        nodes = []
        queue = [(root, 0)]
        while queue and len(nodes) < max_nodes:
            wrapper, depth = queue.pop(0)
            if depth > max_depth:
                continue
            try:
                info = wrapper.element_info
                nodes.append({"name": str(info.name or ""),
                              "automation_id": str(info.automation_id or ""),
                              "control_type": str(info.control_type or ""), "depth": depth,
                              "enabled": bool(wrapper.is_enabled())})
                queue.extend((child, depth + 1) for child in wrapper.children())
            except Exception:
                continue
        return {"handle": int(handle), "nodes": nodes, "truncated": len(nodes) >= max_nodes}

    def invoke(self, handle: int, *, automation_id: str = "", name: str = "") -> Dict[str, Any]:
        tree = self.accessibility_tree(handle)
        target = next((node for node in tree["nodes"] if
                       (automation_id and node["automation_id"] == automation_id) or
                       (name and node["name"].casefold() == name.casefold())), None)
        if not target:
            raise LookupError("접근성 트리에서 대상 Control을 찾지 못했습니다.")
        # Inspecting and identifying a control is safe. Invocation needs a native element lookup;
        # expose the deterministic selector instead of silently falling back to a coordinate click.
        return {"strategy": "uia", "handle": int(handle), "selector": target,
                "verified": True, "coordinate_fallback_used": False}

    @staticmethod
    def policy() -> Dict[str, Any]:
        return {"priority": list(WindowsAutomationRuntime.STRATEGY_ORDER),
                "coordinate_fallback": "explicit_user_approval_only"}
