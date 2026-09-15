"""Real mail plugin/UI wiring, using only in-memory accounts and fake servers."""
import copy
import os
import ssl

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from core.mail_accounts import MailAccountService
from core.plugin import PluginRegistry, ToolCancelledError
from plugins.mail import MailPlugin


class Vault:
    def __init__(self):
        self.record = None

    def load(self, provider, account):
        assert (provider, account) == (MailAccountService.PROVIDER, MailAccountService.ACCOUNT)
        return copy.deepcopy(self.record)

    def save(self, provider, account, value):
        assert (provider, account) == (MailAccountService.PROVIDER, MailAccountService.ACCOUNT)
        self.record = copy.deepcopy(value)


@pytest.fixture
def service(monkeypatch):
    # Deliberately incompatible env account: saved identity/endpoints must win.
    for name, value in {"PROVIDER": "other", "SMTP_HOST": "wrong.example.test",
                        "SMTP_USERNAME": "wrong@example.test", "SMTP_PASSWORD": "wrong-secret",
                        "SMTP_SECURITY": "ssl", "SMTP_PORT": "465",
                        "FROM": "wrong@example.test", "IMAP_HOST": "wrong.example.test",
                        "IMAP_USERNAME": "wrong@example.test", "IMAP_PASSWORD": "wrong-secret"}.items():
        monkeypatch.setenv("MAIL_" + name, value)
    account = MailAccountService(Vault())
    account.save("fixture@naver.com", "fixture-private-password")
    return account


def test_saved_account_is_the_real_imap_source(service, monkeypatch):
    class Reader:
        def __init__(self, settings, checkpoint):
            assert (settings.host, settings.port, settings.username, settings.password) == (
                "imap.naver.com", 993, "fixture", "fixture-private-password")
            checkpoint()

        def list_inbox(self, **kwargs):
            assert kwargs == {"limit": 3}
            return {"items": [], "read_only": True}
    monkeypatch.setattr("plugins.mail.ImapReader", Reader)
    result = MailPlugin(service).execute_tool("mail_list_inbox", {"limit": 3})
    assert result.succeeded
    assert "fixture-private-password" not in str(result.to_dict())


