import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core.interface_control import get_interface_control_bridge
from main_qt import JarvisApp
from ui.main_window import JarvisMainWindow


_APP = QApplication.instance() or QApplication([])


def test_idle_chat_shell_does_not_continuously_repaint_window():
    from PyQt6.QtCore import QEvent, QObject
    from PyQt6.QtTest import QTest

    class PaintCounter(QObject):
        count = 0

        def eventFilter(self, _watched, event):
            if event.type() == QEvent.Type.Paint:
                self.count += 1
            return False

    window = JarvisMainWindow()
    counter = PaintCounter(window)
    window.installEventFilter(counter)
    try:
        _APP.processEvents()
        counter.count = 0
        QTest.qWait(350)
        assert counter.count <= 4
    finally:
        window.close()


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
    app.proactive_policy = Service("proactive", fail=True)
    app.runtime_services = Service("runtime")
    app.tool_executor = Tools()
    bridge = get_interface_control_bridge()
    bridge.register("open_surface", lambda: None)

    app._shutdown_runtime()
    app._shutdown_runtime()

    assert calls == ["proactive", "runtime", "tts"]
    assert len(app._shutdown_errors) == 1 and "proactive_policy.stop" in app._shutdown_errors[0]
    assert not bridge.available("open_surface")


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
