"""Offline IMAP read contracts with generated MIME and a fake server only."""
from __future__ import annotations

from email import policy
from email.message import EmailMessage
import json
import re
import ssl

import pytest

from core.mail_runtime import ImapReader, ImapSettings, MailReadError
from core.plugin import ToolCancelledError


def make_message(uid=101, *, body="안녕하세요.  본문 공백을 보존합니다.\n둘째 줄 🙂", html=False, attachment=False):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "보낸 사람 <sender@example.test>"
    message["To"] = "받는 사람 <receiver@example.test>"
    message["Subject"] = f"검토 요청 {uid} — 일정 확인"
    message["Date"] = "Mon, 14 Sep 2026 09:30:00 +0900"
    message["Message-ID"] = f"<fixture-{uid}@example.test>"
    message.set_content(body)
    if html:
        message.add_alternative("<script>HTML-ONLY-SECRET</script><p>HTML body</p>", subtype="html")
    if attachment:
        message.add_attachment(b"ATTACHMENT-ONLY-SECRET", maintype="application", subtype="octet-stream", filename="private.bin")
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, messages=None):
        self.messages = {101: make_message()} if messages is None else dict(messages)
        self.calls = []
        self.uidvalidity_reply = ("UIDVALIDITY", [b"77"])
        self.search_reply = None
        self.fetch_reply = None
        self.login_reply = ("OK", [b"fixture login"])
        self.select_reply = ("OK", [str(len(self.messages)).encode()])
        self.after_call = lambda _stage: None
        self.logout_error = None

    def login(self, username, password):
        self.calls.append(("login", username, password))
        self.after_call("login")
        return self.login_reply

    def select(self, mailbox, *, readonly):
        self.calls.append(("select", mailbox, readonly))
        self.after_call("select")
        return self.select_reply

    def response(self, label):
        self.calls.append(("response", label))
        self.after_call("response")
        return self.uidvalidity_reply

    def uid(self, command, *args):
        self.calls.append((command, *args))
        self.after_call(command)
        if command == "SEARCH":
            if self.search_reply is not None:
                return self.search_reply
            ids = sorted(self.messages)
            if "UID" in args:
                ceiling = int(args[-1].split(":")[1])
                ids = [value for value in ids if value <= ceiling]
            return "OK", [b" ".join(str(value).encode() for value in ids)]
        assert command == "FETCH", "No state-changing IMAP command is allowed"
        uid, fields = int(args[0]), args[1]
        if self.fetch_reply is not None:
            return self.fetch_reply(uid, fields) if callable(self.fetch_reply) else self.fetch_reply
        message = self.messages[uid]
        match = re.search(r"BODY\.PEEK\[(.*)\]<0\.([0-9]+)>", fields)
        assert match, "Every fetch must use a bounded BODY.PEEK range"
        section, size = match.group(1), int(match.group(2))
        raw = message if not section else message.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        raw = raw[:size]
        metadata = (f"1 (UID {uid} RFC822.SIZE {len(message)} BODY[{section}]<0> {{{len(raw)}}}").encode()
        return "OK", [(metadata, raw), b")"]

    def logout(self):
        self.calls.append(("logout",))
        if self.logout_error:
            raise self.logout_error
        return "BYE", [b"fixture logout"]


def make_reader(server=None, **kwargs):
    server = server if server is not None else FakeIMAP()
    connections = []

    def factory(host, port, **options):
        connections.append((host, port, options))
        return server

    reader = ImapReader(ImapSettings("imap.example.test", "fixture-user", "fixture-password"),
                        connection_factory=factory, **kwargs)
    return reader, server, connections


def commands(server, name):
    return [call for call in server.calls if call[0] == name]


