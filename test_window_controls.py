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


def test_task_details_show_readable_progress_instead_of_internal_fields(app):
    from types import SimpleNamespace
    from ui.main_window import TaskManagerDialog

    task = SimpleNamespace(
        task_id="internal-task-id", status="awaiting_user", goal="회의 자료 정리",
        pending_question="어느 날짜의 자료를 정리할까요?", intent_name="internal.intent",
        last_tool="internal_tool", verification_status="unverified", artifacts=[],
        evidence=[{"kind": "profile_database", "summary": "사용자 설정을 확인했습니다.",
                   "event_id": "private-event-id"}],
        updated_at="2026-10-20T00:30:00+00:00",
    )
    dialog = TaskManagerDialog(SimpleNamespace(list_tasks=lambda *a, **kw: [task]), "session", "")
    try:
        listing = dialog.task_list.item(0).text()
        detail = dialog.details.toPlainText()
        assert "회의 자료 정리" in listing and "상태" in listing
        assert "어느 날짜의 자료" in detail and "사용자 설정을 확인했습니다." in detail
        assert "2026년 10월 20일" in detail
        assert not any(value in listing + detail for value in (
            "internal-task-id", "awaiting_user", "internal.intent", "internal_tool", "private-event-id",
        ))
        assert dialog.cancel_button.isEnabled()
    finally:
        dialog.close()


