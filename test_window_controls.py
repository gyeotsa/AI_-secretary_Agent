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
    assert window.sound_bar.isHidden()
    assert window.sidebar.isVisible()
    assert window.sound_bar.bar_count == 28
    window.update_state(State.LISTENING)
    assert window.sound_bar.is_active and not window.sound_bar.is_speaking
    window.update_state(State.RESPONDING)
    assert window.sound_bar.is_active and window.sound_bar.is_speaking
    window.update_state(State.IDLE)
    assert not window.sound_bar.is_active
    window.close()


def test_chat_panel_collapses_and_restores_while_brain_expands(app):
    window = JarvisMainWindow()
    window.resize(800, 700)
    window.show()
    app.processEvents()
    initial_height = window.brain_orbit.height()

    assert window.chat_collapsed is False
    assert not window.chat_panel.isHidden()
    assert window.chat_panel.isAncestorOf(window.user_text_label)
    assert window.chat_panel.isAncestorOf(window.assistant_text_label)
    assert window.user_text_label.parentWidget() is window.message_scroll.widget()
    assert window.assistant_text_label.parentWidget() is window.message_scroll.widget()
    assert window.chat_panel.isAncestorOf(window.text_input)

    window.toggle_chat_panel()
    app.processEvents()
    assert window.chat_collapsed is True
    assert window.chat_panel.isHidden()
    assert not window.brain_orbit.isHidden()
    assert "열기" in window.chat_toggle_btn.text()
    assert window.brain_orbit.height() > 0

    window.toggle_chat_panel()
    app.processEvents()
    assert window.chat_collapsed is False
    assert not window.chat_panel.isHidden()
    assert "숨기기" in window.chat_toggle_btn.text()
    window.close()


def test_chat_collapse_state_survives_window_mode_round_trip(app):
    window = JarvisMainWindow()
    window.set_chat_collapsed(True)
    window.toggle_window_mode()
    assert window.window_mode == "mini"
    window.toggle_window_mode()
    app.processEvents()

    assert window.window_mode == "maximized"
    assert window.chat_collapsed is True
    assert window.chat_panel.isHidden()
    window.close()


def test_brain_note_open_selects_the_same_vault_document(app):
    window = JarvisMainWindow()
    selected = []

    class GraphWindowStub:
        def show_note(self, relative_path):
            selected.append(relative_path)

    stub = GraphWindowStub()
    window.open_specialist_workspace = lambda key: stub if key == "knowledge_graph" else None

    assert window.open_knowledge_graph_note("wiki/project-brief.md") is stub
    assert selected == ["wiki/project-brief.md"]
    window.close()


def test_packaged_app_icon_exists():
    assert resource_path("assets/jarvis.ico").is_file()


def test_composer_keyboard_sends_once_and_preserves_newlines(app):
    from PyQt6.QtTest import QTest
    window = JarvisMainWindow()
    submitted = []
    window.text_submitted.connect(submitted.append)
    window.text_input.setText("first")
    window.text_input.moveCursor(window.text_input.textCursor().MoveOperation.End)
    QTest.keyClick(window.text_input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    window.text_input.insertPlainText("second")
    assert submitted == []
    QTest.keyClick(window.text_input, Qt.Key.Key_Return)
    assert submitted == ["first\nsecond"]
    assert window.text_input.text() == ""
    assert not window.send_btn.isEnabled()
    window.close()


def test_conversation_retains_turns_and_reset_removes_them(app):
    window = JarvisMainWindow()
    window.show_user_text("first question")
    window.show_assistant_text("first answer")
    window.show_user_text("second question")
    window.show_assistant_text("second answer")
    assert [label.text() for label in window._archived_messages] == ["first question", "first answer"]
    window.show_assistant_text("updated second answer")
    assert len(window._archived_messages) == 2
    window.clear_conversation_display()
    assert not window._archived_messages
    assert window.welcome.isVisible()
    assert window.user_text_label.isHidden()
    window.close()


def test_sidebar_search_selection_and_new_chat_use_runtime_signals(app):
    class Memory:
        def list_session_details(self):
            return [{"session_id": "a", "title": "Alpha"}, {"session_id": "b", "title": "Beta"}]
    window = JarvisMainWindow()
    selected, created = [], []
    window.session_selected.connect(selected.append)
    window.session_created.connect(created.append)
    window.set_memory_manager(Memory())
    window.set_current_session("b")
    assert window.conversation_title.text() == "Beta"
    window.session_search.setText("alpha")
    assert not window.session_list.item(0).isHidden()
    assert window.session_list.item(1).isHidden()
    window.session_list.itemClicked.emit(window.session_list.item(0))
    window.new_chat_btn.click()
    assert selected == ["a"]
    assert len(created) == 1
    window.close()
