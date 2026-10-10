"""Synthetic inline body rendering; never opens the user's browser or credentials."""
import os
import threading
import time
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPlainTextEdit

from core.browser_mail import BrowserMailError
from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS


ACCOUNT = "fixture@example.test"
ITEMS = [
    {"sender": "Alice", "subject": "Alpha", "date": "2026-01-01", "unread": True, "message_ref": "ref-alpha"},
    {"sender": "Bob", "subject": "Beta", "date": "2026-01-02", "unread": False, "message_ref": "ref-beta"},
]


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


def body(message_ref="ref-alpha", text="Synthetic mail body", **overrides):
    return {"provider": "gmail", "account": ACCOUNT, "message_ref": message_ref,
            "subject": "Alpha", "sender": "Alice", "date": "2026-01-01", "body": text, **overrides}


@pytest.fixture
def inbox():
    credentials = Mock()
    credentials.status.return_value = {"configured": True}
    service = Mock(connection_credentials=credentials)
    service.read_message.side_effect = lambda _provider, ref, **_kwargs: body(ref)
    dialog = MailInboxDialog(service)
    dialog._apply_result({"provider": "gmail", "account": ACCOUNT, "scope": "current_view",
                          "items": [dict(item) for item in ITEMS], "observed_count": 2})
    yield dialog, service
    dialog.reject()
    assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_click_expands_below_row_plaintext_and_second_click_collapses(inbox):
    dialog, service = inbox
    malicious = '<script>alert("fixture")</script><img src="https://example.test/tracker">\nLiteral body'
    service.read_message.side_effect = lambda _provider, ref, **_kwargs: body(ref, malicious)
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert dialog.table.rowCount() == 3 and dialog._body_row == 1
    viewer = dialog.table.cellWidget(1, 0)
    assert isinstance(viewer, QPlainTextEdit) and viewer.isReadOnly()
    assert viewer.toPlainText() == malicious and viewer.accessibleName() == "선택한 메일 본문"
    assert dialog.table.item(2, 1).text() == "Beta" and dialog.table.rowHeight(1) == 240
    assert dialog.table.columnSpan(1, 0) == 4
    dialog.table.cellClicked.emit(0, 0)
    assert dialog.table.rowCount() == 2 and dialog._body_view is None
    assert len(dialog._items) == 2 and service.read_message.call_count == 1


def test_filtered_row_uses_selected_ref_and_only_one_body_is_expanded(inbox):
    dialog, service = inbox
    dialog.search_input.setText("Beta")
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert service.read_message.call_args.args == ("gmail", "ref-beta")
    assert dialog.table.rowCount() == 2
    dialog.search_input.clear()
    assert dialog.table.rowCount() == 2 and dialog._body_view is None
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    dialog.table.cellClicked.emit(2, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert dialog._body_ref == "ref-beta" and dialog._body_row == 2
    assert dialog.table.rowCount() == 3 and service.read_message.call_args.args == ("gmail", "ref-beta")


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Space])
def test_keyboard_activation_and_legacy_row_without_reference(inbox, key):
    dialog, service = inbox
    dialog.table.setCurrentCell(1, 0)
    QTest.keyClick(dialog.table, key)
    assert pump_until(lambda: dialog._worker is None)
    assert dialog._body_ref == "ref-beta"
    QTest.keyClick(dialog.table, key)
    assert dialog._body_view is None
    dialog._apply_result({"provider": "gmail", "account": ACCOUNT, "scope": "current_view",
                          "items": [{key: value for key, value in ITEMS[0].items() if key != "message_ref"}],
                          "observed_count": 1})
    dialog.table.cellClicked.emit(0, 0)
    assert dialog._worker is None and dialog.table.rowCount() == 1 and service.read_message.call_count == 1


def test_body_failure_redacts_arbitrary_error_and_reclick_retries(inbox):
    dialog, service = inbox
    service.read_message.side_effect = RuntimeError("private-fixture-page-and-token")
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert dialog._body_failed and "private-fixture" not in dialog._body_view.toPlainText()
    assert dialog.table.rowCount() == 3 and len(dialog._items) == 2
    service.read_message.side_effect = lambda _provider, ref, **_kwargs: body(ref, "Retry succeeded")
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert dialog._body_view.toPlainText() == "Retry succeeded" and not dialog._body_failed


@pytest.mark.parametrize("invalid", [{"provider": "naver"}, {"account": "other@example.test"},
                                    {"message_ref": "other-ref"}, {"body": None}, {"truncated": "yes"}])
def test_mismatched_or_invalid_body_never_displays_content(inbox, invalid):
    dialog, service = inbox
    service.read_message.side_effect = lambda *_args, **_kwargs: body(text="private-synthetic-body", **invalid)
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert dialog._body_failed and "private-synthetic-body" not in dialog._body_view.toPlainText()


def test_truncated_body_is_explicitly_labeled(inbox):
    dialog, service = inbox
    service.read_message.side_effect = lambda *_args, **_kwargs: body(text="Short fixture", truncated=True)
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert "일부만 표시" in dialog._body_view.toPlainText()


def test_collapse_during_fetch_discards_late_result_without_removing_list(inbox):
    dialog, service = inbox
    entered, release = threading.Event(), threading.Event()

    def delayed(_provider, ref, *, checkpoint):
        entered.set()
        release.wait(3)
        checkpoint()
        return body(ref, "late synthetic body")

    service.read_message.side_effect = delayed
    try:
        dialog.table.cellClicked.emit(0, 1)
        assert pump_until(entered.is_set)
        assert dialog.table.rowCount() == 3 and len(dialog._items) == 2
        assert not dialog.refresh_button.isEnabled()
        dialog.table.cellClicked.emit(0, 1)
        assert dialog.table.rowCount() == 2
        release.set()
        assert pump_until(lambda: dialog._worker is None)
        assert dialog._body_view is None and len(dialog._items) == 2
        service.disconnect.assert_not_called()
    finally:
        release.set()


@pytest.mark.parametrize("action", ["filter", "provider", "disconnect", "reject", "workspace_close"])
def test_context_change_cancels_body_and_clears_all_late_results(inbox, action):
    dialog, service = inbox
    entered, release = threading.Event(), threading.Event()

    def delayed(_provider, ref, *, checkpoint):
        entered.set()
        release.wait(3)
        return body(ref, "private late body")

    service.read_message.side_effect = delayed
    try:
        dialog.table.cellClicked.emit(0, 1)
        assert pump_until(entered.is_set)
        if action == "filter":
            dialog.search_input.setText("Beta")
        elif action == "provider":
            dialog.provider.setCurrentIndex(1)
        elif action == "disconnect":
            dialog.disconnect_button.click()
        elif action == "workspace_close":
            dialog.can_close_workspace_tab()
        else:
            dialog.reject()
        assert not dialog._items and dialog._body_view is None and dialog.table.rowCount() == 0
        release.set()
        assert pump_until(lambda: dialog._worker is None)
        assert not dialog._items and dialog._body_view is None and dialog.table.rowCount() == 0
        assert service.disconnect.called
    finally:
        release.set()


def test_close_after_body_worker_finishes_before_queued_ui_delivery(inbox):
    dialog, service = inbox
    dialog.table.cellClicked.emit(0, 1)
    assert dialog._worker.wait(3000)
    dialog.can_close_workspace_tab()
    assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
    assert dialog._body_view is None and not dialog._items and service.disconnect.called
