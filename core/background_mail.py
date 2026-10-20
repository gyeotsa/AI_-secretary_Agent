"""Read-only Gmail API and Naver IMAP mail access without browser automation."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from email import policy
from email.message import EmailMessage
import requests

from config import Config
from core.browser_mail import BrowserMailError
from core.mail_accounts import MailAccountService
from core.mail_runtime import ImapReader, mail_body
from core.plugin import ToolCancelledError
from core.permission import get_permission_manager
from core.remote_runtime import OAuthCoordinator, SecureTokenVault
from core.turn_context import check_turn_cancelled


class BackgroundMailError(BrowserMailError):
    """Fixed application errors; never provider responses or credential text."""


class GmailMailAccountService(OAuthCoordinator):
    SPECS = {"google": {**OAuthCoordinator.SPECS["google"], "scopes": [
        "openid", "email", "https://www.googleapis.com/auth/gmail.readonly"]}}
    _lock = threading.RLock()
    _PROVIDER, _ACCOUNT = "anis-background-mail", "gmail-active-v1"

    def __init__(self, vault=None, session=None):
        super().__init__(vault or SecureTokenVault(str(Path(Config.DB_PATH).with_name("mail_background"))), session)

    def _active(self):
        record = self.vault.load(self._PROVIDER, self._ACCOUNT)
        if record is None:
            return {"account": "", "revision": ""}
        if (not isinstance(record, dict) or set(record) != {"account", "revision"}
                or not isinstance(record["account"], str)
                or record["account"] and not re.fullmatch(r"[^\s@]{1,160}@[^\s@]{1,160}", record["account"])
                or not isinstance(record["revision"], str) or not re.fullmatch(r"[a-f0-9]{32}", record["revision"])):
            raise BackgroundMailError("저장된 Gmail 연결 정보를 확인하지 못했습니다. 계정을 다시 연결하세요.")
        return record

    def active_revision(self):
        with self._lock:
            record = self._active()
            return record["account"], record["revision"]

    def active_account(self):
        return self.active_revision()[0]

    def check_revision(self, snapshot):
        if self.active_revision() != snapshot or not snapshot[0]:
            raise ToolCancelledError("Gmail 연결 설정이 변경되어 진행 중인 작업을 중단했습니다.")

    @staticmethod
    def _require_read_scope(status):
        if (not isinstance(status, dict) or status.get("scopes_verified") is not True
                or "https://www.googleapis.com/auth/gmail.readonly" not in status.get("granted_scopes", [])):
            raise BackgroundMailError("Gmail 읽기 권한이 확인되지 않았습니다. 메일 계정을 다시 연결하세요.")

    def complete(self, state, code, *, check_cancelled=None):
        # The same lock fences connection, refresh and deletion across instances.
        with self._lock:
            pending = self._pending.get(state)
            account = pending["account"] if pending else ""
            old = self.vault.load("google", account) if account else None
            result = super().complete(state, code, check_cancelled=check_cancelled)
            try:
                self._require_read_scope(result)
                if (result.get("authenticated") is not True
                        or str(result.get("identity_email", "")).casefold() != account.casefold()):
                    raise BackgroundMailError("Gmail 계정 신원을 확인하지 못했습니다. 계정을 다시 연결하세요.")
                if check_cancelled is not None:
                    check_cancelled()
                self.vault.save(self._PROVIDER, self._ACCOUNT, {"account": account, "revision": uuid4().hex})
            except Exception:
                if old is None:
                    self.vault.delete("google", account)
                else:
                    self.vault.save("google", account, old)
                raise
            return result

    def access_token(self, provider, account):
        with self._lock:
            if provider != "google" or self.active_account() != account:
                raise BackgroundMailError("백그라운드 Gmail 계정을 먼저 연결하세요.")
            self._require_read_scope(self.status(provider, account))
            access = super().access_token(provider, account)
            self._require_read_scope(self.status(provider, account))
            return access

    def disconnect(self, provider, account):
        if provider != "google":
            raise BackgroundMailError("지원하지 않는 Gmail 연결입니다.")
        with self._lock:
            # Deactivation survives failures deleting a credential file.
            if self.active_account() == account:
                self.vault.save(self._PROVIDER, self._ACCOUNT, {"account": "", "revision": uuid4().hex})
            self.vault.delete(provider, account)


class BackgroundMailService:
    background = True
    message_unit = "message"
    baseline_prefix = "background-v1-account-"
    _BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
    _ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")

    def __init__(self, gmail=None, naver=None, analysis=None, *, session=None, reader_factory=ImapReader,
                 clock=time.monotonic):
        self._gmail, self._naver, self._analysis = gmail, naver, analysis
        self.session = session or requests.Session()
        self.reader_factory, self.clock = reader_factory, clock
        self.workflow_lock = threading.Lock()
        self._operation_lock, self._state_lock = threading.Lock(), threading.RLock()
        self._generation, self._messages, self._snapshots = 0, {}, {}

    @property
    def gmail(self):
        if self._gmail is None:
            self._gmail = GmailMailAccountService()
        return self._gmail

    @property
    def naver(self):
        if self._naver is None:
            self._naver = MailAccountService()
        return self._naver

    @property
    def analysis(self):
        if self._analysis is None:
            from core.mail_analysis import MailAnalysisService
            self._analysis = MailAnalysisService()
        return self._analysis

    @staticmethod
    def _provider(provider):
        if provider not in ("gmail", "naver"):
            raise BackgroundMailError("Gmail 또는 네이버 메일을 선택하세요.")
        return provider

    def connection_status(self, provider):
        self._provider(provider)
        if provider == "gmail":
            account = self.gmail.active_account()
            status = self.gmail.status("google", account) if account else {}
            return {"configured": bool(account and status.get("configured")), "account": account}
        status = self.naver.status()
        return {"configured": status["configured"], "account": status["username"] + "@naver.com" if status["configured"] else ""}

    def disconnect(self):
        with self._state_lock:
            self._generation += 1
            self._messages.clear()
            self._snapshots.clear()

    def switch_provider(self):
        self.disconnect()

    def _credentials(self, provider):
        self._check_read_permission()
        if provider == "gmail":
            snapshot = self.gmail.active_revision()
            if not snapshot[0]:
                raise BackgroundMailError("백그라운드 Gmail 계정을 먼저 연결하세요.")
            return snapshot[0], snapshot
        credentials = self.naver.credentials()
        if credentials is None:
            raise BackgroundMailError("네이버 IMAP 계정과 앱 비밀번호를 먼저 연결하세요.")
        return credentials.sender, credentials

    def _check(self, provider, snapshot, generation, checkpoint):
        checkpoint()
        self._check_read_permission()
        with self._state_lock:
            if generation != self._generation:
                raise ToolCancelledError("메일 연결이 변경되어 이전 조회 결과를 폐기했습니다.")
        (self.gmail if provider == "gmail" else self.naver).check_revision(snapshot)

    @staticmethod
    def _check_read_permission():
        if get_permission_manager().is_blocked("cloud_read"):
            raise BackgroundMailError("권한 관리에서 클라우드 조회가 거절되어 메일을 조회할 수 없습니다.")

    def check_snapshot(self, provider, account):
        self._provider(provider)
        with self._state_lock:
            saved = self._snapshots.get(provider)
            generation = self._generation
        if saved is None or saved[0] != account:
            raise ToolCancelledError("메일 목록 계정이 변경되었습니다. 다시 조회하세요.")
        self._check(provider, saved[1], generation, check_turn_cancelled)

    def _check_deadline(self, check, deadline):
        check()
        if self.clock() >= deadline:
            raise BackgroundMailError("메일 조회 시간 한도를 초과했습니다.")

    def _gmail_get(self, path, account, params, check, deadline, *, size_limit=2097152):
        if not re.fullmatch(r"/(?:profile|messages|messages/[A-Za-z0-9_-]{1,200}|drafts)", path):
            raise BackgroundMailError("메일 API 조회 경로를 확인하지 못했습니다.")
        self._check_deadline(check, deadline)
        access = self.gmail.access_token("google", account)
        self._check_deadline(check, deadline)
        remaining = max(0.1, min(20, deadline - self.clock()))
        response = self.session.get(self._BASE + path, headers={"Authorization": "Bearer " + access},
                                    params=params, timeout=(min(10, remaining), remaining),
                                    allow_redirects=False, stream=True)
        try:
            self._check_deadline(check, deadline)
            if not 200 <= response.status_code < 300:
                raise BackgroundMailError("Gmail API 조회를 완료하지 못했습니다. 연결 권한과 네트워크를 확인하세요.")
            raw = bytearray()
            for chunk in response.iter_content(65536):
                self._check_deadline(check, deadline)
                if not isinstance(chunk, bytes) or len(raw) + len(chunk) > size_limit:
                    raise BackgroundMailError("Gmail 조회 응답이 크기 한도를 초과했습니다.")
                raw.extend(chunk)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise BackgroundMailError("Gmail 조회 응답을 확인하지 못했습니다.")
            self._check_deadline(check, deadline)
            return data
        finally:
            response.close()

    def _gmail_identity(self, account, check, deadline):
        profile = self._gmail_get("/profile", account, {}, check, deadline, size_limit=65536)
        if (not isinstance(profile.get("emailAddress"), str)
                or profile["emailAddress"].casefold() != account.casefold()):
            raise BackgroundMailError("연결한 Gmail 계정과 조회 계정이 다릅니다. 계정을 다시 연결하세요.")

    @staticmethod
    def _headers(headers):
        if not isinstance(headers, list) or len(headers) > 500:
            raise BackgroundMailError("메일 제목·발신 정보를 확인하지 못했습니다.")
        result = {"subject": "", "sender": "", "date": ""}
        names, seen = {"subject": "subject", "from": "sender", "date": "date"}, set()
        for header in headers:
            if not isinstance(header, dict) or not isinstance(header.get("name"), str) or not isinstance(header.get("value"), str):
                raise BackgroundMailError("메일 제목·발신 정보를 확인하지 못했습니다.")
            name, value = header["name"].casefold(), header["value"]
            if name in names:
                if name in seen or "\x00" in value or len(value) > 32768:
                    raise BackgroundMailError("메일 제목·발신 정보를 확인하지 못했습니다.")
                seen.add(name)
                result[names[name]] = str(policy.default.header_factory(name, value))[:2000]
        return result

    @staticmethod
    def _key(provider, account, identity):
        return hashlib.sha256(json.dumps(["background-v1", provider, account, identity],
                                        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def _gmail_body(self, payload, check, deadline):
        budget = {"parts": 0, "bytes": 0, "partial": False}

        def build(part, depth=0):
            self._check_deadline(check, deadline)
            budget["parts"] += 1
            if depth > 20 or budget["parts"] > 100 or not isinstance(part, dict):
                raise BackgroundMailError("Gmail 본문 구조가 조회 한도를 초과했습니다.")
            mime, headers, filename = part.get("mimeType"), part.get("headers", []), part.get("filename", "")
            if (not isinstance(mime, str) or not isinstance(filename, str)
                    or not isinstance(headers, list) or len(headers) > 500):
                raise BackgroundMailError("Gmail 본문 형식을 확인하지 못했습니다.")
            parsed = {}
            for header in headers:
                if (not isinstance(header, dict) or not isinstance(header.get("name"), str)
                        or not isinstance(header.get("value"), str) or len(header["value"]) > 32768):
                    raise BackgroundMailError("Gmail 본문 형식을 확인하지 못했습니다.")
                name = header["name"].casefold()
                if name in ("content-type", "content-disposition"):
                    if name in parsed:
                        raise BackgroundMailError("Gmail 본문 형식을 확인하지 못했습니다.")
                    parsed[name] = policy.default.header_factory(name, header["value"])
            disposition = parsed.get("content-disposition")
            if (filename or mime.casefold() == "message/rfc822"
                    or disposition is not None and (disposition.content_disposition == "attachment"
                                                    or disposition.params.get("filename"))):
                return None
            mime = mime.casefold()
            message = EmailMessage()
            if mime.startswith("multipart/"):
                children = part.get("parts")
                if not isinstance(children, list) or len(children) > 100:
                    raise BackgroundMailError("Gmail 본문 형식을 확인하지 못했습니다.")
                message.set_type(mime)
                for child in children:
                    built = build(child, depth + 1)
                    if built is not None:
                        message.attach(built)
                return message
            if mime not in ("text/plain", "text/html"):
                return None
            body = part.get("body")
            if (not isinstance(body, dict) or type(body.get("size")) is not int or body["size"] < 0):
                raise BackgroundMailError("Gmail 본문 형식을 확인하지 못했습니다.")
            encoded = body.get("data")
            if encoded is None and isinstance(body.get("attachmentId"), str):
                budget["partial"] = True
                return None
            if encoded is None and body["size"] == 0:
                encoded = ""
            if (not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", encoded)
                    or len(encoded.rstrip("=")) % 4 == 1):
                raise BackgroundMailError("Gmail 본문 인코딩을 확인하지 못했습니다.")
            remaining = ImapReader.BODY_LIMIT - budget["bytes"]
            prefix_size = ((remaining + 3) // 3) * 4
            prefix = encoded[:prefix_size]
            decoded = base64.b64decode(prefix + "=" * (-len(prefix) % 4), altchars=b"-_", validate=True)
            if body["size"] < len(decoded):
                raise BackgroundMailError("Gmail 본문 크기를 확인하지 못했습니다.")
            truncated = len(encoded) > prefix_size or len(decoded) > remaining or body["size"] > len(decoded)
            decoded = decoded[:remaining]
            budget["bytes"] += len(decoded)
            budget["partial"] |= truncated
            content_type = parsed.get("content-type")
            charset = content_type.params.get("charset", "utf-8") if content_type is not None else "utf-8"
            text = decoded.decode(charset, errors="replace")
            message.set_content(text, subtype=mime.split("/", 1)[1])
            return message

        message = build(payload)
        self._check_deadline(check, deadline)
        body, available, truncated = mail_body(message, include_html=True) if message is not None else ("", False, False)
        return body, budget["partial"] or truncated or not available

    def list_inbox(self, provider, limit=50, *, checkpoint=check_turn_cancelled):
        self._provider(provider)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise BackgroundMailError("메일 조회 개수는 1~50 사이의 정수여야 합니다.")
        if not self._operation_lock.acquire(blocking=False):
            raise BackgroundMailError("이전 메일 조회가 진행 중입니다.")
        try:
            self.disconnect()
            account, snapshot = self._credentials(provider)
            generation = self._generation
            check = lambda: self._check(provider, snapshot, generation, checkpoint)
            check()
            items, refs, binding = [], {}, str(uuid4())
            if provider == "gmail":
                deadline = self.clock() + 180
                self._gmail_identity(account, check, deadline)
                listing = self._gmail_get("/messages", account, {"labelIds": "INBOX", "maxResults": limit}, check, deadline)
                messages = listing.get("messages", [])
                if not isinstance(messages, list) or len(messages) > limit:
                    raise BackgroundMailError("Gmail 목록 조회 결과를 확인하지 못했습니다.")
                seen = set()
                for message in messages:
                    identifier = message.get("id") if isinstance(message, dict) else None
                    if not isinstance(identifier, str) or not self._ID.fullmatch(identifier) or identifier in seen:
                        raise BackgroundMailError("Gmail 메일 식별자를 확인하지 못했습니다.")
                    seen.add(identifier)
                    data = self._gmail_get("/messages/" + identifier, account,
                        {"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]}, check, deadline)
                    labels = data.get("labelIds")
                    if (data.get("id") != identifier or not isinstance(labels, list)
                            or any(not isinstance(label, str) for label in labels) or "INBOX" not in labels):
                        raise BackgroundMailError("Gmail 목록이 변경되었습니다. 다시 조회하세요.")
                    item = {**self._headers(data.get("payload", {}).get("headers")), "unread": "UNREAD" in labels,
                            "message_key": self._key(provider, account, identifier), "message_ref": binding + ":" + str(len(items))}
                    items.append(item)
                    refs[item["message_ref"]] = (identifier, None, item, None)
                self._gmail_identity(account, check, deadline)
            else:
                reader = self.reader_factory(snapshot.imap_settings(), checkpoint=check)
                listing = reader.list_inbox(limit=limit, include_flags=True)
                for row in listing["items"]:
                    headers = row["headers"]
                    identity = [listing["uid_validity"], row["uid"]]
                    expected_headers = None
                    if listing["uid_validity"] == 0:
                        # shortcut: Naver's zero epoch cannot detect identical-header UID reuse; use a real epoch when available.
                        expected_headers = {key: headers[key] for key in ("Message-ID", "From", "To", "Subject", "Date")}
                        identity.append(expected_headers)
                    item = {"subject": headers["Subject"], "sender": headers["From"], "date": headers["Date"],
                            "unread": row["unread"], "message_key": self._key(provider, account, identity),
                            "message_ref": binding + ":" + str(len(items))}
                    items.append(item)
                    refs[item["message_ref"]] = (row["uid"], listing["uid_validity"], item, expected_headers)
            check()
            with self._state_lock:
                check()
                self._messages = refs
                self._snapshots[provider] = (account, snapshot)
            return {"provider": provider, "account": account, "items": items, "scope": "inbox",
                    "observed_count": len(items), "message_unit": "message",
                    "baseline_epoch": self._key(provider, account, ["uid-epoch", listing["uid_validity"]])
                                      if provider == "naver" else ""}
        except ToolCancelledError:
            raise
        except BackgroundMailError:
            raise
        except Exception:
            raise BackgroundMailError("백그라운드 메일 조회를 확인하지 못했습니다. 메일 계정 연결·권한·네트워크를 확인하세요.") from None
        finally:
            self._operation_lock.release()

    def list_current(self, provider, limit=50, *, checkpoint=check_turn_cancelled):
        return self.list_inbox(provider, limit, checkpoint=checkpoint)

    def read_message(self, provider, message_ref, *, checkpoint=check_turn_cancelled):
        self._provider(provider)
        if not isinstance(message_ref, str) or not re.fullmatch(r"[a-f0-9-]{36}:[0-9]{1,2}", message_ref):
            raise BackgroundMailError("조회한 목록의 메일을 다시 선택하세요.")
        if not self._operation_lock.acquire(blocking=False):
            raise BackgroundMailError("이전 메일 조회가 진행 중입니다.")
        try:
            with self._state_lock:
                saved, entry, generation = self._snapshots.get(provider), self._messages.get(message_ref), self._generation
            if saved is None or entry is None:
                raise BackgroundMailError("메일 목록이 변경되었습니다. 다시 조회하세요.")
            account, snapshot = saved
            identifier, validity, item, expected_headers = entry
            check = lambda: self._check(provider, snapshot, generation, checkpoint)
            check()
            if provider == "gmail":
                deadline = self.clock() + 90
                self._gmail_identity(account, check, deadline)
                data = self._gmail_get("/messages/" + identifier, account, {"format": "full"}, check, deadline)
                payload = data.get("payload")
                if data.get("id") != identifier or not isinstance(payload, dict):
                    raise BackgroundMailError("Gmail 본문 조회 결과를 확인하지 못했습니다.")
                metadata = self._headers(payload.get("headers"))
                body, truncated = self._gmail_body(payload, check, deadline)
                self._gmail_identity(account, check, deadline)
            else:
                reader = self.reader_factory(snapshot.imap_settings(), checkpoint=check)
                detail = reader.read_message(uid=identifier, uid_validity=validity, include_html=True)
                if expected_headers is not None and expected_headers != {key: detail["headers"].get(key) for key in expected_headers}:
                    raise BackgroundMailError("선택한 메일 식별 정보가 변경되었습니다. 목록을 다시 조회하세요.")
                metadata = {"subject": detail["headers"]["Subject"], "sender": detail["headers"]["From"], "date": detail["headers"]["Date"]}
                body, truncated = detail["body"], detail["truncated"]
            if metadata != {key: item[key] for key in ("subject", "sender", "date")}:
                raise BackgroundMailError("선택한 메일 정보가 변경되었습니다. 목록을 다시 조회하세요.")
            check()
            return {"provider": provider, "account": account, "message_ref": message_ref, **item,
                    "body": body, "truncated": truncated}
        except ToolCancelledError:
            raise
        except BackgroundMailError:
            raise
        except Exception:
            raise BackgroundMailError("백그라운드 메일 본문을 확인하지 못했습니다. 목록과 계정 연결을 확인하세요.") from None
        finally:
            self._operation_lock.release()

    def draft_count(self, provider, *, checkpoint=check_turn_cancelled):
        self._provider(provider)
        if not self._operation_lock.acquire(blocking=False):
            raise BackgroundMailError("이전 메일 조회가 진행 중입니다.")
        try:
            with self._state_lock:
                saved, generation = self._snapshots.get(provider), self._generation
            if saved is None:
                raise BackgroundMailError("받은편지함을 먼저 조회하세요.")
            account, snapshot = saved
            check = lambda: self._check(provider, snapshot, generation, checkpoint)
            check()
            if provider == "naver":
                reader = self.reader_factory(snapshot.imap_settings(), checkpoint=check)
                count = reader.draft_count(known_folders=("Drafts",))
            else:
                deadline = self.clock() + 180
                self._gmail_identity(account, check, deadline)
                seen, pages, cursor = set(), set(), ""
                for _ in range(200):
                    params = {"maxResults": 500}
                    if cursor:
                        params["pageToken"] = cursor
                    data = self._gmail_get("/drafts", account, params, check, deadline)
                    drafts = data.get("drafts", [])
                    if not isinstance(drafts, list) or len(drafts) > 500:
                        raise BackgroundMailError("Gmail 임시보관함 조회 결과를 확인하지 못했습니다.")
                    for draft in drafts:
                        identifier = draft.get("id") if isinstance(draft, dict) else None
                        if not isinstance(identifier, str) or not self._ID.fullmatch(identifier):
                            raise BackgroundMailError("Gmail 임시보관함 식별자를 확인하지 못했습니다.")
                        seen.add(identifier)
                    cursor = data.get("nextPageToken", "")
                    if not cursor:
                        break
                    if (not isinstance(cursor, str) or len(cursor) > 4096 or cursor in pages
                            or any(ord(c) < 33 or ord(c) > 126 for c in cursor)):
                        raise BackgroundMailError("Gmail 임시보관함 페이지를 확인하지 못했습니다.")
                    pages.add(cursor)
                else:
                    raise BackgroundMailError("Gmail 임시보관함 조회 한도를 초과했습니다.")
                self._gmail_identity(account, check, deadline)
                count = len(seen)
            check()
            return count
        except ToolCancelledError:
            raise
        except Exception:
            raise BackgroundMailError("임시보관함의 실제 초안 건수를 확인하지 못했습니다.") from None
        finally:
            self._operation_lock.release()


_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def get_background_mail_service():
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BackgroundMailService()
        return _SERVICE
