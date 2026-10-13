"""Windows-wide emergency stop while another application owns keyboard focus."""
import ctypes
from ctypes import wintypes
import sys

from PyQt6.QtCore import QAbstractNativeEventFilter


class EmergencyStopHotkey(QAbstractNativeEventFilter):
    HOTKEY_ID = 0x41A6
    WM_HOTKEY = 0x0312

    def __init__(self, application, stop):
        super().__init__()
        self.application, self.stop = application, stop
        self.registered = False
        if sys.platform != "win32":
            return
        try:
            self.user32 = ctypes.windll.user32
            # Register on the Qt GUI thread; a null window receives WM_HOTKEY there.
            self.registered = bool(self.user32.RegisterHotKey(None, self.HOTKEY_ID, 0x4006, 0x7A))
        except (AttributeError, OSError):
            return
        if self.registered:
            try:
                self.application.installNativeEventFilter(self)
            except RuntimeError:
                self.close()

    def nativeEventFilter(self, event_type, message):
        if self.registered and bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            event = wintypes.MSG.from_address(int(message))
            if event.message == self.WM_HOTKEY and event.wParam == self.HOTKEY_ID:
                self.stop()
                return True, 0
        return False, 0

    def close(self):
        if self.registered:
            self.registered = False
            self.user32.UnregisterHotKey(None, self.HOTKEY_ID)
            self.application.removeNativeEventFilter(self)