class SMTP:
    def __init__(self, service, *, change_after_login=False):
        self.service, self.change = service, change_after_login
        self.log = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.log.append("exit")

    def ehlo(self):
        self.log.append("ehlo")

    def starttls(self, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        self.log.append("tls")

    def login(self, username, password):
        assert (username, password) == ("fixture", "fixture-private-password")
        self.log.append("login")
        if self.change:
            self.service.disconnect()

    def send_message(self, message):
        self.log.append("send")
        assert message["From"] == "fixture@naver.com"
        assert message["To"] == "recipient@example.test"
        return {}


MESSAGE = {"to": "recipient@example.test", "subject": "fixture", "body": "not sent to any real server"}


@pytest.mark.parametrize("change", [False, True])
def test_saved_account_smtp_and_disconnect_before_send(service, monkeypatch, change):
    smtp = SMTP(service, change_after_login=change)
    def factory(host, port, timeout):
        assert (host, port, timeout) == ("smtp.naver.com", 587, 15)
        return smtp
    monkeypatch.setattr("plugins.mail.smtplib.SMTP", factory)
    plugin = MailPlugin(service)
    if change:
        with pytest.raises(ToolCancelledError):
            plugin.execute_tool("mail_send_smtp", MESSAGE)
        assert "send" not in smtp.log
    else:
        result = plugin.execute_tool("mail_send_smtp", MESSAGE)
        assert result.succeeded and result.evidence[0].data["delivered"] is None
        assert "fixture-private-password" not in str(result.to_dict())
        assert smtp.log == ["ehlo", "tls", "ehlo", "login", "send", "exit"]
    assert smtp.log[-1] == "exit"


@pytest.mark.parametrize("tool,args", [("mail_send_smtp", MESSAGE), ("mail_list_inbox", {}),
                                       ("mail_read_message", {"uid": 1, "uid_validity": 2})])
@pytest.mark.parametrize("state", ["disconnected", "corrupt"])
def test_no_env_fallback_on_disabled_or_corrupt_store(service, monkeypatch, tool, args, state):
    if state == "disconnected":
        service.disconnect()
    else:
        service.vault.record = {"password": "fixture-private-password"}
    def forbidden(*args, **kwargs):
        pytest.fail("network/runtime must not be reached")
    monkeypatch.setattr("plugins.mail.ImapReader", forbidden)
    monkeypatch.setattr("plugins.mail.smtplib.SMTP", forbidden)
    monkeypatch.setattr("plugins.mail.smtplib.SMTP_SSL", forbidden)
    plugin = MailPlugin(service)
    result = plugin.execute_tool(tool, args)
    assert not result.succeeded
    assert "fixture-private-password" not in str(result.to_dict())
    assert plugin.is_authenticated() is False and plugin.is_connected() is False


def test_saved_account_status_does_not_claim_current_login(service):
    plugin = MailPlugin(service)
    assert plugin.is_authenticated() is None and plugin.is_connected() is None


@pytest.mark.parametrize("disconnected", [False, True])
def test_offline_draft_does_not_restore_a_different_env_sender(service, monkeypatch, tmp_path, disconnected):
    from email import policy
    from email.parser import BytesParser
    if disconnected:
        service.disconnect()
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    target = tmp_path / "account-draft.eml"
    result = MailPlugin(service).execute_tool("mail_create_draft", {**MESSAGE, "path": str(target)})
    assert result.succeeded
    mail = BytesParser(policy=policy.default).parsebytes(target.read_bytes())
    assert mail["From"] == (None if disconnected else "fixture@naver.com")


def test_account_change_during_read_discards_stale_result(service, monkeypatch):
    class Reader:
        def __init__(self, settings, checkpoint):
            self.checkpoint = checkpoint

        def list_inbox(self, **kwargs):
            service.save("different", "new-secret")
            self.checkpoint()
            pytest.fail("stale result cannot return")
    monkeypatch.setattr("plugins.mail.ImapReader", Reader)
    with pytest.raises(ToolCancelledError):
        MailPlugin(service).execute_tool("mail_list_inbox", {})


_APP = None


def test_real_diagnostics_button_opens_the_plugins_account_service(service, monkeypatch):
    global _APP
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from ui.main_window import PluginDiagnosticsDialog
    from ui.mail_account_dialog import MailAccountDialog
    _APP = QApplication.instance() or QApplication([])
    registry = PluginRegistry()
    registry.register_plugin(MailPlugin(service))
    parent = PluginDiagnosticsDialog(registry)
    original = MailAccountDialog.exec
    observed = []
    def inspect(dialog):
        assert dialog.service is service
        assert dialog.username_input.text() == "fixture"
        assert dialog.password_input.text() == ""
        observed.append(dialog)
        QTimer.singleShot(0, dialog.reject)
        return original(dialog)
    monkeypatch.setattr(MailAccountDialog, "exec", inspect)
    parent.mail_account_button.click()
    assert len(observed) == 1
    parent.close()


def test_diagnostics_missing_mail_plugin_does_not_create_hollow_connection(monkeypatch):
    global _APP
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from ui.main_window import PluginDiagnosticsDialog
    _APP = QApplication.instance() or QApplication([])
    dialog = PluginDiagnosticsDialog(PluginRegistry())
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args))
    dialog.mail_account_button.click()
    assert len(messages) == 1 and "등록" in messages[0][2]
    dialog.close()


@pytest.mark.integration
def test_windows_dpapi_account_roundtrip_uses_only_synthetic_credentials(tmp_path):
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows DPAPI only")
    pytest.importorskip("win32crypt")
    from core.remote_runtime import SecureTokenVault
    def no_network(*args, **kwargs):
        pytest.fail("DPAPI roundtrip must not contact any server")
    directory = tmp_path / "synthetic-mail-vault"
    secret = "not-a-real-account-password"
    service = MailAccountService(SecureTokenVault(str(directory)),
                                 imap_factory=no_network, smtp_factory=no_network)
    assert service.save("anis-qa-fixture", secret)["verified_at"] is None
    encrypted = list(directory.glob("*.dpapi"))
    assert len(encrypted) == 1
    assert secret.encode() not in encrypted[0].read_bytes()
    reloaded = MailAccountService(SecureTokenVault(str(directory)),
                                  imap_factory=no_network, smtp_factory=no_network)
    assert reloaded.credentials().password == secret
    assert reloaded.status()["username"] == "anis-qa-fixture"
    assert "password" not in reloaded.status()
    assert reloaded.disconnect()["reason"] == "disconnected"
    tombstone = reloaded.vault.load(reloaded.PROVIDER, reloaded.ACCOUNT)
    assert "password" not in tombstone and "username" not in tombstone
    from core.mail_runtime import MailReadError
    with pytest.raises(MailReadError):
        service.credentials()
