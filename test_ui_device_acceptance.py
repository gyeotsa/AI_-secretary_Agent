import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core.interface_control import get_interface_control_bridge
from main_qt import AppSignals, JarvisApp
from ui.main_window import JarvisMainWindow


_APP = QApplication.instance() or QApplication([])


class _Settings:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value


class _Permission:
    def __init__(self, allowed):
        self.allowed = allowed

    def request_permission(self, _permission):
        return self.allowed


class _Gesture:
    def __init__(self, running=False, fail=False):
        self.running = running
        self.fail = fail
        self.stop_calls = 0

    def start(self):
        if self.fail:
            raise RuntimeError("camera unavailable")
        self.running = True
        return SimpleNamespace(running=True, error="")

    def stop(self):
        self.stop_calls += 1
        self.running = False
        return SimpleNamespace(running=False, error="")

    def status(self):
        return {"running": self.running, "error": ""}


def _camera_app(*, allowed=True, fail=False):
    app = JarvisApp.__new__(JarvisApp)
    app.assistant_settings = _Settings()
    app.permission_manager = _Permission(allowed)
    app.gesture_runtime = _Gesture(fail=fail)
    app.signals = AppSignals()
    return app


def test_camera_persistence_follows_verified_runtime_state():
    accepted = _camera_app()
    assert accepted._set_gesture_camera_sync(True)["running"] is True
    assert accepted.assistant_settings.values["gesture_camera_enabled"] == "true"

    denied = _camera_app(allowed=False)
    status = denied._set_gesture_camera_sync(True)
    assert status["running"] is False and "권한" in status["error"]
    assert denied.assistant_settings.values["gesture_camera_enabled"] == "false"

    unavailable = _camera_app(fail=True)
    assert unavailable._set_gesture_camera_sync(True)["running"] is False
    assert unavailable.assistant_settings.values["gesture_camera_enabled"] == "false"


def test_shutdown_continues_after_one_cleanup_failure_and_detaches_ui_bridge():
    calls = []

    class Service:
        def __init__(self, name, fail=False): self.name, self.fail = name, fail
        def stop(self):
            calls.append(self.name)
            if self.fail: raise RuntimeError("boom")

    class Tools:
        def shutdown_tts(self): calls.append("tts")

    app = JarvisApp.__new__(JarvisApp)
    app._runtime_shutdown_started = False
    app.gesture_runtime = Service("gesture", fail=True)
    app.proactive_policy = Service("proactive")
    app.runtime_services = Service("runtime")
    app.tool_executor = Tools()
    bridge = get_interface_control_bridge()
    for name in ("set_gesture_camera", "get_gesture_status", "set_gesture_configuration",
                 "get_gesture_configuration", "open_surface"):
        bridge.register(name, lambda: None)

    app._shutdown_runtime()
    app._shutdown_runtime()

    assert calls == ["gesture", "proactive", "runtime", "tts"]
    assert len(app._shutdown_errors) == 1 and "gesture_runtime.stop" in app._shutdown_errors[0]
    assert not any(bridge.available(name) for name in (
        "set_gesture_camera", "get_gesture_status", "set_gesture_configuration",
        "get_gesture_configuration", "open_surface",
    ))


def test_chat_panel_toggle_changes_real_visibility_and_restores_it():
    window = JarvisMainWindow()
    window.show()
    _APP.processEvents()
    window.set_chat_collapsed(True)
    _APP.processEvents()
    assert window.chat_collapsed is True
    assert window.chat_panel.isVisible() is False
    assert "열기" in window.chat_toggle_btn.text()

    window.toggle_chat_panel()
    _APP.processEvents()
    assert window.chat_collapsed is False
    assert window.chat_panel.isVisible() is True
    assert "숨기기" in window.chat_toggle_btn.text()
    window.close()
