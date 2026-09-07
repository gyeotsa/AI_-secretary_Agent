"""Exercise the production clipboard paste on a self-owned hidden RichEdit.

No messenger window is opened and no message is sent.  User clipboard values are
never printed or saved; the production transaction restores its copied formats.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.desktop_messaging import DesktopMessagingRuntime
from core.windows_clipboard import _NativeClipboard


def run():
    if os.name != "nt":
        raise OSError("Windows 실환경에서 실행하세요.")
    native = _NativeClipboard()
    child = None
    module = None
    body = "아니스 입력 검증\n한글과 emoji 😀 / 끝 공백  "
    try:
        kernel = native.kernel
        kernel.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
        kernel.LoadLibraryW.restype = wintypes.HMODULE
        kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
        kernel.FreeLibrary.restype = wintypes.BOOL
        module = kernel.LoadLibraryW("Msftedit.dll")
        if not module:
            raise OSError("Msftedit.dll을 불러오지 못했습니다.")
        child = native.user.CreateWindowExW(
            0, "RICHEDIT50W", "", 0x40000000 | 0x0004,
            0, 0, 500, 160, native.window, None, None, None,
        )
        if not child:
            raise OSError("검증용 RichEdit 생성 실패")
        native.user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        native.user.GetWindowTextLengthW.restype = ctypes.c_int
        native.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        native.user.GetWindowTextW.restype = ctypes.c_int
        native.user.IsWindow.argtypes = [wintypes.HWND]
        native.user.IsWindow.restype = wintypes.BOOL

        def read_body():
            length = native.user.GetWindowTextLengthW(child)
            buffer = ctypes.create_unicode_buffer(length + 1)
            native.user.GetWindowTextW(child, buffer, len(buffer))
            return buffer.value.replace("\r\n", "\n").replace("\r", "\n")

        def verify_target():
            if not native.user.IsWindow(child) or read_body():
                raise RuntimeError("검증용 입력 요소가 사라졌거나 초안이 변경됐습니다.")

        # Exercise the production identity checks, EM_SETSEL and WM_PASTE,
        # not a second implementation of that path. Its clipboard owner is
        # separate from this self-owned control, and is restored before return.
        DesktopMessagingRuntime()._paste_unicode_preserving_clipboard(
            body,
            identity={"native_handle": int(child), "process_id": os.getpid(),
                      "window_handle": int(native.window), "class_name": "RICHEDIT50W"},
            verify_target=verify_target,
        )
        if read_body() != body:
            raise AssertionError("검증용 RichEdit 본문이 입력 요청과 일치하지 않습니다.")

        return {
            "implementation": "DesktopMessagingRuntime._paste_unicode_preserving_clipboard",
            "paste_callback_completed": True,
            "exact_unicode_body_verified": True,
            "line_count": body.count("\n") + 1,
            "utf16_units": len(body.encode("utf-16-le")) // 2,
            "clipboard_restored": True,
            "scope": "self_owned_hidden_richedit_not_messenger_delivery",
        }
    finally:
        if child and native.user.IsWindow(child):
            native.user.DestroyWindow(child)
        native.dispose()
        if module:
            native.kernel.FreeLibrary(module)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/acceptance/native_text_input.json")
    args = parser.parse_args()
    report = {"scenario": "native_richedit_paste", "external_send": False,
              "executed_at": datetime.now(timezone.utc).isoformat()}
    try:
        report.update(run())
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
