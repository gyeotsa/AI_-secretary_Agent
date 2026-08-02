import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ui.main_window import JarvisMainWindow
from main_qt import resource_path
from core.state_machine import State


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_main_window_is_taskbar_window_not_tool(app):
    window = JarvisMainWindow()
    assert window.windowType() == Qt.WindowType.Window
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert not hasattr(window, "title_label")
    window.set_assistant_identity("아니스")
    assert window.assistant_identity == "아니스"
    window.close()


def test_both_minimize_buttons_use_standard_minimize(app):
    window = JarvisMainWindow()
    assert window.minimize_btn.receivers(window.minimize_btn.clicked) > 0
    assert window.mini_control_bar.minimize_btn.receivers(
        window.mini_control_bar.minimize_btn.clicked
    ) > 0
    window.minimize_window()
    assert window.windowState() & Qt.WindowState.WindowMinimized
    window.close()


def test_main_window_has_rounded_translucent_surface_and_voice_bar(app):
    window = JarvisMainWindow()
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert window.sound_bar.isVisible()
    assert window.sound_bar.bar_count == 28
    window.update_state(State.LISTENING)
    assert window.sound_bar.is_active and not window.sound_bar.is_speaking
    window.update_state(State.RESPONDING)
    assert window.sound_bar.is_active and window.sound_bar.is_speaking
    window.update_state(State.IDLE)
    assert not window.sound_bar.is_active
    window.close()


def test_packaged_app_icon_exists():
    assert resource_path("assets/jarvis.ico").is_file()
