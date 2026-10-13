"""Offscreen OAuth UI; no browser launch or real provider request."""
import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from core.plugin import ToolCancelledError
from core.remote_runtime import OAuthCoordinator
from test_oauth_connection import Vault
from ui.oauth_account_dialog import OAuthAccountDialog, _ACTIVE_CONNECTIONS


_APP = None


def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app().processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class OAuth:
    SPECS = OAuthCoordinator.SPECS

    def __init__(self):
        self.vault = Vault()
        self.calls = []

    def status(self, provider, account):
        self.calls.append(("status", provider, account))
        token = self.vault.load(provider, account)
        return {"provider": provider, "account": account, "configured": bool(token),
                "authenticated": bool(token), "identity_email": account if token else "",
                "identity_verified_at": time.time() if token else None,
                "scopes_verified": False, "granted_scopes": [], "refresh_available": bool(token)}


class Connection:
    def __init__(self, oauth):
        self.oauth = oauth
        self.started, self.release, self.cancelled = threading.Event(), threading.Event(), threading.Event()
        self.release.set()
        self.thread_id = None
        self.error = False
        self.cooperative = True
        self.kwargs = None

    def cancel(self):
        self.cancelled.set()

    def connect(self, provider, account, **kwargs):
        self.thread_id = threading.get_ident()
        self.kwargs = kwargs
        self.started.set()
        kwargs["on_browser"]("http://127.0.0.1:8765/oauth/callback")
        while not self.release.wait(0.005):
            if self.cooperative and self.cancelled.is_set():
                raise ToolCancelledError("fixture-secret")
        if self.cancelled.is_set():
            raise ToolCancelledError("fixture-secret")
        if self.error:
            raise RuntimeError("fixture-secret")
        self.oauth.vault.save(provider, account, {"access_token": "fixture-secret"})
        return self.oauth.status(provider, account)


@pytest.fixture
def make_dialog(monkeypatch):
    app()
    for spec in OAuth.SPECS.values():
        monkeypatch.delenv(spec["client_id"], raising=False)
    created = []

    def create():
        oauth = OAuth()
        connection = Connection(oauth)
        dialog = OAuthAccountDialog(oauth, connection_factory=lambda _oauth: connection)
        dialog.show()
        dialog.account_input.setText("owner@example.com")
        dialog.client_id_input.setText("fixture-client")
        created.append((dialog, connection))
        return dialog, oauth, connection

    yield create
    for dialog, connection in created:
        connection.release.set()
        try:
            dialog.reject()
        except RuntimeError:
            pass
    assert pump_until(lambda: not _ACTIVE_CONNECTIONS)
    for dialog, _ in created:
        try:
            dialog.deleteLater()
        except RuntimeError:
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def text(dialog):
    return "\n".join(label.text() for label in dialog.findChildren(QLabel))


def test_direct_input_background_connection_clears_secret_and_reports_scope_unknown(make_dialog, capsys):
    dialog, oauth, connection = make_dialog()
    connection.release.clear()
    dialog.client_secret_input.setText("fixture-secret")
    gui_thread = threading.get_ident()
    dialog.connect_button.click()
    assert dialog.client_secret_input.text() == ""
    assert pump_until(connection.started.is_set)
    assert connection.thread_id != gui_thread
    assert not dialog.account_input.isEnabled() and not dialog.disconnect_button.isEnabled()
    assert dialog.cancel_button.isEnabled()
    assert pump_until(lambda: "아직 연결 완료가 아닙니다" in dialog.status_label.text())
    connection.release.set()
    assert pump_until(lambda: dialog._worker is None)
    assert "암호화 저장됨" in text(dialog) and "허용 범위 미확인" in text(dialog)
    assert "현재 API 접속" in text(dialog) and "fixture-secret" not in text(dialog)
    assert connection.kwargs["client_secret"] == "fixture-secret"
    assert oauth.vault.load("google", "owner@example.com")
    output = capsys.readouterr()
    assert "fixture-secret" not in output.out + output.err


def test_cancel_remains_responsive_and_does_not_save(make_dialog):
    dialog, oauth, connection = make_dialog()
    connection.release.clear()
    dialog.connect_button.click()
    assert pump_until(connection.started.is_set)
    dialog.cancel_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert dialog.isVisible() and not oauth.vault.tokens and "취소" in text(dialog)


@pytest.mark.parametrize("method", ["close", "reject", "done"])
def test_close_waits_for_bounded_call_then_disposes_worker(make_dialog, method):
    dialog, oauth, connection = make_dialog()
    connection.release.clear()
    connection.cooperative = False
    dialog.connect_button.click()
    assert pump_until(connection.started.is_set)
    if method == "done":
        dialog.done(0)
    else:
        getattr(dialog, method)()
    app().processEvents()
    assert dialog.isVisible() and dialog._worker.isRunning()
    connection.release.set()
    assert pump_until(lambda: not dialog.isVisible() and not _ACTIVE_CONNECTIONS)
    assert not oauth.vault.tokens


def test_direct_qobject_destruction_cancels_retained_worker(make_dialog):
    dialog, oauth, connection = make_dialog()
    connection.release.clear()
    dialog.connect_button.click()
    assert pump_until(connection.started.is_set)
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert pump_until(lambda: not _ACTIVE_CONNECTIONS)
    assert connection.cancelled.is_set() and not oauth.vault.tokens


def test_microsoft_requires_exact_fixed_port_and_provider_change_clears_secret(make_dialog):
    dialog, _oauth, _connection = make_dialog()
    dialog.client_secret_input.setText("fixture-secret")
    dialog.provider.setCurrentIndex(1)
    dialog.client_id_input.setText("fixture-client")
    assert dialog.client_secret_input.text() == "" and dialog.port.value() > 0
    assert "http://127.0.0.1:" in dialog.setup_label.text() and "Public client" in dialog.setup_label.text()
    dialog.port.setValue(0)
    assert not dialog.connect_button.isEnabled()