def test_list_uses_verified_tls_readonly_inbox_and_peek_headers_only():
    reader, server, connections = make_reader(FakeIMAP({value: make_message(value) for value in (5, 101, 9)}))
    result = reader.list_inbox(limit=2, unread_only=True)
    host, port, options = connections[0]
    assert host == "imap.example.test" and port == 993 and options["timeout"] == 10
    assert options["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert options["ssl_context"].check_hostname
    assert commands(server, "select") == [("select", "INBOX", True)]
    assert commands(server, "SEARCH") == [("SEARCH", None, "UNSEEN")]
    assert [item["uid"] for item in result["items"]] == [101, 9]
    assert result["more"] and result["next_before_uid"] == 9
    assert result["uid_validity"] == 77 and result["read_only"]
    assert not result["snapshot"] and not result["body_included"]
    assert result["content_trust"] == "untrusted_external"
    for call in commands(server, "FETCH"):
        assert "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]<0.32769>" in call[2]
    headers = result["items"][0]["headers"]
    assert headers["Subject"] == "검토 요청 101 — 일정 확인"
    assert headers["From"] == "보낸 사람 <sender@example.test>"
    assert headers["To"] == "받는 사람 <receiver@example.test>"
    assert "본문 공백" not in json.dumps(result, ensure_ascii=False)
    assert server.calls[-1] == ("logout",)
    assert not {"STORE", "CLOSE", "EXPUNGE"} & {call[0] for call in server.calls}


def test_pagination_keeps_uidvalidity_and_strictly_excludes_boundary():
    reader, server, _connections = make_reader(FakeIMAP({value: make_message(value) for value in (3, 9, 101)}))
    result = reader.list_inbox(before_uid=101, uid_validity=77, limit=1)
    assert commands(server, "SEARCH") == [("SEARCH", None, "ALL", "UID", "1:100")]
    assert [item["uid"] for item in result["items"]] == [9]
    assert result["more"] and result["next_before_uid"] == 9


@pytest.mark.parametrize("messages,before_uid", [({}, None), ({101: make_message()}, 1)])
def test_empty_inbox_and_first_uid_boundary_are_empty_without_fetch(messages, before_uid):
    reader, server, _connections = make_reader(FakeIMAP(messages))
    result = reader.list_inbox(before_uid=before_uid, uid_validity=77 if before_uid else None)
    assert result["items"] == [] and not result["more"] and result["next_before_uid"] is None
    assert not commands(server, "FETCH")
    if before_uid == 1:
        assert not commands(server, "SEARCH")
    assert server.calls[-1] == ("logout",)


@pytest.mark.parametrize("options", [
    {"limit": True}, {"limit": 0}, {"limit": 51}, {"limit": "20"},
    {"unread_only": 1}, {"unread_only": "UNSEEN OR ALL"},
    {"before_uid": 9}, {"before_uid": False, "uid_validity": 77},
    {"before_uid": "9 STORE 1 +FLAGS (\\Seen)", "uid_validity": 77},
    {"before_uid": 0, "uid_validity": 77}, {"before_uid": 4294967296, "uid_validity": 77},
    {"uid_validity": True}, {"uid_validity": "77"},
])
def test_invalid_list_bounds_types_and_injection_make_no_connection(options):
    reader, server, connections = make_reader()
    with pytest.raises(MailReadError):
        reader.list_inbox(**options)
    assert not connections and not server.calls


@pytest.mark.parametrize("uid,validity", [(True, 77), (0, 77), (4294967296, 77),
                                       ("1:*", 77), (101, False), (101, "77"), (101, -1)])
def test_invalid_read_identifiers_make_no_connection(uid, validity):
    reader, server, connections = make_reader()
    with pytest.raises(MailReadError):
        reader.read_message(uid=uid, uid_validity=validity)
    assert not connections and not server.calls


@pytest.mark.parametrize("action", ["read", "list"])
def test_changed_uidvalidity_aborts_before_search_or_fetch(action):
    reader, server, _connections = make_reader()
    with pytest.raises(MailReadError, match="메일함이 변경"):
        if action == "read":
            reader.read_message(uid=101, uid_validity=76)
        else:
            reader.list_inbox(before_uid=102, uid_validity=76)
    assert not commands(server, "FETCH") and not commands(server, "SEARCH")
    assert server.calls[-1] == ("logout",)


@pytest.mark.parametrize("reply", [
    ("UIDVALIDITY", []), ("UIDVALIDITY", [None]), ("UIDVALIDITY", ["77"]),
    ("UIDVALIDITY", [b"0"]), ("UIDVALIDITY", [b"4294967296"]),
    ("UIDVALIDITY", [b"77\r\nBAD"]), ("UIDVALIDITY", [b"77", b"78"]),
    ("NOT-UIDVALIDITY", [b"77"]), None,
])
def test_malformed_mailbox_identity_reply_fails_closed_and_logs_out(reply):
    reader, server, _connections = make_reader()
    server.uidvalidity_reply = reply
    with pytest.raises(MailReadError):
        reader.list_inbox()
    assert not commands(server, "FETCH") and server.calls[-1] == ("logout",)


@pytest.mark.parametrize("reply", [
    ("NO", [b"server-private-text"]), ("OK", [None]), ("OK", ["101"]),
    ("OK", [b"101 BAD"]), ("OK", [b"0"]), ("OK", [b"4294967296"]),
    ("OK", [b"101 " * 270000]),
], ids=["status", "none", "string", "bad-uid", "zero", "overflow", "oversized"])
def test_malformed_or_oversized_search_is_redacted_and_never_fetches(reply):
    reader, server, _connections = make_reader()
    server.search_reply = reply
    with pytest.raises(MailReadError) as error:
        reader.list_inbox()
    assert "server-private-text" not in str(error.value)
    assert not commands(server, "FETCH") and server.calls[-1] == ("logout",)


def test_search_duplicate_uids_are_deduplicated_but_out_of_range_is_rejected():
    reader, server, _connections = make_reader()
    server.search_reply = ("OK", [b"101 101"])
    assert len(reader.list_inbox()["items"]) == 1
    with pytest.raises(MailReadError, match="범위"):
        reader.list_inbox(before_uid=101, uid_validity=77)


@pytest.mark.parametrize("reply", [
    ("OK", []), ("OK", [(b"1 (UID 102 RFC822.SIZE 3)", b"abc")]),
    ("OK", [(b"1 (UID 101)", b"abc")]),
    ("OK", [(b"1 (UID 101 RFC822.SIZE 3)", "abc")]),
    ("OK", [(b"1 (UID 101 RFC822.SIZE 3)", b"abc"), (b"2 (UID 101 RFC822.SIZE 3)", b"abc")]),
    ("NO", [b"private-server-reply"]),
    ("OK", [(b"1 (UID 101 UID 102 RFC822.SIZE 3)", b"abc")]),
], ids=["missing", "wrong-uid", "missing-size", "not-bytes", "duplicates", "status", "conflicting-uids"])
def test_fetch_requires_one_exact_uid_literal_and_redacts_server_reply(reply):
    reader, server, _connections = make_reader()
    server.fetch_reply = reply
    with pytest.raises(MailReadError) as error:
        reader.read_message(uid=101, uid_validity=77)
    assert "private-server-reply" not in str(error.value)
    assert server.calls[-1] == ("logout",)


def test_read_decodes_plain_mime_and_excludes_html_binary_and_attached_messages():
    outer = EmailMessage(policy=policy.SMTP)
    outer["Subject"] = "여러 MIME 부분"
    outer.set_content("보이는 본문  🙂\n둘째 줄")
    outer.add_alternative("<script>HTML-ONLY-SECRET</script>", subtype="html")
    outer.add_attachment(b"BINARY-ONLY-SECRET", maintype="application", subtype="octet-stream", filename="x.bin")
    outer.add_attachment("ATTACHED-TEXT-ONLY-SECRET", subtype="plain", filename="notes.txt")
    forwarded = EmailMessage(policy=policy.SMTP)
    forwarded.set_content("FORWARDED-ONLY-SECRET")
    outer.add_attachment(forwarded, filename="forwarded.eml")
    reader, server, _connections = make_reader(FakeIMAP({101: outer.as_bytes()}))
    result = reader.read_message(uid=101, uid_validity=77)
    assert result["body"].replace("\r\n", "\n") == "보이는 본문  🙂\n둘째 줄\n"
    assert result["body_available"] and result["body_format"] == "text/plain"
    assert result["read_only"] and not result["attachments_included"]
    assert "ONLY-SECRET" not in json.dumps(result, ensure_ascii=False)
    assert commands(server, "FETCH")[0][2] == "(UID RFC822.SIZE BODY.PEEK[]<0.262145>)"
    assert server.calls[-1] == ("logout",)


@pytest.mark.parametrize("kind", ["html", "plain-attachment", "attached-message"])
def test_no_plain_inline_body_is_not_replaced_by_html_or_attachment(kind):
    message = EmailMessage(policy=policy.SMTP)
    if kind == "html":
        message.set_content("<b>HIDDEN-SECRET</b>", subtype="html")
    elif kind == "plain-attachment":
        message.set_content("HIDDEN-SECRET")
        message["Content-Disposition"] = 'attachment; filename="private.txt"'
    else:
        attachment = EmailMessage(policy=policy.SMTP)
        attachment.set_content("HIDDEN-SECRET")
        message.add_attachment(attachment, filename="private.eml")
    reader, _server, _connections = make_reader(FakeIMAP({101: message.as_bytes()}))
    result = reader.read_message(uid=101, uid_validity=77)
    assert result["body"] == "" and not result["body_available"]
    assert "HIDDEN-SECRET" not in json.dumps(result)


@pytest.mark.parametrize("charset", ["utf-8", "iso-2022-kr"])
def test_generated_encoded_korean_mime_headers_and_body_are_decoded(charset):
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = "한국어 제목"
    message["From"] = "작성자 <writer@example.test>"
    message.set_content("한국어 본문", charset=charset, cte="base64")
    reader, _server, _connections = make_reader(FakeIMAP({101: message.as_bytes()}))
    result = reader.read_message(uid=101, uid_validity=77)
    assert result["headers"]["Subject"] == "한국어 제목"
    assert result["headers"]["From"] == "작성자 <writer@example.test>"
    assert result["body"].strip() == "한국어 본문"


@pytest.mark.parametrize("body_size", [18000, 400000])
def test_body_text_and_raw_fetch_caps_report_truncation(body_size):
    reader, server, _connections = make_reader(FakeIMAP({101: make_message(body="x" * body_size)}))
    result = reader.read_message(uid=101, uid_validity=77)
    assert result["truncated"] and len(result["body"]) <= 16000
    assert result["body"].startswith("x" * 100)
    assert "<0.262145>" in commands(server, "FETCH")[0][2]


def test_oversized_headers_are_bounded_and_marked_truncated():
    message = make_message().replace(b"Subject:", b"X-Long: " + b"x" * 40000 + b"\r\nSubject:", 1)
    reader, _server, _connections = make_reader(FakeIMAP({101: message}))
    result = reader.list_inbox()
    assert result["items"][0]["headers_truncated"]
    assert all(len(value) <= 2000 for value in result["items"][0]["headers"].values())


def test_declared_size_larger_than_returned_body_is_always_truncated():
    reader, server, _connections = make_reader()
    raw = make_message(body="short fixture body")
    server.fetch_reply = ("OK", [(
        f"1 (UID 101 RFC822.SIZE {len(raw) + 5000} BODY[]<0> {{{len(raw)}}}".encode(), raw,
    ), b")"])
    result = reader.read_message(uid=101, uid_validity=77)
    assert result["truncated"] and result["body"].strip() == "short fixture body"


@pytest.mark.parametrize("stage", ["SEARCH", "FETCH"])
def test_network_exception_after_login_still_logs_out(stage):
    reader, server, _connections = make_reader()

    def disconnect(value):
        if value == stage:
            raise OSError("fixture network failure")

    server.after_call = disconnect
    with pytest.raises(OSError):
        reader.list_inbox()
    assert server.calls[-1] == ("logout",)


@pytest.mark.parametrize("stage", ["before-connect", "login", "select", "response", "SEARCH", "FETCH"])
def test_cancellation_stops_subsequent_commands_and_logs_out_if_connected(stage):
    cancelled = {"value": stage == "before-connect"}

    def checkpoint():
        if cancelled["value"]:
            raise ToolCancelledError("fixture cancellation")

    reader, server, connections = make_reader(checkpoint=checkpoint)
    server.after_call = lambda value: cancelled.update(value=True) if value == stage else None
    with pytest.raises(ToolCancelledError):
        reader.list_inbox()
    if stage == "before-connect":
        assert not connections and not server.calls
    else:
        assert server.calls[-1] == ("logout",)
        assert len(commands(server, "FETCH")) <= (1 if stage == "FETCH" else 0)


def test_deadline_expiry_stops_fetch_and_logs_out():
    clock = {"now": 0.0}
    reader, server, _connections = make_reader(clock=lambda: clock["now"])
    server.after_call = lambda stage: clock.update(now=61.0) if stage == "SEARCH" else None
    with pytest.raises(MailReadError, match="시간 한도"):
        reader.list_inbox()
    assert not commands(server, "FETCH") and server.calls[-1] == ("logout",)


@pytest.mark.parametrize("stage", ["login", "select"])
def test_failed_setup_always_logs_out_without_exposing_server_text(stage):
    reader, server, _connections = make_reader()
    setattr(server, stage + "_reply", ("NO", [b"private-server-text"]))
    server.logout_error = OSError("logout is already disconnected")
    with pytest.raises(MailReadError) as error:
        reader.list_inbox()
    assert "private-server-text" not in str(error.value)
    assert server.calls[-1] == ("logout",)


@pytest.fixture
def empty_mail_environment(monkeypatch):
    for variable in ("MAIL_PROVIDER", "MAIL_IMAP_HOST", "MAIL_IMAP_USERNAME", "MAIL_IMAP_PASSWORD", "MAIL_IMAP_PORT"):
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


def test_naver_settings_require_explicit_credentials_and_hide_password(empty_mail_environment):
    env = empty_mail_environment
    env.setenv("MAIL_PROVIDER", "naver")
    with pytest.raises(MailReadError):
        ImapSettings.from_environment()
    env.setenv("MAIL_IMAP_USERNAME", "fixture-user")
    env.setenv("MAIL_IMAP_PASSWORD", "fixture-password-secret")
    settings = ImapSettings.from_environment()
    assert settings.host == "imap.naver.com" and settings.port == 993
    assert "fixture-password-secret" not in repr(settings)
    env.setenv("MAIL_IMAP_HOST", "imap.example.test")
    assert ImapSettings.from_environment().host == "imap.example.test"


@pytest.mark.parametrize("port", ["zero", "0", "65536", "-1"])
def test_bad_environment_port_is_rejected_without_leaking_password(empty_mail_environment, port):
    env = empty_mail_environment
    for key, value in {"MAIL_IMAP_HOST": "imap.example.test", "MAIL_IMAP_USERNAME": "fixture-user",
                       "MAIL_IMAP_PASSWORD": "private-setting", "MAIL_IMAP_PORT": port}.items():
        env.setenv(key, value)
    with pytest.raises(MailReadError) as error:
        ImapSettings.from_environment()
    assert "private-setting" not in str(error.value)


@pytest.mark.parametrize("key,value", [
    ("MAIL_IMAP_HOST", "imap.example.test\r\nINJECT"),
    ("MAIL_IMAP_HOST", " imap.example.test "),
    ("MAIL_IMAP_USERNAME", "owner\r\nINJECT"),
])
def test_control_or_ambiguous_environment_fields_are_rejected(empty_mail_environment, key, value):
    env = empty_mail_environment
    for name, setting in {"MAIL_IMAP_HOST": "imap.example.test", "MAIL_IMAP_USERNAME": "fixture-user",
                          "MAIL_IMAP_PASSWORD": "fixture-password"}.items():
        env.setenv(name, setting)
    env.setenv(key, value)
    with pytest.raises(MailReadError):
        ImapSettings.from_environment()
