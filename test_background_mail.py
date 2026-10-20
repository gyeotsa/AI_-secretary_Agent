"""Background mail trust boundaries, using generated mail and fake servers only."""
import base64
from copy import deepcopy
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from core.background_mail import BackgroundMailError, BackgroundMailService, GmailMailAccountService
from core.mail_accounts import MailAccountService
from core.mail_runtime import ImapReader, MailReadError
from core.plugin import ToolCancelledError
from test_mail_read_runtime import FakeIMAP, make_message, make_reader
from test_oauth_connection import Session as OAuthSession, begin


ACCOUNT = "owner@example.com"


class Vault:
    def __init__(self):
        self.tokens = {}

    def load(self, provider, account):
        return deepcopy(self.tokens.get((provider, account)))

    def save(self, provider, account, value):
        self.tokens[provider, account] = deepcopy(value)

    def delete(self, provider, account):
        return self.tokens.pop((provider, account), None) is not None


def gmail_account():
    vault, session = Vault(), OAuthSession()
    session.token["scope"] = " ".join(GmailMailAccountService.SPECS["google"]["scopes"])
    service = GmailMailAccountService(vault, session)
    service.complete(begin(service)["state"], "fixture-code")
    return service, vault, session


def message_raw(body="본문 🙂", *, html=False):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = "작성자 <sender@example.test>"
    message["Subject"] = "검토 요청"
    message["Date"] = "Mon, 14 Sep 2026 09:30:00 +0900"
    message.set_content(body, subtype="html" if html else "plain")
    message.add_attachment(b"PRIVATE-ATTACHMENT", maintype="application", subtype="octet-stream", filename="private.bin")
    return message.as_bytes()


def full_payload(message):
    headers = [{"name": k, "value": v} for k, v in message.raw_items()]
    result = {"mimeType": message.get_content_type(), "filename": message.get_filename() or "", "headers": headers}
    if message.is_multipart():
        result["parts"] = [full_payload(part) for part in message.iter_parts()]
    elif message.get_filename():
        result["body"] = {"size": 100000000, "attachmentId": "private-attachment"}
    else:
        data = message.get_payload(decode=True) or b""
        result["body"] = {"size": len(data), "data": base64.urlsafe_b64encode(data).decode().rstrip("=")}
    return result


class Response:
    def __init__(self, data, *, status=200, raw=None):
        self.raw = json.dumps(data, ensure_ascii=False).encode() if raw is None else raw
        self.status_code, self.closed = status, False

    def iter_content(self, size):
        for offset in range(0, len(self.raw), size):
            yield self.raw[offset:offset + size]

    def close(self):
        self.closed = True


class Api:
    def __init__(self, raw=None):
        self.raw = message_raw() if raw is None else raw
        self.calls, self.responses = [], []
        self.profile = {"emailAddress": ACCOUNT}
        self.listing = {"messages": [{"id": "a1"}], "resultSizeEstimate": 99999}
        self.labels, self.drafts = ["INBOX", "UNREAD"], {"": {"drafts": [{"id": "d1"}], "resultSizeEstimate": 999}}
        self.before_get = lambda path: None
        self.response_override = None
        self.headers_override = None
        self.payload_override = None

    def get(self, url, **kwargs):
        path = url.removeprefix(BackgroundMailService._BASE)
        self.calls.append((path, kwargs))
        self.before_get(path)
        if self.response_override:
            response = self.response_override(path, kwargs)
        else:
            if path == "/profile":
                data = self.profile
            elif path == "/messages":
                data = self.listing
            elif path == "/drafts":
                data = self.drafts[kwargs["params"].get("pageToken", "")]
            elif kwargs["params"]["format"] == "metadata":
                headers = [{"name": k, "value": v} for k, v in BytesParser(policy=policy.default).parsebytes(self.raw).raw_items()]
                data = {"id": path.rsplit("/", 1)[-1], "labelIds": self.labels,
                        "payload": {"headers": self.headers_override if self.headers_override is not None else headers}}
            else:
                payload = full_payload(BytesParser(policy=policy.default).parsebytes(self.raw))
                data = {"id": path.rsplit("/", 1)[-1], "payload": self.payload_override if self.payload_override is not None else payload}
            response = Response(data)
        self.responses.append(response)
        return response