def test_calendar_sidebar_opens_one_tab_and_preserves_date_on_reopen(app, tmp_path, monkeypatch):
    from PyQt6.QtCore import QDate
    from PyQt6.QtWidgets import QCalendarWidget
    from config import Config
    from ui.codex_shell import close_tab

    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    window = JarvisMainWindow()
    try:
        window.calendar_btn.click()
        page = window.calendar_dialog
        assert page.store.db_path == tmp_path / "calendar.db"
        assert window.workspace_tabs.currentWidget() is page
        assert window.workspace_tabs.count() == 2
        calendar = page.findChild(QCalendarWidget)
        calendar.setSelectedDate(QDate(2026, 10, 25))
        window.open_interface_surface("calendar")
        assert window.workspace_tabs.count() == 2

        close_tab(window, window.workspace_tabs.indexOf(page))
        assert window.workspace_tabs.count() == 1
        window.calendar_btn.click()
        assert window.workspace_tabs.currentWidget() is page
        assert calendar.selectedDate() == QDate(2026, 10, 25)
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_calendar_store_failure_does_not_open_a_tab(app, monkeypatch):
    import sqlite3
    from PyQt6.QtWidgets import QMessageBox
    from core.local_calendar import LocalCalendarStore

    def fail(_self, *args, **kwargs):
        raise sqlite3.DatabaseError("database is unavailable")

    warnings = []
    monkeypatch.setattr(LocalCalendarStore, "__init__", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    window = JarvisMainWindow()
    try:
        assert window.show_local_calendar() is None
        assert window.calendar_dialog is None
        assert window.workspace_tabs.count() == 1
        assert "저장소를 열 수 없습니다" in warnings[0]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


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
    window.open_knowledge_graph = lambda: stub

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
        def list_session_details(self, all_workspaces=False):
            assert all_workspaces
            return [{"session_id": "a", "title": "Alpha"}, {"session_id": "b", "title": "Beta"}]
    window = JarvisMainWindow()
    selected, created = [], []
    window.session_selected.connect(selected.append)
    window.session_created.connect(created.append)
    window.set_memory_manager(Memory())
    window.set_current_session("b")
    assert window.conversation_title.text() == "Beta"
    group, children = window._session_groups['global']
    assert group.text().endswith("일반 대화")
    window.session_search.setText("alpha")
    assert not children[0].isHidden()
    assert children[1].isHidden()
    window.session_list.itemClicked.emit(children[0])
    window.new_chat_btn.click()
    assert selected == ["a"]
    assert len(created) == 1
    window.close()


def test_project_folders_keep_pins_selection_and_folding_when_refreshed(app):
    from PyQt6.QtTest import QTest
    from core.workspace import WorkspaceInfo
    from ui.codex_shell import refresh_sessions

    class Memory:
        def list_session_details(self, all_workspaces=False):
            return [
                {"session_id": "recent", "title": "최근 대화", "workspace_namespace": "project-a"},
                {"session_id": "pinned", "title": "중요한 대화", "workspace_namespace": "project-a", "is_pinned": True},
                {"session_id": "old", "title": "이전 자료", "workspace_namespace": "missing-project"},
            ]

    window = JarvisMainWindow()
    window.project_workspaces = [
        WorkspaceInfo(path="C:/alpha/project", alias="프로젝트", namespace="project-a"),
        WorkspaceInfo(path="C:/beta/project", alias="프로젝트", namespace="project-b"),
        WorkspaceInfo(path="C:/inactive", alias="사용하지 않는 프로젝트", namespace="inactive"),
    ]
    window.current_workspace_path = "C:/beta/project"
    try:
        window.set_memory_manager(Memory())
        window.set_current_session("pinned")
        window.show()
        app.processEvents()
        tree = window.session_list
        assert len(window._session_groups) == 3
        first, children = window._session_groups['project-a']
        second, empty = window._session_groups['project-b']
        previous, old_children = window._session_groups['missing-project']
        assert first.text().endswith("프로젝트 · alpha") and second.text().endswith("프로젝트 · beta")
        assert first.toolTip() == "C:/alpha/project"
        assert empty == []
        assert old_children[0].text().strip() == "이전 자료"
        assert children[0].text().strip() == "📌 중요한 대화"
        assert tree.currentItem().data(Qt.ItemDataRole.UserRole) == "pinned"

        selected = []
        window.session_selected.connect(selected.append)
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualItemRect(first).center())
        assert all(child.isHidden() for child in children) and selected == []
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualItemRect(first).center())
        assert all(not child.isHidden() for child in children)
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualItemRect(first).center())
        refresh_sessions(window)
        first, children = window._session_groups['project-a']
        previous = window._session_groups['missing-project'][0]
        assert all(child.isHidden() for child in children)
        assert tree.currentItem().data(Qt.ItemDataRole.UserRole) == "pinned"

        window.session_search.setText("최근")
        assert not children[1].isHidden()
        assert children[0].isHidden() and previous.isHidden()
        window.session_search.clear()
        assert all(child.isHidden() for child in children)
        assert not previous.isHidden()
        window.session_search.setText("beta")
        assert not window._session_groups['project-b'][0].isHidden() and first.isHidden()
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_sidebar_context_menu_renames_pins_copies_and_confirms_permanent_delete(app, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog, QMenu, QMessageBox
    from ui.codex_shell import session_context_menu

    class Memory:
        def list_session_details(self, all_workspaces=False):
            return [{"session_id": "test", "title": "기존 이름", "is_pinned": False}]

        def load_session(self, session_id):
            assert session_id == "test"
            return [{"role": "user", "content": "첫 질문\n두 번째 줄"},
                    {"role": "assistant", "content": "답변"},
                    {"role": "system", "content": "내부 지시"}]

    window = JarvisMainWindow()
    window.set_assistant_identity("아니스")
    window.set_permission_manager(object())
    window.permission_action_requested.connect(lambda _permission, _description, callback: callback())
    renamed, pinned, deleted, menus = [], [], [], []
    window.session_renamed.connect(lambda session_id, title: renamed.append((session_id, title)))
    window.session_pin_changed.connect(lambda session_id, value: pinned.append((session_id, value)))
    window.session_deleted.connect(deleted.append)
    window.set_memory_manager(Memory())
    window.show()
    app.processEvents()
    item = window._session_groups['global'][1][0]
    position = window.session_list.visualItemRect(item).center()

    def run_menu(menu, _position):
        actions = [action for action in menu.actions() if not action.isSeparator()]
        menus.append([action.text() for action in actions])
        for action in actions:
            action.trigger()

    def decline_delete(dialog):
        assert dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.No)
        assert dialog.textFormat() == Qt.TextFormat.PlainText
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMenu, "exec", run_menu)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("  새 이름  ", True))
    monkeypatch.setattr(QMessageBox, "exec", decline_delete)
    try:
        session_context_menu(window, position)
        assert menus == [["이름 변경", "고정", "복사", "영구 삭제"]]
        assert renamed == [("test", "새 이름")] and pinned == [("test", True)]
        assert QApplication.clipboard().text() == "기존 이름\n\n사용자\n첫 질문\n두 번째 줄\n\n아니스\n답변"
        assert deleted == []
        monkeypatch.setattr(QMessageBox, "exec", lambda _dialog: QMessageBox.StandardButton.Yes)
        session_context_menu(window, position)
        assert deleted == ["test"]
        session_context_menu(window, window.session_list.visualItemRect(window._session_groups['global'][0]).center())
        assert len(menus) == 3 and menus[-1] == ["새 채팅"]
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_sidebar_refresh_keeps_items_when_displayed_metadata_is_unchanged(app):
    from core.workspace import WorkspaceInfo
    from ui.codex_shell import refresh_sessions

    class Memory:
        sessions = [{"session_id": "a", "title": "대화", "workspace_namespace": "project-a"}]

        def list_session_details(self, all_workspaces=False):
            return self.sessions

    window = JarvisMainWindow()
    memory = Memory()
    window.project_workspaces = [WorkspaceInfo(path="C:/alpha", alias="프로젝트", namespace="project-a")]
    window.set_memory_manager(memory)
    window.set_current_session("a")
    resets = []
    window.session_list.model().modelReset.connect(lambda: resets.append(True))
    group, children = window._session_groups['project-a']
    original_item = children[0]
    window._session_expanded['project-a'] = False
    window.session_search.setText("대화")
    try:
        for _ in range(20):
            refresh_sessions(window)
        assert resets == []
        assert window._session_groups['project-a'][1][0] is original_item
        assert window.session_list.currentItem() is original_item
        memory.sessions[0]['message_count'] = 100
        refresh_sessions(window)
        assert resets == []

        memory.sessions[0]['title'] = '변경한 대화'
        refresh_sessions(window)
        assert len(resets) == 1
        assert window.conversation_title.text() == '변경한 대화'
        assert window.session_search.text() == '대화'
        window.session_search.clear()
        assert window._session_groups['project-a'][1][0].isHidden()

        window.project_workspaces[0].alias = '다른 이름'
        refresh_sessions(window)
        assert len(resets) == 2
        assert window._session_groups['project-a'][0].text().endswith('다른 이름')
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_project_context_menu_targets_clicked_folder_and_confirms_removal(app, monkeypatch):
    from core.workspace import WorkspaceInfo
    from PyQt6.QtWidgets import QMenu, QMessageBox
    from ui.codex_shell import filter_sessions, session_context_menu

    class Memory:
        def list_session_details(self, all_workspaces=False):
            return [{"session_id": namespace, "title": namespace, "workspace_namespace": namespace}
                    for namespace in ('project-a', 'project-b', 'missing-project', 'global')]

    window = JarvisMainWindow()
    window.project_workspaces = [WorkspaceInfo(path=f'C:/{name}', alias=name, namespace=name)
                                 for name in ('project-a', 'project-b')]
    created, removed, selected, menus = [], [], [], []
    window.project_chat_created.connect(created.append)
    window.project_removed.connect(removed.append)
    window.session_selected.connect(selected.append)
    window.set_memory_manager(Memory())
    window.set_current_session('project-a')
    window._session_expanded['project-b'] = False
    filter_sessions(window, '')
    window.show()
    app.processEvents()

    def run_menu(menu, _position):
        actions = [action for action in menu.actions() if not action.isSeparator()]
        menus.append([action.text() for action in actions])
        for action in actions:
            action.trigger()

    def decline(dialog):
        assert dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.No)
        assert dialog.button(QMessageBox.StandardButton.Yes).text() == '제거'
        assert dialog.button(QMessageBox.StandardButton.No).text() == '취소'
        assert '실제 폴더와 파일은 삭제하지 않으며' in dialog.informativeText()
        assert '일반 대화로 옮깁니다' in dialog.informativeText()
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMenu, 'exec', run_menu)
    monkeypatch.setattr(QMessageBox, 'exec', decline)
    try:
        group = window._session_groups['project-b'][0]
        session_context_menu(window, window.session_list.visualItemRect(group).center())
        assert menus == [['새 채팅', '폴더 제거']]
        assert created == ['project-b'] and removed == [] and selected == []
        assert window.current_session_id == 'project-a'
        assert not window._session_expanded['project-b']

        monkeypatch.setattr(QMessageBox, 'exec', lambda _dialog: QMessageBox.StandardButton.Yes)
        session_context_menu(window, window.session_list.visualItemRect(group).center())
        assert removed == ['project-b']
        previous = window._session_groups['missing-project'][0]
        session_context_menu(window, window.session_list.visualItemRect(previous).center())
        assert removed == ['project-b', 'missing-project']

        general = window._session_groups['global'][0]
        session_context_menu(window, window.session_list.visualItemRect(general).center())
        assert menus[-1] == ['새 채팅']
        assert created[-1] == 'global' and removed == ['project-b', 'missing-project']
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_sidebar_rename_rejects_blank_title_and_copy_keeps_clipboard_on_read_error(app, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog, QMessageBox
    from ui.codex_shell import copy_session, rename_session

    window = JarvisMainWindow()
    renamed, warnings = [], []
    window.session_renamed.connect(lambda *values: renamed.append(values))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("   ", True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))

    class Memory:
        def load_session(self, _session_id):
            raise OSError("unavailable")

    window.memory_manager = Memory()
    try:
        session = {"session_id": "test", "title": "이름"}
        rename_session(window, session)
        assert renamed == [] and "이름을 입력" in warnings[0]
        QApplication.clipboard().setText("기존 클립보드")
        copy_session(window, session)
        assert QApplication.clipboard().text() == "기존 클립보드"
        assert "불러오지 못했습니다" in warnings[1]
    finally:
        window.close()


