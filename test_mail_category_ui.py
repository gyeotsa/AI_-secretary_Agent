"""Synthetic category editing and collection lifecycle; no user mail or token."""
import copy
import os
import threading
import time
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QApplication

from core.mail_analysis import MailAnalysisError
from core.turn_context import current_turn_context, check_turn_cancelled
from ui.mail_category_dialog import MailCategoryDialog
from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS


SETTINGS = {"enabled": True, "categories": [
    {"id": "work", "name": "업무", "description": "업무 요청과 협업", "notify": True},
    {"id": "advertising", "name": "광고", "description": "프로모션", "notify": False},
    {"id": "other", "name": "기타", "description": "다른 기준에 해당하지 않는 메일", "notify": False},
]}
ITEMS = [
    {"sender": "Alice", "subject": "Fixture task", "date": "2026-01-01", "unread": True, "message_ref": "fixture-ref"},
    {"sender": "Bob", "subject": "Fixture offer", "date": "2026-01-02", "unread": False, "message_ref": "fixture-offer"},
]


class Analysis:
    def __init__(self, enabled=True):
        self.saved = copy.deepcopy(SETTINGS)
        self.saved["enabled"] = enabled
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.error = None

    def settings(self):
        return copy.deepcopy(self.saved)

    def save_settings(self, enabled, categories):
        self.saved = {"enabled": enabled, "categories": copy.deepcopy(categories)}

    def collect(self, provider, account, items, read_message, *, checkpoint, progress):
        self.calls.append((provider, account, items, read_message))
        self.entered.set()
        progress({"processed": 1, "partial": 0, "skipped": 0, "failed": 0, "total": len(items)})
        self.release.wait(3)
        checkpoint()
        if self.error:
            raise self.error
        return {"processed": 1, "partial": 0, "skipped": len(items) - 1, "failed": 0}


@pytest.fixture(scope="module", autouse=True)
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.instance().processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


@pytest.fixture
def inbox():
    analysis = Analysis()
    credentials = Mock()
    credentials.status.return_value = {"configured": True}
    service = Mock(connection_credentials=credentials, analysis=analysis)
    service.list_current.return_value = {
        "provider": "gmail", "account": "fixture@example.test", "scope": "current_view",
        "items": copy.deepcopy(ITEMS), "observed_count": len(ITEMS),
    }
    dialog = MailInboxDialog(service)
    yield dialog, service, analysis
    analysis.release.set()
    dialog.reject()
    assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_categories_edit_add_delete_notify_and_save_persist_on_reopen():
    analysis = Analysis()
    dialog = MailCategoryDialog(analysis)
    changes = []
    dialog.settings_changed.connect(lambda: changes.append(True))
    dialog.table.setCurrentCell(2, 0)
    assert not dialog.delete_button.isEnabled()
    dialog._delete()
    assert dialog.table.rowCount() == 3
    dialog._add()
    dialog.table.item(3, 0).setText("계약")
    dialog.table.item(3, 1).setText("계약서 검토와 서명 요청")
    dialog.table.item(3, 2).setCheckState(Qt.CheckState.Checked)
    dialog.enabled.setChecked(False)
    dialog.save_button.click()
    assert changes == [True]
    assert analysis.saved["enabled"] is False
    assert analysis.saved["categories"][3]["name"] == "계약"
    assert analysis.saved["categories"][3]["notify"] is True
    reopened = MailCategoryDialog(analysis)
    assert not reopened.enabled.isChecked() and reopened.table.rowCount() == 4
    reopened.table.setCurrentCell(3, 0)
    reopened.delete_button.click()
    reopened.save_button.click()
    assert len(analysis.saved["categories"]) == 3


@pytest.mark.parametrize("name,description", [("", ""), ("x" * 41, ""), ("업무", ""), ("Valid", "x" * 301)])
def test_invalid_category_does_not_save(name, description):
    analysis = Analysis()
    dialog = MailCategoryDialog(analysis)
    dialog._add()
    dialog.table.item(3, 0).setText(name)
    dialog.table.item(3, 1).setText(description)
    dialog.save_button.click()
    assert analysis.saved == SETTINGS and dialog.result() == 0
    assert dialog.status_label.text() != ""
    dialog.reject()


@pytest.mark.parametrize("operation", ["settings", "save_settings"])
def test_category_failures_never_display_arbitrary_exception(operation):
    analysis = Analysis()
    setattr(analysis, operation, Mock(side_effect=RuntimeError("private-fixture-token-and-mail")))
    dialog = MailCategoryDialog(analysis)
    if operation == "settings":
        assert not dialog.save_button.isEnabled() and dialog.table.rowCount() == 0
    else:
        dialog.save_button.click()
    assert "private-fixture" not in dialog.status_label.text()
    dialog.reject()


def test_category_limit_enforced_on_add():
    dialog = MailCategoryDialog(Analysis())
    for _ in range(25):
        dialog._add()
    assert dialog.table.rowCount() == 20 and not dialog.add_button.isEnabled()
    dialog.reject()