def test_failure_never_leaks_exception_or_claims_saved(make_dialog):
    dialog, oauth, connection = make_dialog()
    connection.error = True
    dialog.connect_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert not oauth.vault.tokens and "완료하지 못했습니다" in dialog.status_label.text()
    assert "fixture-secret" not in text(dialog)


def test_wrong_status_target_is_rejected_and_disconnect_is_scoped_confirmed(make_dialog, monkeypatch):
    dialog, oauth, _connection = make_dialog()
    oauth.vault.save("google", "owner@example.com", {"access_token": "fixture-secret"})
    oauth.vault.save("microsoft", "owner@example.com", {"access_token": "other"})
    dialog.status_button.click()
    assert dialog.disconnect_button.isEnabled()
    with pytest.raises(ValueError):
        dialog._apply_status({"provider": "microsoft", "account": "owner@example.com", "configured": True})
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    dialog.disconnect_button.click()
    assert oauth.vault.load("google", "owner@example.com")
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    dialog.disconnect_button.click()
    assert not oauth.vault.load("google", "owner@example.com")
    assert oauth.vault.load("microsoft", "owner@example.com")
    assert not dialog.disconnect_button.isEnabled()


@pytest.mark.parametrize("email_scope", ["email", "https://www.googleapis.com/auth/userinfo.email"])
def test_gmail_mode_explains_desktop_setup_and_requests_readonly_identity_scopes(monkeypatch, email_scope):
    app()
    oauth = OAuth()
    oauth.SPECS = {"google": {**OAuth.SPECS["google"], "scopes": [
        "openid", email_scope, "https://www.googleapis.com/auth/gmail.readonly"]}}
    oauth.active_account = lambda: "owner@example.com"
    monkeypatch.delenv(oauth.SPECS["google"]["client_id"], raising=False)
    dialog = OAuthAccountDialog(oauth, gmail_only=True)
    try:
        assert dialog.windowTitle() == "Gmail 읽기 연결" and dialog.provider.count() == 1
        assert dialog.account_input.text() == "owner@example.com"
        assert not dialog.client_id_input.text() and dialog.client_secret_input.text() == ""
        assert not dialog.connect_button.isEnabled() and dialog.port.isHidden()
        setup = dialog.setup_label.text()
        for required in ("Gmail API", "Branding", "Audience", "External", "Test users", "Data Access",
                         "gmail.readonly", "openid", "userinfo.email", "Clients", "Desktop app", "7일"):
            assert required in setup
        assert "Calendar" not in setup and "Drive" not in setup and "Microsoft" not in setup
        assert dialog.setup_label.openExternalLinks()
        assert 'href="https://developers.google.com/' in setup
        assert "전송·삭제·읽음 변경 권한은 요청하지 않습니다" in setup
        assert "연결이 없습니다" in dialog.status_label.text()
    finally:
        dialog.reject()


def test_account_disconnect_uses_service_guard_when_available(make_dialog, monkeypatch):
    dialog, oauth, _connection = make_dialog()
    oauth.vault.save("google", "owner@example.com", {"access_token": "fixture-secret"})
    calls = []

    def disconnect(provider, account):
        calls.append((provider, account))
        oauth.vault.delete(provider, account)

    oauth.disconnect = disconnect
    dialog.status_button.click()
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    dialog.disconnect_button.click()
    assert calls == [("google", "owner@example.com")]
    assert not dialog.disconnect_button.isEnabled()


def test_gmail_mode_rejects_generic_write_capable_oauth_coordinator():
    app()
    with pytest.raises(ValueError, match="읽기 전용"):
        OAuthAccountDialog(OAuth(), gmail_only=True)


def test_gmail_dialog_uses_actual_readonly_service_contract_without_saved_account_or_network(monkeypatch):
    from core.background_mail import GmailMailAccountService
    from unittest.mock import Mock
    app()
    vault = Vault()
    session = SimpleNamespace(get=Mock(side_effect=AssertionError("unexpected network")),
                              post=Mock(side_effect=AssertionError("unexpected network")))
    oauth = GmailMailAccountService(vault=vault, session=session)
    monkeypatch.delenv(oauth.SPECS["google"]["client_id"], raising=False)
    dialog = OAuthAccountDialog(oauth, gmail_only=True)
    try:
        assert dialog.windowTitle() == "Gmail 읽기 연결" and dialog.provider.count() == 1
        assert dialog.account_input.text() == "" and dialog.client_id_input.text() == ""
        assert not dialog.connect_button.isEnabled()
        assert not vault.tokens
        session.get.assert_not_called()
        session.post.assert_not_called()
    finally:
        dialog.reject()


def test_plugin_hub_and_diagnostics_buttons_open_account_dialog(monkeypatch):
    from ui.plugin_hub import PluginHub
    from ui.main_window import PluginDiagnosticsDialog
    app()
    oauth = OAuth()
    registry = SimpleNamespace(get_plugin=lambda name: SimpleNamespace(oauth=oauth) if name == "cloud_communication" else None,
                               get_plugin_statuses=lambda: [])
    opened = []
    monkeypatch.setattr(OAuthAccountDialog, "exec", lambda self: opened.append(self.oauth) or 0)
    for cls in (PluginHub, PluginDiagnosticsDialog):
        parent = cls(registry)
        parent.oauth_account_button.click()
        parent.close()
        parent.deleteLater()
    assert opened == [oauth, oauth]
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