def test_microphone_control_precedes_theme_and_can_cancel_start(app):
    window = JarvisMainWindow()
    requested = []
    window.microphone_toggled.connect(requested.append)
    try:
        toolbar = window.drag_tab.layout()
        assert toolbar.indexOf(window.microphone_btn) + 1 == toolbar.indexOf(window.theme_toggle_btn)
        assert not window.microphone_btn.isChecked()
        window.microphone_btn.click()
        assert requested == [True]
        window.set_microphone_state(True, busy=True)
        assert "연결 중" in window.microphone_btn.text() and window.microphone_btn.isEnabled()
        window.microphone_btn.click()
        assert requested == [True, False]
        window.set_microphone_state(False)
        assert window.microphone_btn.text() == "마이크 OFF" and not window.microphone_btn.isChecked()
        window.set_microphone_state(True)
        assert window.microphone_btn.text() == "마이크 ON" and window.microphone_btn.isChecked()
        assert requested == [True, False]
    finally:
        window.close()


def test_permission_settings_all_toggles_save_and_forward_changes(app, tmp_path):
    from PyQt6.QtWidgets import QCheckBox, QLabel
    from core.permission import PermissionManager

    manager = PermissionManager(str(tmp_path / "permissions.json"))
    window = JarvisMainWindow()
    window.set_permission_manager(manager)
    changed = []
    window.permission_changed.connect(lambda *values: changed.append(values))
    dialog = window.show_permission_settings()
    try:
        toggles = dialog.findChildren(QCheckBox)
        permissions = manager.get_all_permissions()
        assert len(toggles) == len(permissions) == 24
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        for toggle, permission in zip(toggles, permissions):
            assert toggle.isEnabled() and permission.name in labels
            assert all(f"({permission.id})" not in label for label in labels)
            toggle.setChecked(True)
            toggle.setChecked(False)
            assert manager.is_blocked(permission.id)
            assert changed[-1] == (permission.id, False)
            toggle.setChecked(True)
            assert manager.check_permission(permission.id)
            assert changed[-1] == (permission.id, True)
        assert all(PermissionManager(str(manager.storage_path)).check_permission(p.id) for p in permissions)
    finally:
        window.close()


def test_copy_waits_for_permission_callback_and_denies_without_manager(app, monkeypatch):
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QMessageBox
    from ui.codex_shell import copy_session

    window = JarvisMainWindow()
    window.memory_manager = SimpleNamespace(load_session=lambda _id: [{"role": "user", "content": "질문"}])
    warnings, requests = [], []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    window.permission_action_requested.connect(lambda *values: requests.append(values))
    QApplication.clipboard().setText("유지할 내용")
    session = {"session_id": "temporary", "title": "검증"}
    try:
        copy_session(window, session)
        assert warnings and requests == []
        assert QApplication.clipboard().text() == "유지할 내용"
        window.set_permission_manager(object())
        copy_session(window, session)
        assert requests[0][0] == "clipboard"
        assert QApplication.clipboard().text() == "유지할 내용"
        requests[0][2]()
        assert QApplication.clipboard().text() == "검증\n\n사용자\n질문"
    finally:
        window.close()