def test_list_displays_before_background_collection_and_body_clicks_are_blocked(inbox):
    dialog, service, analysis = inbox
    analysis.release.clear()
    dialog.list_button.click()
    assert pump_until(analysis.entered.is_set)
    assert dialog.table.rowCount() == 2 and len(dialog._items) == 2
    assert dialog._worker.operation == "collect" and not dialog.category_button.isEnabled()
    assert pump_until(lambda: "분류 완료 1건" in dialog.status_label.text())
    dialog.table.cellClicked.emit(0, 1)
    assert dialog._body_view is None
    service.read_message.assert_not_called()
    dialog.search_input.setText("offer")
    assert dialog.table.rowCount() == 1 and not dialog._worker.cancelled.is_set()
    analysis.release.set()
    assert pump_until(lambda: dialog._worker is None)
    assert "처리 종료" in dialog.status_label.text() and "기존 기록 1건" in dialog.status_label.text()
    provider, account, items, reader = analysis.calls[0]
    assert provider == "gmail" and account == "fixture@example.test" and items == ITEMS
    assert reader is service.read_message and len(dialog._items) == 2
    assert dialog.category_button.isEnabled()


def test_disabled_analysis_never_starts_collection(inbox):
    dialog, service, analysis = inbox
    analysis.saved["enabled"] = False
    dialog.list_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert not analysis.calls and dialog.table.rowCount() == 2
    assert "꺼짐" in dialog.analysis_label.text()
    service.read_message.assert_not_called()


def test_collection_failure_preserves_list_and_sanitizes_errors(inbox):
    dialog, _service, analysis = inbox
    analysis.error = RuntimeError("private-synthetic-mail-and-token")
    dialog.list_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert analysis.calls and len(dialog._items) == 2 and dialog.table.rowCount() == 2
    assert "private" not in dialog.status_label.text() and "다시 조회" in dialog.status_label.text()


def test_collection_authored_failure_is_visible(inbox):
    dialog, _service, analysis = inbox
    analysis.error = MailAnalysisError("메일 본문 수집이 중단되었습니다. 목록을 다시 조회하세요.")
    dialog.list_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert dialog.status_label.text() == str(analysis.error)
    assert len(dialog._items) == 2


def test_collect_binds_own_turn_context_and_cancel_prevents_late_save(inbox):
    dialog, service, analysis = inbox
    entered, release = threading.Event(), threading.Event()
    contexts, saved = [], []

    def collect(*_args, **_kwargs):
        contexts.append(current_turn_context())
        entered.set()
        release.wait(3)
        check_turn_cancelled()
        saved.append("must not be saved after cancellation")
        return {"processed": 2, "partial": 0, "skipped": 0, "failed": 0}

    analysis.collect = collect
    try:
        dialog.list_button.click()
        assert pump_until(entered.is_set)
        context = contexts[0]
        assert context is not None and context.session_id == "mail"
        assert context is dialog._worker.context and not context.cancelled
        dialog.cancel_button.click()
        assert context.cancelled
        release.set()
        assert pump_until(lambda: dialog._worker is None)
        assert not saved and not dialog._items and service.disconnect.called
        assert current_turn_context() is None
    finally:
        release.set()


@pytest.mark.parametrize("action", ["cancel", "provider", "disconnect", "close"])
def test_cancel_collection_clears_context_and_discards_late_results(inbox, action):
    dialog, service, analysis = inbox
    analysis.release.clear()
    dialog.list_button.click()
    assert pump_until(analysis.entered.is_set)
    if action == "provider":
        dialog.provider.setCurrentIndex(1)
    elif action == "disconnect":
        dialog.disconnect_button.click()
    elif action == "close":
        dialog.reject()
    else:
        dialog.cancel_button.click()
    assert not dialog._items and dialog.table.rowCount() == 0
    analysis.release.set()
    assert pump_until(lambda: dialog._worker is None)
    assert not dialog._items and dialog.table.rowCount() == 0 and service.disconnect.called
    assert "처리 종료" not in dialog.status_label.text()


def test_category_modal_blocks_parent_and_saved_settings_apply_on_next_query(inbox, monkeypatch):
    dialog, _service, analysis = inbox

    def category_dialog(service, parent):
        child = MailCategoryDialog(service, parent)

        def save():
            assert not parent.category_button.isEnabled() and not parent.provider.isEnabled()
            assert not parent.list_button.isEnabled() and not parent.connection_button.isEnabled()
            child.enabled.setChecked(False)
            child.save_button.click()

        QTimer.singleShot(0, save)
        return child

    monkeypatch.setattr("ui.mail_inbox_dialog.MailCategoryDialog", category_dialog)
    dialog.category_button.click()
    assert analysis.saved["enabled"] is False and "꺼짐" in dialog.analysis_label.text()
    assert "새로고침" in dialog.status_label.text() and dialog.list_button.isEnabled()
    dialog.list_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert not analysis.calls
