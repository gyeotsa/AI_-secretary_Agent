"""Synthetic token registration UI; never reads the user's extension token."""
import os
import threading
import time
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit

from core.browser_extension import BrowserExtensionCredentialsError
from ui.mail_connection_dialog import MailConnectionDialog
from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS


TOKEN = "synthetic-connection-token"


class Credentials:
    def __init__(self, configured=False):
        self.configured = configured
        self.saved = []

    def status(self):
        return {"configured": self.configured}

    def save(self, token):
        self.saved.append(token)
        self.configured = True
        return self.status()

    def delete(self):
        self.configured = False
        return self.status()


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


def test_settings_mask_token_clear_after_save_and_never_prefill():
    credentials = Credentials()
    dialog = MailConnectionDialog(credentials)
    changes = []
    dialog.registration_changed.connect(lambda: changes.append(True))
    assert dialog.token_input.echoMode() == QLineEdit.EchoMode.Password
    assert dialog.token_input.inputMethodHints() & Qt.InputMethodHint.ImhSensitiveData
    assert not dialog.save_button.isEnabled() and not dialog.delete_button.isEnabled()
    dialog.token_input.setText(TOKEN)
    dialog.save_button.click()
    assert credentials.saved == [TOKEN] and changes == [True]
    assert dialog.token_input.text() == "" and TOKEN not in dialog.status_label.text()
    dialog.token_input.undo()
    assert not dialog.token_input.text()
    assert "등록됨" in dialog.status_label.text() and "아직 확인" in dialog.status_label.text()
    dialog.reject()
    reopened = MailConnectionDialog(credentials)
    assert reopened.token_input.text() == "" and reopened.delete_button.isEnabled()
    reopened.reject()


@pytest.mark.parametrize("close", ["reject", "close", "accept"])
def test_settings_close_always_clears_unsaved_token(close):
    credentials = Credentials()
    dialog = MailConnectionDialog(credentials)
    dialog.show()
    dialog.token_input.setText(TOKEN)
    getattr(dialog, close)()
    assert dialog.token_input.text() == "" and not credentials.saved


def test_settings_delete_clears_input_and_registration_status():
    credentials = Credentials(configured=True)
    dialog = MailConnectionDialog(credentials)
    changes = []
    dialog.registration_changed.connect(lambda: changes.append(True))
    dialog.token_input.setText(TOKEN)
    dialog.delete_button.click()
    assert not credentials.configured and changes == [True]
    assert dialog.token_input.text() == "" and not dialog.delete_button.isEnabled()
    assert "미등록" in dialog.status_label.text()
    dialog.reject()


@pytest.mark.parametrize("operation", ["status", "save", "delete"])
def test_settings_arbitrary_errors_cannot_expose_token(operation):
    credentials = Credentials(configured=True)
    if operation == "status":
        credentials.status = Mock(side_effect=RuntimeError(TOKEN))
    dialog = MailConnectionDialog(credentials)
    if operation != "status":
        setattr(credentials, operation, Mock(side_effect=RuntimeError(TOKEN)))
        dialog.token_input.setText(TOKEN)
        getattr(dialog, operation + "_button").click()
    assert TOKEN not in dialog.status_label.text() and not dialog.token_input.text()
    dialog.reject()


def test_settings_app_authored_error_is_visible_but_input_is_cleared():
    credentials = Credentials()
    credentials.save = Mock(side_effect=BrowserExtensionCredentialsError("토큰만 입력하세요."))
    dialog = MailConnectionDialog(credentials)
    dialog.token_input.setText(TOKEN)
    dialog.save_button.click()
    assert dialog.status_label.text() == "토큰만 입력하세요."
    assert not dialog.token_input.text() and not dialog.delete_button.isEnabled()
    dialog.reject()


def test_mail_settings_waits_for_disconnect_and_refreshes_registration(monkeypatch):
    credentials = Credentials()
    service = Mock(connection_credentials=credentials)
    entered, release = threading.Event(), threading.Event()

    def disconnect():
        entered.set()
        release.wait(3)

    service.disconnect.side_effect = disconnect
    dialog = MailInboxDialog(service)
    opened = []

    def settings(parent_credentials, parent):
        assert parent_credentials is credentials and parent._worker is None
        child = MailConnectionDialog(parent_credentials, parent)
        opened.append(child)

        def save_and_close():
            assert not parent.connection_button.isEnabled()
            assert not parent.list_button.isEnabled() and not parent.provider.isEnabled()
            child.token_input.setText(TOKEN)
            child.save_button.click()
            assert "등록됨" in parent.connection_label.text()
            child.reject()

        QTimer.singleShot(0, save_and_close)
        return child

    monkeypatch.setattr("ui.mail_inbox_dialog.MailConnectionDialog", settings)
    try:
        dialog.connection_button.click()
        assert pump_until(entered.is_set)
        assert not opened and not dialog.provider.isEnabled() and not dialog.connection_button.isEnabled()
        release.set()
        assert pump_until(lambda: dialog._worker is None and bool(opened))
        assert service.disconnect.call_count == 1
        assert opened[0].token_input.text() == "" and "등록됨" in dialog.connection_label.text()
        assert dialog.provider.isEnabled() and dialog.connection_button.isEnabled()
    finally:
        release.set()
        dialog.reject()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_mail_busy_work_cannot_open_or_change_connection_settings(monkeypatch):
    service = Mock(connection_credentials=Credentials())
    entered, release = threading.Event(), threading.Event()

    def list_current(*args, checkpoint, **kwargs):
        entered.set()
        release.wait(3)
        checkpoint()

    service.list_current.side_effect = list_current
    dialog = MailInboxDialog(service)
    show = Mock()
    monkeypatch.setattr(dialog, "_show_connection_settings", show)
    try:
        dialog._start("list")
        assert pump_until(entered.is_set)
        assert not dialog.connection_button.isEnabled()
        dialog._request_connection_settings()
        assert not dialog._pending_connection_settings
        dialog.reject()
        release.set()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
        show.assert_not_called()
    finally:
        release.set()
        dialog.reject()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_mail_cancelled_disconnect_does_not_open_settings(monkeypatch):
    service = Mock(connection_credentials=Credentials())
    entered, release = threading.Event(), threading.Event()
    service.disconnect.side_effect = lambda: entered.set() or release.wait(3)
    dialog = MailInboxDialog(service)
    show = Mock()
    monkeypatch.setattr(dialog, "_show_connection_settings", show)
    try:
        dialog.connection_button.click()
        assert pump_until(entered.is_set)
        dialog._cancel()
        release.set()
        assert pump_until(lambda: dialog._worker is None)
        assert not dialog._pending_connection_settings
        show.assert_not_called()
    finally:
        release.set()
        dialog.reject()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_idle_mail_provider_change_uses_switch_without_disconnecting():
    service = Mock(connection_credentials=Credentials(configured=True))
    dialog = MailInboxDialog(service)
    try:
        dialog.provider.setCurrentIndex(1)
        assert pump_until(lambda: dialog._worker is None)
        service.switch_provider.assert_called_once_with()
        service.disconnect.assert_not_called()
        assert "전환했습니다" in dialog.status_label.text()
        assert "등록됨" in dialog.connection_label.text()
        assert not dialog._items
    finally:
        dialog.reject()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