def backend(raw=None):
    gmail, vault, oauth = gmail_account()
    api = Api(raw)
    return BackgroundMailService(gmail=gmail, session=api), api, gmail, vault, oauth


def test_gmail_oauth_only_requests_read_scope_and_activates_verified_identity():
    service, vault, session = gmail_account()
    started = begin(service)
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    assert set(query["scope"][0].split()) == {"openid", "email", "https://www.googleapis.com/auth/gmail.readonly"}
    assert query["access_type"] == ["offline"]
    assert service.active_account() == ACCOUNT
    assert service.status("google", ACCOUNT)["authenticated"]
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert service._PROVIDER != "google" and (service._PROVIDER, service._ACCOUNT) in vault.tokens


def test_denied_read_scope_restores_previous_token_and_active_revision():
    service, vault, session = gmail_account()
    before, token = service.active_revision(), vault.load("google", ACCOUNT)
    session.token["scope"] = "openid email"
    session.token["access_token"] = "other-fixture-access"
    with pytest.raises(BackgroundMailError, match="읽기 권한"):
        service.complete(begin(service)["state"], "fixture-code")
    assert service.active_revision() == before
    assert vault.load("google", ACCOUNT) == token


def test_read_scope_is_required_after_refresh_and_configuration_is_not_auth_proof():
    service, vault, session = gmail_account()
    saved = vault.load("google", ACCOUNT)
    saved["expires_at"] = 0
    vault.save("google", ACCOUNT, saved)
    session.token["scope"] = "openid email"
    with pytest.raises(BackgroundMailError, match="읽기 권한"):
        service.access_token("google", ACCOUNT)


def test_disconnect_cannot_resurrect_token_during_refresh():
    service, vault, session = gmail_account()
    snapshot = service.active_revision()
    saved = vault.load("google", ACCOUNT)
    saved["expires_at"] = 0
    vault.save("google", ACCOUNT, saved)
    refreshing, release, disconnected = threading.Event(), threading.Event(), threading.Event()
    session.on_post = lambda: (refreshing.set(), release.wait(3))
    refresh = threading.Thread(target=lambda: service.access_token("google", ACCOUNT))
    disconnect = threading.Thread(target=lambda: (service.disconnect("google", ACCOUNT), disconnected.set()))
    refresh.start()
    assert refreshing.wait(2)
    disconnect.start()
    assert not disconnected.wait(0.05)
    release.set()
    refresh.join(2)
    disconnect.join(2)
    assert disconnected.is_set() and service.active_account() == ""
    assert vault.load("google", ACCOUNT) is None
    with pytest.raises(ToolCancelledError):
        service.check_revision(snapshot)


def test_gmail_list_body_and_drafts_are_read_only_and_account_verified():
    service, api, _gmail, _vault, _oauth = backend()
    status = service.connection_status("gmail")
    assert status == {"configured": True, "account": ACCOUNT} and not api.calls
    listing = service.list_inbox("gmail")
    item = listing["items"][0]
    assert listing["scope"] == "inbox" and listing["observed_count"] == 1 and item["unread"]
    assert len(item["message_key"]) == 64 and "a1" != item["message_key"]
    detail = service.read_message("gmail", item["message_ref"])
    assert detail["body"].strip() == "본문 🙂" and not detail["truncated"]
    assert detail["message_key"] == item["message_key"]
    assert "PRIVATE-ATTACHMENT" not in json.dumps(detail)
    assert service.draft_count("gmail") == 1
    assert all(options["allow_redirects"] is False and options["stream"] for _path, options in api.calls)
    assert all(response.closed for response in api.responses)
    assert all(path == "/profile" or path.startswith(("/messages", "/drafts")) for path, _ in api.calls)
    assert [options["params"]["format"] for path, options in api.calls if path.startswith("/messages/")] == ["metadata", "full"]
    assert service.background and service.message_unit == "message"
    assert service.baseline_prefix == "background-v1-account-"


def test_exact_draft_count_pages_unique_ids_and_ignores_estimate():
    service, api, *_ = backend()
    service.list_inbox("gmail")
    api.drafts = {"": {"drafts": [{"id": "d1"}], "nextPageToken": "page+two=", "resultSizeEstimate": 999},
                  "page+two=": {"drafts": [{"id": "d1"}, {"id": "d2"}], "resultSizeEstimate": 444}}
    assert service.draft_count("gmail") == 2
    api.drafts = {"": {"drafts": [], "resultSizeEstimate": 888}}
    assert service.draft_count("gmail") == 0


