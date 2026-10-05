"""Offscreen acceptance tests for the local-first reminder UI."""
import os
import threading
import time
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox
from ui.continuity_dialog import ContinuityDialog

_APP = QApplication.instance() or QApplication([])


class Service:
    notifications_enabled = False

    def __init__(self):
        self.calls = []
        self.connected = []
        self.step = {"step_id": "step-1", "description": "<b>로그 수집 검증</b>",
                     "status": "pending", "verification": "reported", "provider": "codex",
                     "session_key": "codex:session-1", "workspace_path": "C:/project",
                     "goal": "통합 리마인더", "priority": 0, "due_at": None,
                     "evidence": {"text": "<img src=x>원본 근거", "source_ref": "log.jsonl:10"}}

    def dashboard(self):
        return {"next_actions": [dict(self.step)], "blocked": [],
                "recent": [{**self.step, "status": "done"}],
                "suggested": [{"id": "session:codex:session-1", "status": "suggested",
                               "session_key": "codex:session-1", "description": "이어서 작업"}], "stats": {}}

    def list_sessions(self, **kwargs):
        return [{"session_key": "codex:session-1", "provider": "codex", "title": "외부 작업", "workspace_path": "C:/project"}]

    def session_detail(self, key):
        return {"session": self.list_sessions()[0], "steps": [dict(self.step)],
                "messages": [{"text": "<script>실행하지 말고 표시</script>"}],
                "plans": [{"version": 2}], "events": [{"kind": "tool_result", "text": "pytest passed"}], "history": []}

    def sources(self):
        return self.connected

    def discover_sources(self):
        self.calls.append(("discover",))
        return [{"provider": "codex", "path": "C:/logs", "exists": True}]

    def add_source(self, provider, path):
        self.calls.append(("add", provider, path))
        self.connected.append({"source_id": "source-1", "provider": provider, "path": path, "enabled": True})

    def sync(self):
        self.calls.append(("sync",))
        return {"events": 2, "errors": []}

    def update_step(self, key, **changes):
        self.calls.append(("update", key, changes))
        self.step.update(changes)

    def confirm_suggestion(self, key, description):
        self.calls.append(("confirm", key, description))

    def create_manual_plan(self, key, goal, steps):
        self.calls.append(("plan", key, goal, steps))

    def set_source_enabled(self, key, enabled):
        self.calls.append(("enabled", key, enabled))
        self.connected[0]["enabled"] = enabled

    def delete_source(self, key, purge=True):
        self.calls.append(("delete", key, purge))
        self.connected.clear()

    def set_notifications_enabled(self, value):
        self.notifications_enabled = value
        self.calls.append(("notifications", value))


@pytest.fixture
def dialog():
    window = ContinuityDialog(Service())
    yield window
    if window._sync_thread:
        window._sync_thread.join(timeout=3)
    _APP.processEvents()
    window.refresh_timer.stop()
    window.close()
    window.deleteLater()
    _APP.processEvents()


def wait_idle(dialog):
    deadline = time.monotonic() + 3
    while dialog._busy and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.005)
    assert not dialog._busy, dialog.status_label.text()


def test_source_discovery_is_read_only_until_user_connects(dialog):
    assert dialog.service.calls == []
    assert not dialog.notifications.isChecked()
    dialog._discover_sources()
    assert dialog.service.calls == [("discover",)]
    assert dialog.source_table.item(0, 2).text() == "연결 가능"
    dialog.source_table.selectRow(0)
    dialog._connect_selected_source()
    wait_idle(dialog)
    assert ("add", "codex", "C:/logs") in dialog.service.calls
    assert dialog.service.calls.count(("sync",)) == 1
    assert dialog.source_table.item(0, 2).text() == "수집 중"


def test_evidence_and_transcripts_are_plain_text_with_reported_completion(dialog):
    dialog.tasks_table.selectRow(0)
    assert "<img src=x>원본 근거" in dialog.evidence.toPlainText()
    assert dialog.selection_label.textFormat() == Qt.TextFormat.PlainText
    dialog.category.setCurrentIndex(dialog.category.findData("recent"))
    assert "미검증" in dialog.tasks_table.item(0, 1).text()
    dialog.sessions_list.setCurrentRow(0)
    assert "<script>실행하지 말고 표시</script>" in dialog.timeline.toPlainText()
    assert "pytest passed" in dialog.timeline.toPlainText()
    assert dialog.plan_table.rowCount() == 1


