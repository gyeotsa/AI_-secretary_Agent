import json
import ssl
import pytest

from core.plugin import ToolCancelledError
from plugins.mail import MailPlugin


@pytest.fixture(autouse=True)
def clean_mail_environment(monkeypatch, tmp_path):
    monkeypatch.setattr("config.Config.DB_PATH", str(tmp_path / "isolated.db"))
    for key in ("MAIL_PROVIDER", "MAIL_FROM", "MAIL_SMTP_HOST", "MAIL_SMTP_PORT",
                "MAIL_SMTP_USERNAME", "MAIL_SMTP_PASSWORD", "MAIL_SMTP_SECURITY",
                "MAIL_IMAP_HOST", "MAIL_IMAP_PORT", "MAIL_IMAP_USERNAME", "MAIL_IMAP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)


def setup_smtp(monkeypatch, *, naver=False):
    monkeypatch.setenv("MAIL_SMTP_USERNAME", "sender@example.test")
    monkeypatch.setenv("MAIL_SMTP_PASSWORD", "password-never-output")
    if naver:
        monkeypatch.setenv("MAIL_PROVIDER", "naver")
    else:
        monkeypatch.setenv("MAIL_SMTP_HOST", "smtp.example.test")


MESSAGE = {"to": "receiver@example.test", "subject": "테스트", "body": "본문"}


class FakeSMTP:
    def __init__(self, log, *, fail_at=None, refused=None):
        self.log, self.fail_at, self.refused = log, fail_at, refused

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.log.append("exit")
        if self.fail_at == "exit":
            raise OSError("password-never-output")

    def ehlo(self):
        self.log.append("ehlo")

    def starttls(self, context):
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        self.log.append("tls")
        if self.fail_at == "tls":
            raise OSError("password-never-output")

    def login(self, username, password):
        assert username == "sender@example.test" and password == "password-never-output"
        self.log.append("login")
        if self.fail_at == "login":
            raise OSError("password-never-output")

    def send_message(self, message):
        assert message["To"] == MESSAGE["to"]
        self.log.append("send")
        if self.fail_at == "send":
            raise OSError("password-never-output")
        return self.refused or {}


def test_naver_defaults_starttls_before_credentials(monkeypatch):
    setup_smtp(monkeypatch, naver=True)
    log = []
    def factory(host, port, timeout):
        assert (host, port, timeout) == ("smtp.naver.com", 587, 15)
        return FakeSMTP(log)
    monkeypatch.setattr("plugins.mail.smtplib.SMTP", factory)
    result = MailPlugin().execute_tool("mail_send_smtp", MESSAGE)
    assert result.succeeded
    assert log == ["ehlo", "tls", "ehlo", "login", "send", "exit"]
    assert result.evidence[0].data["delivered"] is None
    assert result.evidence[0].data["server_accepted"] is True
    assert "도착" in result.raw_output and "확인하지 않았" in result.raw_output
    assert "password-never-output" not in str(result.to_dict())


@pytest.mark.parametrize("fail_at,expected", [("tls", "failed"), ("login", "failed"),
                                               ("send", "unverified"), ("exit", "unverified")])
def test_smtp_failure_does_not_claim_delivery_or_echo_server_secret(monkeypatch, fail_at, expected):
    setup_smtp(monkeypatch, naver=True)
    log = []
    monkeypatch.setattr("plugins.mail.smtplib.SMTP", lambda *a, **k: FakeSMTP(log, fail_at=fail_at))
    result = MailPlugin().execute_tool("mail_send_smtp", MESSAGE)
    assert result.status.value == expected
    assert "password-never-output" not in str(result.to_dict())
    if fail_at == "tls":
        assert "login" not in log and "send" not in log
    if expected == "unverified":
        assert result.evidence[0].data["retry_allowed"] is False
    assert log.count("send") <= 1


def test_partial_recipient_acceptance_is_not_a_safe_retry(monkeypatch):
    setup_smtp(monkeypatch)
    log = []
    monkeypatch.setattr("plugins.mail.smtplib.SMTP_SSL", lambda *a, **k: FakeSMTP(log, refused={"private": (550, b"secret")}))
    result = MailPlugin().execute_tool("mail_send_smtp", MESSAGE)
    assert result.status.value == "unverified"
    assert result.evidence[0].data == {"refused_count": 1, "retry_allowed": False}
    assert "private" not in str(result.to_dict()) and "secret" not in str(result.to_dict())


@pytest.mark.parametrize("mode,port", [("plain", "25"), ("starttls", "0"), ("ssl", "65536"), ("ssl", "garbage")])
def test_invalid_transport_fails_before_network(monkeypatch, mode, port):
    setup_smtp(monkeypatch)
    monkeypatch.setenv("MAIL_SMTP_SECURITY", mode)
    monkeypatch.setenv("MAIL_SMTP_PORT", port)
    def forbidden(*a, **k):
        pytest.fail("network must not open")
    monkeypatch.setattr("plugins.mail.smtplib.SMTP", forbidden)
    monkeypatch.setattr("plugins.mail.smtplib.SMTP_SSL", forbidden)
    assert not MailPlugin().execute_tool("mail_send_smtp", MESSAGE).succeeded


def test_smtp_cancel_before_send_is_not_swallowed(monkeypatch):
    setup_smtp(monkeypatch)
    def cancel():
        raise ToolCancelledError("cancelled")
    monkeypatch.setattr("plugins.mail.check_turn_cancelled", cancel)
    with pytest.raises(ToolCancelledError):
        MailPlugin().execute_tool("mail_send_smtp", MESSAGE)


def test_read_tools_have_readonly_permission_and_bounded_contract():
    tools = {t.name: t for t in MailPlugin().get_tools()}
    for name in ("mail_list_inbox", "mail_read_message"):
        assert tools[name].side_effect == "read"
        assert tools[name].required_permissions == ["cloud_read"]
        assert tools[name].timeout_seconds == 75 and tools[name].max_retries == 0
        assert tools[name].cancellable
    assert tools["mail_send_smtp"].side_effect == "external_send"
    assert tools["mail_send_smtp"].required_permissions == ["mail_send"]


def test_mail_list_runtime_is_reachable_and_private_content_is_not_duplicated_in_evidence(monkeypatch):
    marker = "private mail subject"
    detail = {"uid_validity": 1, "items": [{"uid": 2, "headers": {"Subject": marker}}], "read_only": True}
    monkeypatch.setattr("plugins.mail.ImapSettings.from_environment", lambda: object())
    class Reader:
        def __init__(self, settings, checkpoint):
            checkpoint()
        def list_inbox(self, **kwargs):
            assert kwargs == {"limit": 5}
            return detail
    monkeypatch.setattr("plugins.mail.ImapReader", Reader)
    result = MailPlugin().execute_tool("mail_list_inbox", {"limit": 5})
    assert result.succeeded and json.loads(result.raw_output) == detail
    assert marker not in str(result.evidence)


def test_mail_read_runtime_error_is_redacted(monkeypatch):
    def fail():
        raise OSError("password-never-output")
    monkeypatch.setattr("plugins.mail.ImapSettings.from_environment", fail)
    result = MailPlugin().execute_tool("mail_read_message", {"uid": 1, "uid_validity": 1})
    assert not result.succeeded and "password-never-output" not in str(result.to_dict())
