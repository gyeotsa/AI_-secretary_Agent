"""Transactional, bounded Windows clipboard use for native text input.

Clipboard data is copied *before* EmptyClipboard; retaining OleGetClipboard's
proxy alone is not a snapshot.  No clipboard contents are logged or persisted.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
import time


_LOCK = threading.RLock()


class ClipboardChangedError(RuntimeError):
    pass


class _NativeClipboard:
    CF_UNICODETEXT = 13
    _GDI = {2, 9, 0x82}
    _METAFILE = {3, 0x83}
    _ENHMETAFILE = {14, 0x8E}

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows 클립보드 입력은 Windows에서만 지원합니다.")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.window = None
        self.opened = False
        signatures = (
            (self.user.OpenClipboard, [wintypes.HWND], wintypes.BOOL),
            (self.user.CloseClipboard, [], wintypes.BOOL),
            (self.user.EmptyClipboard, [], wintypes.BOOL),
            (self.user.EnumClipboardFormats, [wintypes.UINT], wintypes.UINT),
            (self.user.GetClipboardData, [wintypes.UINT], wintypes.HANDLE),
            (self.user.SetClipboardData, [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE),
            (self.user.GetClipboardSequenceNumber, [], wintypes.DWORD),
            (self.user.GetClipboardOwner, [], wintypes.HWND),
            (self.user.RegisterClipboardFormatW, [wintypes.LPCWSTR], wintypes.UINT),
            (self.user.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
            (self.kernel.GlobalAlloc, [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL),
            (self.kernel.GlobalLock, [wintypes.HGLOBAL], ctypes.c_void_p),
            (self.kernel.GlobalSize, [wintypes.HGLOBAL], ctypes.c_size_t),
            (self.kernel.GlobalUnlock, [wintypes.HGLOBAL], wintypes.BOOL),
            (self.kernel.GlobalFree, [wintypes.HGLOBAL], wintypes.HGLOBAL),
            (self.ole.OleDuplicateData, [wintypes.HANDLE, wintypes.WORD, wintypes.UINT], wintypes.HANDLE),
            (self.gdi.CopyEnhMetaFileW, [wintypes.HANDLE, wintypes.LPCWSTR], wintypes.HANDLE),
            (self.gdi.DeleteEnhMetaFile, [wintypes.HANDLE], wintypes.BOOL),
            (self.gdi.DeleteMetaFile, [wintypes.HANDLE], wintypes.BOOL),
            (self.gdi.DeleteObject, [wintypes.HANDLE], wintypes.BOOL),
        )
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result
        create = self.user.CreateWindowExW
        create.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                           wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                           wintypes.HINSTANCE, ctypes.c_void_p]
        create.restype = wintypes.HWND
        self.window = create(0, "STATIC", "ANIS clipboard transaction", 0,
                             0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
        if not self.window:
            raise OSError("클립보드 트랜잭션 소유 창을 만들지 못했습니다.")

    def open(self):
        for _ in range(12):
            if self.user.OpenClipboard(self.window):
                self.opened = True
                return
            time.sleep(0.025)
        raise RuntimeError("다른 프로그램이 클립보드를 사용 중입니다. 내용을 변경하지 않았습니다.")

    def close(self):
        if self.opened:
            self.user.CloseClipboard()
            self.opened = False

    def dispose(self):
        self.close()
        if self.window:
            self.user.DestroyWindow(self.window)
            self.window = None

    def sequence(self):
        return int(self.user.GetClipboardSequenceNumber())

    def owns_temporary_text(self, text):
        """Account for Windows synthesizing CF_TEXT/CF_LOCALE while pasting.

        Synthesis can change the sequence without a new copy operation.  Only
        accept that case when our hidden window is still the owner AND the exact
        published Unicode body is intact.  Identical text copied by another app
        has a different owner and must still be preserved as the newer clipboard.
        """
        if not self.opened or self.user.GetClipboardOwner() != self.window:
            return False
        handle = self.user.GetClipboardData(self.CF_UNICODETEXT)
        expected = (text + "\0").encode("utf-16-le")
        if not handle or self.kernel.GlobalSize(handle) < len(expected):
            return False
        address = self.kernel.GlobalLock(handle)
        if not address:
            return False
        try:
            return ctypes.string_at(address, len(expected)) == expected
        finally:
            self.kernel.GlobalUnlock(handle)

    def snapshot(self):
        copies = []
        current = 0
        try:
            for _ in range(256):
                ctypes.set_last_error(0)
                current = int(self.user.EnumClipboardFormats(current))
                if not current:
                    if ctypes.get_last_error():
                        raise RuntimeError("클립보드 형식 목록을 안전하게 읽지 못했습니다.")
                    return copies
                # Owner-display and app-owned GDI/private formats cannot be
                # re-owned generically.  Abort before modifying any clipboard data.
                if current == 0x80 or 0x200 <= current <= 0x3FF:
                    raise RuntimeError("현재 클립보드의 전용 형식을 보존할 수 없어 붙여넣기를 중단했습니다.")
                source = self.user.GetClipboardData(current)
                if not source:
                    raise RuntimeError("클립보드 지연 데이터를 읽지 못해 내용을 보존했습니다.")
                if current in self._ENHMETAFILE:
                    copied = self.gdi.CopyEnhMetaFileW(source, None)
                else:
                    base_format = {0x82: 2, 0x83: 3}.get(current, current)
                    copied = self.ole.OleDuplicateData(source, base_format, 0)
                if not copied:
                    raise RuntimeError("클립보드 원본을 복사하지 못해 붙여넣기를 중단했습니다.")
                copies.append([current, copied])
            raise RuntimeError("클립보드 형식 수가 안전 한도를 초과했습니다.")
        except BaseException:
            self.release(copies)
            raise

    def _allocate(self, data: bytes):
        handle = self.kernel.GlobalAlloc(2, len(data))
        if not handle:
            raise MemoryError("클립보드 임시 메모리를 할당하지 못했습니다.")
        address = self.kernel.GlobalLock(handle)
        if not address:
            self.kernel.GlobalFree(handle)
            raise RuntimeError("클립보드 임시 메모리를 열지 못했습니다.")
        try:
            ctypes.memmove(address, data, len(data))
        finally:
            self.kernel.GlobalUnlock(handle)
        return handle

    def prepare_text(self, text):
        prepared = []
        try:
            prepared.append([self.CF_UNICODETEXT, self._allocate((text + "\0").encode("utf-16-le"))])
            # Do not place private outgoing drafts in clipboard history/cloud sync.
            for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
                format_id = self.user.RegisterClipboardFormatW(name)
                if not format_id:
                    raise RuntimeError("클립보드 개인정보 보호 형식을 등록하지 못했습니다.")
                prepared.append([format_id, self._allocate(b"\0\0\0\0")])
            return prepared
        except BaseException:
            self.release(prepared)
            raise

    def replace(self, handles):
        if not self.user.EmptyClipboard():
            raise RuntimeError("클립보드를 교체할 수 없습니다.")
        for entry in handles:
            if not self.user.SetClipboardData(entry[0], entry[1]):
                raise RuntimeError("클립보드 형식 복원/설정에 실패했습니다.")
            entry[1] = None  # Ownership transferred to Windows, never free it here.

    def release(self, handles):
        for format_id, handle in handles:
            if not handle:
                continue
            if format_id in self._GDI:
                self.gdi.DeleteObject(handle)
            elif format_id in self._ENHMETAFILE:
                self.gdi.DeleteEnhMetaFile(handle)
            elif format_id in self._METAFILE:
                class MetaFilePict(ctypes.Structure):
                    _fields_ = [("mm", wintypes.LONG), ("xExt", wintypes.LONG),
                                ("yExt", wintypes.LONG), ("hMF", wintypes.HANDLE)]
                address = self.kernel.GlobalLock(handle)
                if address:
                    meta = ctypes.cast(address, ctypes.POINTER(MetaFilePict)).contents.hMF
                    if meta:
                        self.gdi.DeleteMetaFile(meta)
                    self.kernel.GlobalUnlock(handle)
                self.kernel.GlobalFree(handle)
            else:
                self.kernel.GlobalFree(handle)


def paste_text_transaction(text: str, paste, *, api=None):
    """Temporarily publish text, invoke synchronous paste, then restore snapshots.

    ``paste`` must verify its target and finish reading the clipboard before it
    returns.  If another app/user copies while it runs, keep that newer clipboard
    and fail the transaction rather than overwrite it with an older snapshot.
    """
    if not isinstance(text, str) or not text or "\0" in text:
        raise ValueError("붙여넣을 본문은 NUL 문자가 없는 비어 있지 않은 문자열이어야 합니다.")
    with _LOCK:
        native = api if api is not None else _NativeClipboard()
        original, prepared = [], []
        changed = False
        try:
            native.open()
            original = native.snapshot()
            prepared = native.prepare_text(text)
            changed = True
            try:
                native.replace(prepared)
            except BaseException:
                native.replace(original)
                changed = False
                raise
            native.close()
            # The first close can race another app/user copying. Never accept a
            # newly observed sequence as our own without owner+body identity.
            native.open()
            if not native.owns_temporary_text(text):
                changed = False
                raise ClipboardChangedError("붙여넣기 전에 클립보드가 변경되어 새 내용을 유지하고 중단합니다.")
            native.close()
            return paste()
        finally:
            try:
                if changed:
                    native.open()
                    if not native.owns_temporary_text(text):
                        raise ClipboardChangedError(
                            "입력 중 새 클립보드 내용이 감지되어 새 내용을 유지했습니다. 전송을 중단합니다."
                        )
                    native.replace(original)
            finally:
                native.close()
                native.release(original)
                native.release(prepared)
                native.dispose()
