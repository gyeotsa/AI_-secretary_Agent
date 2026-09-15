"""Offline account-configuration contracts: fake vault and auth-only servers."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import ssl
import threading

import pytest

import core.mail_accounts as accounts
from core.mail_accounts import MailAccountService, NaverMailCredentials
from core.mail_runtime import MailReadError
from core.plugin import ToolCancelledError


SECRET = "fixture-app-password-never-output"
STAMP = "2026-09-15T08:30:00+00:00"
SCOPE = (MailAccountService.PROVIDER, MailAccountService.ACCOUNT)


@pytest.fixture(autouse=True)
def forbid_real_vault_and_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Offline account tests must not open a real vault or network")

    monkeypatch.setattr(accounts, "SecureTokenVault", forbidden)
    monkeypatch.setattr(accounts.imaplib, "IMAP4_SSL", forbidden)
    monkeypatch.setattr(accounts.smtplib, "SMTP", forbidden)
    monkeypatch.setattr(accounts.smtplib, "SMTP_SSL", forbidden)


class FakeVault:
    def __init__(self):
        self.records = {}
        self.calls = []
        self.fail_load = False
        self.fail_save = False

    def load(self, provider, account):
        self.calls.append(("load", provider, account))
        if self.fail_load:
            raise OSError(SECRET)
        return deepcopy(self.records.get((provider, account)))

    def save(self, provider, account, record):
        self.calls.append(("save", provider, account))
        if self.fail_save:
            raise OSError(SECRET)
        self.records[provider, account] = deepcopy(record)


class AuthServers:
    """No mailbox, message, recipient or send APIs exist on these fakes."""
    def __init__(self):
        self.events = []
        self.connections = []
        self.contexts = []
        self.after = lambda _stage: None
        self.fail_at = set()
        self.imap_reply = ("OK", [b"private-server-response"])
        self.smtp_reply = (235, b"private-server-response")
        self.ehlo_count = 0

    def event(self, name):
        self.events.append(name)
        self.after(name)
        if name in self.fail_at:
            raise OSError(SECRET + " private-server-response")

    def imap_factory(self, host, port, *, ssl_context, timeout):
        self.connections.append(("imap", host, port, timeout))
        self.contexts.append(ssl_context)
        self.event("imap_connect")
        owner = self

        class IMAP:
            def login(self, username, password):
                assert (username, password) == ("fixture-user", SECRET)
                owner.event("imap_login")
                return owner.imap_reply

            def logout(self):
                owner.event("imap_logout")

            def shutdown(self):
                owner.event("imap_shutdown")

        return IMAP()

    def smtp_factory(self, host, port, *, timeout):
        self.connections.append(("smtp", host, port, timeout))
        self.event("smtp_connect")
        owner = self

        class SMTP:
            def ehlo(self):
                owner.ehlo_count += 1
                owner.event("smtp_ehlo_" + str(owner.ehlo_count))
                return 250, b"fixture EHLO"

            def starttls(self, *, context):
                owner.contexts.append(context)
                owner.event("smtp_starttls")
                return 220, b"fixture STARTTLS"

            def login(self, username, password):
                assert (username, password) == ("fixture-user", SECRET)
                owner.event("smtp_login")
                return owner.smtp_reply

            def quit(self):
                owner.event("smtp_quit")

            def close(self):
                owner.event("smtp_close")

        return SMTP()


def make_service(*, saved=True, vault=None, **kwargs):
    vault = vault if vault is not None else FakeVault()
    servers = AuthServers()
    service = MailAccountService(vault, imap_factory=servers.imap_factory,
                                 smtp_factory=servers.smtp_factory,
                                 now=lambda: STAMP, **kwargs)
    if saved:
        service.save("fixture-user", SECRET)
    return service, vault, servers


def assert_public_redacted(value):
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    assert SECRET not in serialized
    assert "private-server-response" not in serialized
    assert '"password"' not in serialized and '"revision"' not in serialized


def test_save_status_credentials_and_replacement_have_separate_safe_surfaces():
    service, vault, servers = make_service(saved=False)
    assert service.status() == {"configured": False, "username": "", "verified_at": None,
                                "reason": "not_configured"}
    assert service.credentials() is None
    status = service.save("  fixture-user@NAVER.COM  ", SECRET)
    assert status == {"configured": True, "username": "fixture-user", "verified_at": None,
                      "reason": "saved"}
    assert_public_redacted(status)
    assert service.status() == status
    credentials = service.credentials()
    assert isinstance(credentials, NaverMailCredentials)
    assert credentials.username == "fixture-user" and credentials.password == SECRET
    assert credentials.sender == "fixture-user@naver.com"
    settings = credentials.imap_settings()
    assert (settings.host, settings.port, settings.username, settings.password) == (
        "imap.naver.com", 993, "fixture-user", SECRET)
    assert SECRET not in repr(credentials) + repr(settings)
    status["username"] = "tampered"
    assert service.status()["username"] == "fixture-user"
    old_revision = credentials.revision
    service.save("fixture-user", SECRET)
    assert service.credentials().revision != old_revision
    assert not servers.events
    assert {call[1:] for call in vault.calls} == {SCOPE}


def test_default_vault_is_lazy_and_only_constructed_once_with_fake_injection(monkeypatch):
    created = []
    vault = FakeVault()
    monkeypatch.setattr(accounts, "SecureTokenVault", lambda: created.append("created") or vault)
    service = MailAccountService()
    assert not created
    service.status()
    service.save("fixture-user", SECRET)
    service.status()
    assert created == ["created"]
    assert {call[1:] for call in vault.calls} == {SCOPE}


@pytest.mark.parametrize("username", [None, True, 123, "", " ", "@naver.com", "fixture@gmail.com",
                                      "fixture@naver.com.evil", "a@naver.com@naver.com", "a b",
                                      "a\r\nLOGIN", "한글", "_fixture", "a" * 65])
def test_invalid_username_never_changes_saved_credentials_or_connects(username):
    service, vault, servers = make_service()
    before = deepcopy(vault.records)
    with pytest.raises(MailReadError) as error:
        service.save(username, SECRET)
    assert_public_redacted(str(error.value))
    assert vault.records == before and not servers.events


@pytest.mark.parametrize("password", [None, True, 123, "", "x" * 1025, "x\n", "x\r", "x\t", "x\x00", "x\x7f"])
def test_invalid_password_never_changes_saved_credentials_or_connects(password):
    service, vault, servers = make_service()
    before = deepcopy(vault.records)
    with pytest.raises(MailReadError):
        service.save("fixture-user", password)
    assert vault.records == before and not servers.events


@pytest.mark.parametrize("password", ["x", "x" * 1024, " spaces-preserved "])
def test_password_boundaries_preserve_exact_value(password):
    service, _vault, _servers = make_service(saved=False)
    service.save("fixture-user", password)
    assert service.credentials().password == password


@pytest.mark.parametrize("mutation", [
    {"version": True}, {"version": 2}, {"enabled": 1}, {"revision": "bad"},
    {"revision": "A" * 32}, {"username": " fixture-user"}, {"username": "fixture-user@naver.com"},
    {"password": ""}, {"password": SECRET + "\n"}, {"verified_at": "not-a-time"},
    {"verified_at": "2026-09-15T08:30:00"}, {"verified_at": 123},
    {"extra": SECRET}, {"enabled": False},
])
def test_malformed_encrypted_payload_fails_closed_and_is_redacted(mutation):
    service, vault, servers = make_service()
    vault.records[SCOPE].update(mutation)
    before = deepcopy(vault.records)
    for operation in (service.status, service.credentials, service.verify):
        with pytest.raises(MailReadError) as error:
            operation()
        assert_public_redacted(str(error.value))
        assert error.value.__cause__ is None and error.value.__suppress_context__
    assert vault.records == before and not servers.events


@pytest.mark.parametrize("record", [[], "secret-record", 0, {}, {"enabled": False}])
def test_non_schema_record_is_not_treated_as_missing(record):
    service, vault, servers = make_service(saved=False)
    vault.records[SCOPE] = record
    with pytest.raises(MailReadError):
        service.credentials()
    assert not servers.events


@pytest.mark.parametrize("field", ["version", "enabled", "revision", "username", "password", "verified_at"])
def test_missing_payload_fields_fail_closed(field):
    service, vault, _servers = make_service()
    del vault.records[SCOPE][field]
    with pytest.raises(MailReadError):
        service.status()


def test_disconnect_is_credential_free_tombstone_and_never_allows_environment_fallback(monkeypatch):
    service, vault, servers = make_service()
    unrelated = ("google", "another-account")
    vault.records[unrelated] = {"fixture": "leave-unchanged"}
    previous = service.credentials()
    monkeypatch.setenv("MAIL_PROVIDER", "naver")
    monkeypatch.setenv("MAIL_IMAP_USERNAME", "fixture-env-user")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD", "fixture-env-password")
    status = service.disconnect()
    assert status == {"configured": False, "username": "", "verified_at": None,
                      "reason": "disconnected"}
    assert_public_redacted(status)
    assert set(vault.records[SCOPE]) == {"version", "enabled", "revision"}
    assert vault.records[SCOPE]["enabled"] is False
    assert vault.records[SCOPE]["revision"] != previous.revision
    assert vault.records[unrelated] == {"fixture": "leave-unchanged"}
    for current in (service, MailAccountService(vault)):
        assert current.status() == status
        with pytest.raises(MailReadError, match="해제"):
            current.credentials()
        with pytest.raises(MailReadError, match="해제"):
            current.verify()
        with pytest.raises(ToolCancelledError):
            current.check_revision(previous)
    assert not servers.events
    service.save("fixture-user", SECRET)
    assert service.credentials().username == "fixture-user"


def test_verify_requires_saved_account_and_does_not_use_environment(monkeypatch):
    service, _vault, servers = make_service(saved=False)
    monkeypatch.setenv("MAIL_IMAP_USERNAME", "fixture-env-user")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD", "fixture-env-password")
    with pytest.raises(MailReadError, match="먼저 저장"):
        service.verify()
    assert not servers.events


@pytest.mark.parametrize("operation", ["status", "credentials", "verify", "save", "disconnect"])
def test_vault_failures_expose_only_safe_errors_and_do_not_modify_prior_record(operation):
    service, vault, servers = make_service()
    before = deepcopy(vault.records)
    vault.fail_load = operation in {"status", "credentials", "verify"}
    vault.fail_save = operation in {"save", "disconnect"}
    with pytest.raises(MailReadError) as error:
        if operation == "save":
            service.save("replacement", "fixture-other-password")
        else:
            getattr(service, operation)()
    assert_public_redacted(str(error.value))
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert vault.records == before and not servers.events


SUCCESS_EVENTS = ["imap_connect", "imap_login", "imap_logout", "smtp_connect",
                  "smtp_ehlo_1", "smtp_starttls", "smtp_ehlo_2", "smtp_login",
                  "smtp_quit", "smtp_close"]


def test_verification_uses_certificate_checked_tls_auth_only_and_persists_timestamp(capsys):
    service, vault, servers = make_service()
    revision = service.credentials().revision
    result = service.verify()
    assert result == {"imap_authenticated": True, "smtp_authenticated": True,
                      "verified_at": STAMP, "reason": "authenticated_at_time"}
    assert servers.events == SUCCESS_EVENTS
    assert servers.connections == [("imap", "imap.naver.com", 993, 10),
                                   ("smtp", "smtp.naver.com", 587, 10)]
    assert len(servers.contexts) == 2
    for context in servers.contexts:
        assert isinstance(context, ssl.SSLContext)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert vault.records[SCOPE]["revision"] == revision
    assert service.status()["verified_at"] == STAMP
    assert_public_redacted(result)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


@pytest.mark.parametrize("imap_ok,smtp_ok", [(False, False), (False, True), (True, False)])
def test_partial_auth_is_not_verified_and_clears_prior_verified_timestamp(imap_ok, smtp_ok):
    service, vault, servers = make_service()
    vault.records[SCOPE]["verified_at"] = STAMP
    servers.imap_reply = ("OK" if imap_ok else "NO", [b"private-server-response"])
    servers.smtp_reply = (235 if smtp_ok else 535, b"private-server-response")
    result = service.verify()
    assert result == {"imap_authenticated": imap_ok, "smtp_authenticated": smtp_ok,
                      "verified_at": None, "reason": "authentication_unverified"}
    assert vault.records[SCOPE]["verified_at"] is None
    assert servers.events == SUCCESS_EVENTS
    assert_public_redacted(result)


@pytest.mark.parametrize("protocol,reply", [
    ("imap", None), ("imap", []), ("imap", ()), ("imap", "OK"), ("imap", (b"OK", [])),
    ("smtp", None), ("smtp", []), ("smtp", ()), ("smtp", "235"), ("smtp", ("235", b"text")),
])
def test_malformed_auth_replies_never_claim_success(protocol, reply):
    service, _vault, servers = make_service()
    setattr(servers, protocol + "_reply", reply)
    result = service.verify()
    assert not result[protocol + "_authenticated"]
    assert result["verified_at"] is None
    assert_public_redacted(result)


@pytest.mark.parametrize("stage", ["imap_connect", "imap_login", "smtp_connect", "smtp_ehlo_1",
                                  "smtp_starttls", "smtp_ehlo_2", "smtp_login"])
def test_connection_and_auth_failures_are_redacted_and_stop_unsafe_protocol_steps(stage):
    service, vault, servers = make_service()
    servers.fail_at.add(stage)
    result = service.verify()
    protocol = stage.split("_")[0]
    assert result[protocol + "_authenticated"] is False
    assert result["verified_at"] is None and vault.records[SCOPE]["verified_at"] is None
    assert_public_redacted(result)
    if stage == "imap_login":
        assert "imap_logout" in servers.events
    if stage.startswith("smtp_") and stage != "smtp_connect":
        assert servers.events[-2:] == ["smtp_quit", "smtp_close"]
        if stage != "smtp_login":
            assert "smtp_login" not in servers.events


@pytest.mark.parametrize("stage", ["imap_logout", "smtp_quit", "smtp_close"])
def test_cleanup_failure_does_not_leak_private_error_or_discard_success(stage):
    service, _vault, servers = make_service()
    servers.fail_at.add(stage)
    result = service.verify()
    assert result["imap_authenticated"] and result["smtp_authenticated"]
    assert result["verified_at"] == STAMP
    assert_public_redacted(result)
    expected = list(SUCCESS_EVENTS)
    if stage == "imap_logout":
        expected.insert(3, "imap_shutdown")
    assert servers.events == expected


def test_imap_shutdown_failure_is_best_effort_and_never_leaks_exception():
    service, _vault, servers = make_service()
    servers.fail_at.update({"imap_logout", "imap_shutdown"})
    result = service.verify()
    assert result["verified_at"] == STAMP
    assert servers.events[:4] == ["imap_connect", "imap_login", "imap_logout", "imap_shutdown"]
    assert_public_redacted(result)


def test_imap_cleanup_failures_preserve_cancellation_and_never_start_smtp():
    service, vault, servers = make_service()
    servers.fail_at.update({"imap_logout", "imap_shutdown"})

    def checkpoint():
        if "imap_login" in servers.events:
            raise ToolCancelledError("fixture cancellation must survive")

    with pytest.raises(ToolCancelledError, match="must survive"):
        service.verify(checkpoint=checkpoint)
    assert servers.events == ["imap_connect", "imap_login", "imap_logout", "imap_shutdown"]
    assert vault.records[SCOPE]["verified_at"] is None


@pytest.mark.parametrize("checkpoint_number", range(1, 13))
def test_cancellation_at_every_checkpoint_never_persists_a_verified_result(checkpoint_number):
    service, vault, servers = make_service()
    before = deepcopy(vault.records)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == checkpoint_number:
            raise ToolCancelledError("fixture user cancellation")

    with pytest.raises(ToolCancelledError, match="fixture user cancellation"):
        service.verify(checkpoint=checkpoint)
    assert calls == checkpoint_number
    assert vault.records == before
    if checkpoint_number <= 2:
        assert not servers.events
    if checkpoint_number >= 3:
        assert "imap_logout" in servers.events
    if checkpoint_number >= 6:
        assert servers.events[-2:] == ["smtp_quit", "smtp_close"]
    if checkpoint_number < 10:
        assert "smtp_login" not in servers.events


def test_cleanup_errors_do_not_mask_cancellation():
    service, vault, servers = make_service()
    servers.fail_at.update({"smtp_quit", "smtp_close"})
    cancelled = False

    def after(stage):
        nonlocal cancelled
        if stage == "smtp_login":
            cancelled = True

    def checkpoint():
        if cancelled:
            raise ToolCancelledError("fixture cancellation must survive")

    servers.after = after
    with pytest.raises(ToolCancelledError, match="must survive"):
        service.verify(checkpoint=checkpoint)
    assert vault.records[SCOPE]["verified_at"] is None
    assert servers.events[-2:] == ["smtp_quit", "smtp_close"]


@pytest.mark.parametrize("stage", SUCCESS_EVENTS)
@pytest.mark.parametrize("change", ["save", "disconnect"])
def test_other_service_account_changes_fence_stale_verification_at_every_io_boundary(stage, change):
    service, vault, servers = make_service()
    other = MailAccountService(vault)
    old_credentials = service.credentials()
    replacement = None

    def after(current_stage):
        nonlocal replacement
        if current_stage == stage:
            if change == "save":
                other.save("replacement-user", "fixture-replacement-password")
            else:
                other.disconnect()
            replacement = deepcopy(vault.records)

    servers.after = after
    with pytest.raises(ToolCancelledError):
        service.verify()
    assert replacement is not None and vault.records == replacement
    with pytest.raises(ToolCancelledError):
        service.check_revision(old_credentials)
    if change == "save":
        assert other.credentials().username == "replacement-user"
        assert other.status()["verified_at"] is None
    else:
        assert set(vault.records[SCOPE]) == {"version", "enabled", "revision"}
        with pytest.raises(MailReadError):
            other.credentials()


@pytest.mark.parametrize("stage", SUCCESS_EVENTS)
def test_deadline_expiry_at_every_io_boundary_cancels_and_cleans_up(stage):
    clock = {"now": 0.0}
    service, vault, servers = make_service(clock=lambda: clock["now"])
    servers.after = lambda current: clock.update(now=60.0) if current == stage else None
    with pytest.raises(ToolCancelledError, match="제한 시간"):
        service.verify()
    assert vault.records[SCOPE]["verified_at"] is None
    assert "imap_logout" in servers.events
    if "smtp_connect" in servers.events:
        assert servers.events[-2:] == ["smtp_quit", "smtp_close"]


def test_post_auth_vault_write_failure_is_redacted_and_never_returns_success():
    service, vault, servers = make_service()
    servers.after = lambda stage: setattr(vault, "fail_save", True) if stage == "smtp_close" else None
    with pytest.raises(MailReadError) as error:
        service.verify()
    assert_public_redacted(str(error.value))
    assert vault.records[SCOPE]["verified_at"] is None
    assert servers.events == SUCCESS_EVENTS


@pytest.mark.parametrize("change", ["save", "disconnect"])
def test_legacy_environment_revision_is_only_valid_until_first_local_configuration(change):
    service, _vault, _servers = make_service(saved=False)
    assert service.check_revision(None) is None
    if change == "save":
        service.save("fixture-user", SECRET)
    else:
        service.disconnect()
    with pytest.raises(ToolCancelledError):
        service.check_revision(None)


@pytest.mark.parametrize("change", ["save", "disconnect"])
def test_blocking_auth_does_not_block_other_service_changes_or_resurrect_stale_account(change):
    service, vault, servers = make_service()
    other = MailAccountService(vault)
    entered = threading.Event()
    release = threading.Event()

    def after(stage):
        if stage == "imap_login":
            entered.set()
            assert release.wait(3), "Fixture network call was not released"

    def change_account():
        if change == "save":
            return other.save("replacement-user", "fixture-replacement-password")
        return other.disconnect()

    servers.after = after
    with ThreadPoolExecutor(max_workers=2) as pool:
        verification = pool.submit(service.verify)
        try:
            assert entered.wait(2), "Verification did not reach the fake network call"
            mutation = pool.submit(change_account)
            status = mutation.result(timeout=1)
            assert status["reason"] == ("saved" if change == "save" else "disconnected")
            replacement = deepcopy(vault.records)
        finally:
            release.set()
        with pytest.raises(ToolCancelledError):
            verification.result(timeout=2)
    assert vault.records == replacement
    assert servers.events == ["imap_connect", "imap_login", "imap_logout"]