def test_user_can_correct_status_due_priority_and_snooze(dialog):
    dialog.tasks_table.selectRow(0)
    dialog.status_combo.setCurrentIndex(dialog.status_combo.findData("done"))
    dialog.priority_combo.setCurrentIndex(dialog.priority_combo.findData(3))
    dialog.due_enabled.setChecked(True)
    dialog.due_edit.setDateTime(QDateTime.fromString("2026-10-01T09:00:00Z", Qt.DateFormat.ISODate))
    dialog._save_step()
    wait_idle(dialog)
    change = dialog.service.calls[-1]
    assert change[0:2] == ("update", "step-1")
    assert change[2] == {"status": "done", "priority": 3, "due_at": "2026-10-01T09:00:00Z"}
    dialog.tasks_table.selectRow(0)
    before = datetime.now(timezone.utc)
    dialog._snooze_step()
    wait_idle(dialog)
    until = datetime.fromisoformat(dialog.service.calls[-1][2]["snoozed_until"])
    assert 3590 < (until - before).total_seconds() < 3610
    dialog.tasks_table.selectRow(0)
    dialog._unsnooze_step()
    wait_idle(dialog)
    assert dialog.service.calls[-1][2] == {"snoozed_until": None}
    dialog.tasks_table.selectRow(0)
    dialog.due_enabled.setChecked(False)
    dialog._save_step()
    wait_idle(dialog)
    assert dialog.service.calls[-1][2]["due_at"] is None


def test_priority_edit_does_not_confirm_reported_completion(dialog):
    dialog.category.setCurrentIndex(dialog.category.findData("recent"))
    dialog.tasks_table.selectRow(0)
    assert not dialog.confirm_done_button.isHidden()
    dialog.priority_combo.setCurrentIndex(dialog.priority_combo.findData(2))
    dialog._save_step()
    wait_idle(dialog)
    assert "status" not in dialog.service.calls[-1][2]
    dialog.tasks_table.selectRow(0)
    dialog.confirm_done_button.click()
    wait_idle(dialog)
    assert dialog.service.calls[-1][2] == {"status": "done"}


def test_sync_worker_is_nonblocking_single_flight_and_reports_failure(dialog):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def delayed_sync():
        calls.append(1)
        entered.set()
        release.wait(timeout=2)
        raise ValueError("테스트 로그 읽기 오류")

    dialog.service.sync = delayed_sync
    try:
        dialog.sync()
        assert entered.wait(timeout=1)
        assert dialog._busy
        assert not dialog.sync_button.isEnabled()
        dialog.sync()
        assert len(calls) == 1
        _APP.processEvents()
        assert dialog.tabs.isEnabled()
    finally:
        release.set()
        wait_idle(dialog)
    assert "테스트 로그 읽기 오류" in dialog.status_label.text()
    assert dialog.sync_button.isEnabled()


def test_suggestion_and_manual_plan_use_explicit_user_input(dialog, monkeypatch):
    dialog.category.setCurrentIndex(dialog.category.findData("suggested"))
    dialog.tasks_table.selectRow(0)
    assert not dialog.save_step_button.isEnabled()
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("배포 확인", True))
    dialog._confirm_suggestion()
    wait_idle(dialog)
    assert ("confirm", "codex:session-1", "배포 확인") in dialog.service.calls
    dialog.sessions_list.setCurrentRow(0)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args, **kwargs: ("빌드\n\n배포", True))
    dialog._create_manual_plan()
    wait_idle(dialog)
    assert dialog.service.calls[-1] == ("plan", "codex:session-1", "외부 작업",
                                       [{"description": "빌드", "status": "pending"}, {"description": "배포", "status": "pending"}])


def test_connected_source_pause_delete_and_notifications(dialog, monkeypatch):
    dialog.service.add_source("claude_code", "C:/claude/projects")
    dialog.refresh()
    assert dialog.source_table.item(0, 1).text() == "Claude Code"
    dialog.source_table.selectRow(0)
    dialog._toggle_source()
    wait_idle(dialog)
    assert dialog.service.connected[0]["enabled"] is False
    dialog.notifications.setChecked(True)
    assert dialog.service.notifications_enabled is True
    dialog.source_table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    dialog._delete_source()
    assert dialog.service.connected
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    dialog._delete_source()
    wait_idle(dialog)
    assert dialog.service.connected == []
    assert ("delete", "source-1", True) in dialog.service.calls


def test_main_window_exposes_reminder_action_without_an_extra_toolbar_button():
    from ui.main_window import JarvisMainWindow
    window = JarvisMainWindow()
    try:
        assert window.continuity_action.text() == "통합 리마인더"
        assert window.task_btn.menu() is window.task_menu
        window.set_continuity_service(Service())
        first = window.show_continuity_reminder()
        assert first is window.open_interface_surface("reminders")
        assert first.service is window.continuity_service
        first.refresh_timer.stop()
        first.close()
    finally:
        window.close()
        window.deleteLater()
        _APP.processEvents()
