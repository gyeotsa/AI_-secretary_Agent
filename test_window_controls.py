import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ui.main_window import JarvisMainWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_main_window_is_taskbar_window_not_tool(app):
    window = JarvisMainWindow()
    assert window.windowType() == Qt.WindowType.Window
    assert not window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
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