def test_draft_cursor_cycle_and_untrusted_ids_fail_unknown():
    service, api, *_ = backend()
    service.list_inbox("gmail")
    api.drafts = {"": {"nextPageToken": "again"}, "again": {"nextPageToken": "again"}}
    with pytest.raises(BackgroundMailError):
        service.draft_count("gmail")
    api.drafts = {"": {"drafts": [{"id": "https://evil.test/secret"}]}}
    with pytest.raises(BackgroundMailError):
        service.draft_count("gmail")


def test_html_only_body_excludes_scripts_styles_links_images_and_attachments():
    raw = message_raw('<head><style>STYLE-SECRET</style></head><script>SCRIPT-SECRET</script>'
                      '<p>내용 &amp; 설명</p><img src="https://evil.test/image"><a href="https://evil.test">확인</a>', html=True)
    service, api, *_ = backend(raw)
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    detail = service.read_message("gmail", ref)
    assert "내용 & 설명" in detail["body"] and "확인" in detail["body"]
    assert all(text not in detail["body"] for text in ("SECRET", "https", "PRIVATE-ATTACHMENT", "<p>"))
    assert not any("evil.test" in path for path, _ in api.calls)


def test_long_gmail_body_is_explicitly_partial():
    service, _api, *_ = backend(message_raw("x" * 30000))
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    detail = service.read_message("gmail", ref)
    assert len(detail["body"]) == 16000 and detail["truncated"]


@pytest.mark.parametrize("action", ["refresh", "switch", "disconnect", "revoke"])
def test_stale_refs_never_read_another_account_or_message(action):
    service, api, gmail, *_ = backend()
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    if action == "refresh":
        service.list_inbox("gmail")
    elif action == "switch":
        service.switch_provider()
    elif action == "disconnect":
        service.disconnect()
    else:
        gmail.disconnect("google", ACCOUNT)
    count = len(api.calls)
    with pytest.raises((BackgroundMailError, ToolCancelledError)):
        service.read_message("gmail", ref)
    assert len(api.calls) == count


def test_revocation_during_response_discards_results_before_cache_or_return():
    service, api, gmail, *_ = backend()
    api.before_get = lambda path: gmail.disconnect("google", ACCOUNT) if path == "/messages" else None
    with pytest.raises(ToolCancelledError):
        service.list_inbox("gmail")
    assert not service._messages and all(response.closed for response in api.responses)


def test_account_mismatch_bad_ids_and_metadata_change_are_rejected():
    service, api, *_ = backend()
    api.profile = {"emailAddress": "other@example.test"}
    with pytest.raises(BackgroundMailError, match="계정"):
        service.list_inbox("gmail")
    api.profile = {"emailAddress": ACCOUNT}
    api.listing = {"messages": [{"id": "https://evil.test/path"}]}
    with pytest.raises(BackgroundMailError):
        service.list_inbox("gmail")
    assert not any("evil.test" in path for path, _ in api.calls)
    api.listing = {"messages": [{"id": "a1"}]}
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    api.raw = api.raw.replace(b"Subject:", b"Subject: changed ", 1)
    with pytest.raises(BackgroundMailError, match="변경"):
        service.read_message("gmail", ref)


@pytest.mark.parametrize("response", [Response({}, status=302), Response({}, status=401),
                                      Response({}, raw=b"PRIVATE-ERROR"), Response({}, raw=b"x" * 70000)])
def test_http_protocol_and_size_failures_are_redacted_and_closed(response):
    service, api, *_ = backend()
    api.response_override = lambda path, kwargs: response
    with pytest.raises(BackgroundMailError) as error:
        service.list_inbox("gmail")
    assert "PRIVATE" not in str(error.value) and response.closed


def test_missing_connections_do_not_use_environment_or_browser(monkeypatch):
    monkeypatch.setenv("MAIL_IMAP_USERNAME", "legacy-owner")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD", "legacy-private")
    service = BackgroundMailService(gmail=GmailMailAccountService(Vault()), naver=MailAccountService(Vault()), session=Api())
    for provider in ("gmail", "naver"):
        assert service.connection_status(provider) == {"configured": False, "account": ""}
        with pytest.raises(BackgroundMailError):
            service.list_inbox(provider)
    assert not service.session.calls


