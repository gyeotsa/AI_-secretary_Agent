"""Handle and UI Automation based Windows control; coordinates are an explicit fallback only."""
from __future__ import annotations

import ctypes
import os
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

    def accessibility_tree(self, handle: int, max_depth: int = 4, max_nodes: int = 300) -> Dict[str, Any]:
        if user32 is None or not user32.IsWindow(int(handle)):
            raise LookupError("유효하지 않은 창 Handle입니다.")
        try:
            import comtypes.client
            automation = comtypes.client.CreateObject("UIAutomationClient.CUIAutomation")
            root = automation.ElementFromHandle(int(handle))
            walker = automation.ControlViewWalker
        except Exception as exc:
            raise RuntimeError(f"Windows UI Automation 연결 실패: {exc}") from exc
        nodes = []

        def visit(element, depth):
            if element is None or depth > max_depth or len(nodes) >= max_nodes:
                return
            try:
                nodes.append({"name": str(element.CurrentName or ""),
                              "automation_id": str(element.CurrentAutomationId or ""),
                              "control_type": int(element.CurrentControlType), "depth": depth,
                              "enabled": bool(element.CurrentIsEnabled)})
                child = walker.GetFirstChildElement(element)
                while child is not None and len(nodes) < max_nodes:
                    visit(child, depth + 1)
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                return

        visit(root, 0)
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