def test_cancellation_and_elapsed_deadline_stop_subsequent_api_calls():
    service, api, *_ = backend()
    with pytest.raises(ToolCancelledError):
        service.list_inbox("gmail", checkpoint=lambda: (_ for _ in ()).throw(ToolCancelledError()))
    assert not api.calls
    clock = [0]
    service.clock = lambda: clock[0]
    api.before_get = lambda _path: clock.__setitem__(0, 181)
    with pytest.raises(BackgroundMailError, match="시간"):
        service.list_inbox("gmail")
    assert len(api.calls) == 1 and api.responses[0].closed


class NaverIMAP(FakeIMAP):
    def __init__(self, messages=None):
        super().__init__(messages)
        self.folders = [b'(\\HasNoChildren \\Drafts) "/" "Drafts"']
        self.status_reply = ("OK", [b'"Drafts" (MESSAGES 22)'])

    def uid(self, command, *args):
        result = super().uid(command, *args)
        if command == "FETCH" and " FLAGS " in args[1]:
            meta, raw = result[1][0]
            flags = b"\\Seen" if int(args[0]) % 2 else b""
            meta = meta.replace(b" BODY[", b" FLAGS (" + flags + b") BODY[", 1)
            return "OK", [(meta, raw), b")"]
        return result

    def list(self):
        self.calls.append(("LIST",))
        return "OK", self.folders

    def status(self, mailbox, fields):
        self.calls.append(("STATUS", mailbox, fields))
        return self.status_reply


def naver_backend(messages=None, *, validity=0):
    account = MailAccountService(Vault())
    account.save("fixture", "fixture-app-password")
    server = NaverIMAP(messages)
    server.uidvalidity_reply = ("UIDVALIDITY", [str(validity).encode()])
    def reader(settings, checkpoint):
        return ImapReader(settings, connection_factory=lambda *a, **kw: server, checkpoint=checkpoint)
    return BackgroundMailService(naver=account, reader_factory=reader), server


def test_naver_server_zero_uidvalidity_lists_reads_and_counts_drafts():
    service, server = naver_backend()
    listing = service.list_inbox("naver")
    assert listing["observed_count"] == 1 and listing["items"][0]["unread"] is False
    assert "<fixture-101@" not in json.dumps(listing)
    detail = service.read_message("naver", listing["items"][0]["message_ref"])
    assert "본문 공백" in detail["body"] and service.draft_count("naver") == 22
    assert not {"STORE", "CLOSE", "EXPUNGE"} & {call[0] for call in server.calls}


@pytest.mark.parametrize("header,value", [
    ("Message-ID", "<replacement@example.test>"), ("To", "different@example.test"),
])
def test_naver_zero_epoch_rejects_reused_uid_body_with_changed_identity(header, value):
    service, server = naver_backend()
    listing = service.list_inbox("naver")
    message = BytesParser(policy=policy.SMTP).parsebytes(server.messages[101])
    message.replace_header(header, value)
    server.messages[101] = message.as_bytes()
    with pytest.raises(BackgroundMailError):
        service.read_message("naver", listing["items"][0]["message_ref"])


@pytest.mark.parametrize("validity", [0, 77])
def test_naver_message_id_distinguishes_reused_zero_epoch_uids_only(validity):
    service, server = naver_backend(validity=validity)
    original = service.list_inbox("naver")
    old_key = original["items"][0]["message_key"]
    message = BytesParser(policy=policy.SMTP).parsebytes(server.messages[101])
    message.replace_header("Message-ID", "<replacement@example.test>")
    server.messages[101] = message.as_bytes()
    new_key = service.list_inbox("naver")["items"][0]["message_key"]
    if validity == 0:
        assert new_key != old_key
    else:
        assert new_key == old_key == service._key("naver", "fixture@naver.com", [77, 101])


def test_naver_missing_message_id_uses_headers_to_distinguish_reused_zero_epoch_uid():
    message = BytesParser(policy=policy.SMTP).parsebytes(make_message())
    del message["Message-ID"]
    service, server = naver_backend({101: message.as_bytes()})
    old_key = service.list_inbox("naver")["items"][0]["message_key"]
    message.replace_header("Subject", "다른 메일")
    server.messages[101] = message.as_bytes()
    new_key = service.list_inbox("naver")["items"][0]["message_key"]
    assert new_key != old_key


def test_naver_same_analysis_contract_with_flags_body_and_actual_drafts():
    account = MailAccountService(Vault())
    account.save("fixture", "fixture-app-password")
    imap = NaverIMAP({101: make_message(101), 102: make_message(102)})
    def reader(settings, checkpoint):
        return ImapReader(settings, connection_factory=lambda *a, **kw: imap, checkpoint=checkpoint)
    service = BackgroundMailService(naver=account, reader_factory=reader)
    listing = service.list_inbox("naver")
    assert listing["account"] == "fixture@naver.com" and [row["unread"] for row in listing["items"]] == [True, False]
    ref = listing["items"][0]["message_ref"]
    assert "본문 공백" in service.read_message("naver", ref)["body"]
    assert service.draft_count("naver") == 22
    epoch = listing["baseline_epoch"]
    assert len(epoch) == 64 and service.list_inbox("naver")["baseline_epoch"] == epoch
    imap.uidvalidity_reply = ("UIDVALIDITY", [b"78"])
    assert service.list_inbox("naver")["baseline_epoch"] != epoch
    assert not {"STORE", "CLOSE", "EXPUNGE", "CREATE", "APPEND"} & {call[0] for call in imap.calls}
    service.disconnect()
    assert account.status()["configured"]


@pytest.mark.parametrize("folders,status,expected", [
    ([b'(\\Drafts) "/" "Drafts"'], ("OK", [b'"Drafts" (MESSAGES 0)']), 0),
    ([b'() "/" "Drafts"'], ("OK", [b'Drafts (MESSAGES 2)']), 2),
    ([b'() "/" "INBOX"'], ("OK", [b'INBOX (MESSAGES 99)']), None),
    ([b'(\\Drafts) "/" "A"', b'(\\Drafts) "/" "B"'], ("OK", [b'A (MESSAGES 1)']), None),
    ([b'(\\Noselect \\Drafts) "/" "Drafts"'], ("OK", [b'Drafts (MESSAGES 1)']), None),
    ([b'(\\Drafts) "/" "Drafts"'], ("OK", [b'INBOX (MESSAGES 999)']), None),
])
def test_imap_draft_folder_identity_empty_and_ambiguity(folders, status, expected):
    server = NaverIMAP()
    server.folders, server.status_reply = folders, status
    reader, *_ = make_reader(server)
    if expected is None:
        with pytest.raises(MailReadError):
            reader.draft_count(known_folders=("Drafts",))
    else:
        assert reader.draft_count(known_folders=("Drafts",)) == expected
    assert server.calls[-1] == ("logout",)


def test_optional_html_and_flags_do_not_change_legacy_defaults():
    server = NaverIMAP({101: message_raw("<script>PRIVATE</script><p>본문</p>", html=True)})
    reader, *_ = make_reader(server)
    assert reader.read_message(uid=101, uid_validity=77)["body"] == ""
    assert reader.read_message(uid=101, uid_validity=77, include_html=True)["body"].strip() == "본문"
    assert "unread" not in reader.list_inbox()["items"][0]
    assert reader.list_inbox(include_flags=True)["items"][0]["unread"] is False


def test_missing_imap_flags_fail_closed_instead_of_guessing_unread():
    reader, server, _connections = make_reader()
    with pytest.raises(MailReadError, match="읽음 상태"):
        reader.list_inbox(include_flags=True)
    assert server.calls[-1] == ("logout",)


def test_naver_setting_changes_during_fetch_abort_and_never_use_environment():
    account = MailAccountService(Vault())
    account.save("fixture", "fixture-app-password")
    server = NaverIMAP()
    def reader(settings, checkpoint):
        return ImapReader(settings, connection_factory=lambda *a, **kw: server, checkpoint=checkpoint)
    service = BackgroundMailService(naver=account, reader_factory=reader)
    server.after_call = lambda stage: account.disconnect() if stage == "FETCH" else None
    with pytest.raises(ToolCancelledError):
        service.list_inbox("naver")
    assert not service._messages and server.calls[-1] == ("logout",)


def test_zero_mailboxes_are_valid_and_invalid_provider_or_ref_do_not_request():
    service, api, *_ = backend()
    api.listing = {"resultSizeEstimate": 0}
    assert service.list_current("gmail")["items"] == []
    assert service.draft_count("gmail") == 1
    calls = len(api.calls)
    for provider, ref in (("outlook", "a" * 36 + ":0"), ("gmail", "https://evil.test/")):
        with pytest.raises(BackgroundMailError):
            service.read_message(provider, ref)
    with pytest.raises(BackgroundMailError):
        service._gmail_get("/messages/../../profile", ACCOUNT, {}, lambda: None, time.monotonic() + 20)
    assert len(api.calls) == calls


def test_snapshot_guards_cached_analysis_after_auth_revision_changes():
    service, _api, gmail, *_ = backend()
    service.list_inbox("gmail")
    service.check_snapshot("gmail", ACCOUNT)
    gmail.disconnect("google", ACCOUNT)
    with pytest.raises(ToolCancelledError):
        service.check_snapshot("gmail", ACCOUNT)


def test_full_mime_large_declared_attachment_does_not_download_or_hide_tiny_body():
    service, api, *_ = backend(message_raw("짧은 업무 요청"))
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    detail = service.read_message("gmail", ref)
    assert detail["body"].strip() == "짧은 업무 요청" and not detail["truncated"]
    assert not any("attachment" in path for path, _options in api.calls)


def test_mime_alternative_prefers_plain_and_ignores_attached_email_and_text():
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = "검토 요청"
    message["From"] = "작성자 <sender@example.test>"
    message["Date"] = "Mon, 14 Sep 2026 09:30:00 +0900"
    message.set_content("실제 본문")
    message.add_alternative("<p>HTML-ONLY-SECRET</p>", subtype="html")
    message.add_attachment("TEXT-ATTACHMENT-SECRET", subtype="plain", filename="private.txt")
    attached = EmailMessage()
    attached.set_content("FORWARDED-SECRET")
    message.add_attachment(attached)
    service, _api, *_ = backend(message.as_bytes())
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    detail = service.read_message("gmail", ref)
    assert detail["body"].strip() == "실제 본문" and not detail["truncated"]
    assert "SECRET" not in detail["body"]


@pytest.mark.parametrize("mode", ["bad-base64", "depth", "parts", "missing-body", "duplicate-type"])
def test_invalid_or_unbounded_mime_fails_closed(mode):
    service, api, *_ = backend()
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    payload = full_payload(BytesParser(policy=policy.default).parsebytes(api.raw))
    child = payload["parts"][0]
    if mode == "bad-base64":
        child["body"]["data"] = "abc!PRIVATE"
    elif mode == "depth":
        for _ in range(22):
            child = {"mimeType": "multipart/mixed", "parts": [child]}
        payload["parts"] = [child]
    elif mode == "parts":
        payload["parts"] = [deepcopy(child)] * 101
    elif mode == "missing-body":
        del child["body"]
    else:
        child["headers"].append({"name": "Content-Type", "value": "text/html"})
    api.payload_override = payload
    with pytest.raises(BackgroundMailError) as error:
        service.read_message("gmail", ref)
    assert "PRIVATE" not in str(error.value)


def test_unavailable_inline_text_is_unknown_partial_and_never_fetched():
    service, api, *_ = backend()
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    payload = full_payload(BytesParser(policy=policy.default).parsebytes(api.raw))
    payload["parts"][0]["body"] = {"size": 2000000, "attachmentId": "unavailable-inline"}
    api.payload_override = payload
    detail = service.read_message("gmail", ref)
    assert detail["body"] == "" and detail["truncated"]
    assert not any("attachment" in path for path, _options in api.calls)


def test_text_size_mismatch_is_partial_and_attachment_only_body_is_unavailable():
    service, api, *_ = backend()
    ref = service.list_inbox("gmail")["items"][0]["message_ref"]
    payload = full_payload(BytesParser(policy=policy.default).parsebytes(api.raw))
    payload["parts"][0]["body"]["size"] += 10
    api.payload_override = payload
    assert service.read_message("gmail", ref)["truncated"]
    payload["parts"] = payload["parts"][1:]
    detail = service.read_message("gmail", ref)
    assert detail["body"] == "" and detail["truncated"]
