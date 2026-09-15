"""Bounded, read-only IMAP access. Message contents are untrusted source data."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
import imaplib
import os
import re
import ssl
import time


class MailReadError(ValueError):
    """Safe errors: never include credentials or server response text."""


@dataclass(frozen=True)
class ImapSettings:
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 993

    def __post_init__(self):
        if (not isinstance(self.host, str) or len(self.host) > 253
                or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                           for label in self.host.split("."))):
            raise MailReadError("IMAP 호스트는 공백·경로 없는 서버 이름이어야 합니다.")
        if (not isinstance(self.username, str) or not self.username or self.username != self.username.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.username)):
            raise MailReadError("IMAP 사용자 이름 형식이 올바르지 않습니다.")
        if not isinstance(self.password, str) or not self.password or any(char in self.password for char in "\r\n\x00"):
            raise MailReadError("IMAP 비밀번호 설정 형식이 올바르지 않습니다.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise MailReadError("IMAP 포트 범위가 올바르지 않습니다.")

    @classmethod
    def from_environment(cls):
        naver = os.getenv("MAIL_PROVIDER", "").strip().lower() == "naver"
        host = os.getenv("MAIL_IMAP_HOST") or ("imap.naver.com" if naver else "")
        username = os.getenv("MAIL_IMAP_USERNAME") or ""
        password = os.getenv("MAIL_IMAP_PASSWORD") or ""
        if not all((host, username, password)):
            raise MailReadError("MAIL_IMAP_HOST/USERNAME/PASSWORD 설정이 필요합니다. 네이버는 IMAP 사용 설정과 앱 비밀번호를 확인하세요.")
        try:
            port = int(os.getenv("MAIL_IMAP_PORT", "993"))
        except ValueError:
            raise MailReadError("IMAP 포트는 정수여야 합니다.") from None
        if not 1 <= port <= 65535:
            raise MailReadError("IMAP 포트 범위가 올바르지 않습니다.")
        return cls(host, username, password, port)


def _uid(value, label="UID"):
    if type(value) is not int or not 1 <= value <= 4294967295:
        raise MailReadError(f"{label}는 1~4294967295 범위의 정수여야 합니다.")
    return value


class ImapReader:
    HEADER_LIMIT = 32768
    BODY_LIMIT = 262144
    SEARCH_LIMIT = 1048576

    def __init__(self, settings, *, connection_factory=None, checkpoint=None, clock=time.monotonic):
        self.settings = settings
        self.factory = connection_factory or imaplib.IMAP4_SSL
        self.checkpoint = checkpoint or (lambda: None)
        self.clock = clock

    @staticmethod
    def _ok(reply):
        if not isinstance(reply, tuple) or len(reply) != 2 or reply[0] != "OK":
            raise MailReadError("IMAP 서버가 요청을 완료하지 못했습니다.")
        return reply[1]

    @contextmanager
    def _inbox(self, expected_validity=None):
        deadline = self.clock() + 60

        def check():
            self.checkpoint()
            if self.clock() >= deadline:
                raise MailReadError("메일 조회 시간 한도를 초과했습니다.")

        check()
        client = self.factory(self.settings.host, self.settings.port,
                              ssl_context=ssl.create_default_context(), timeout=10)
        try:
            check()
            self._ok(client.login(self.settings.username, self.settings.password))
            check()
            self._ok(client.select("INBOX", readonly=True))
            check()
            reply = client.response("UIDVALIDITY")
            data = reply[1] if isinstance(reply, tuple) and len(reply) == 2 and reply[0] == "UIDVALIDITY" else None
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], bytes) or not re.fullmatch(rb"[0-9]{1,10}", data[0]):
                raise MailReadError("메일함 UIDVALIDITY를 확인할 수 없습니다.")
            validity = _uid(int(data[0]), "UIDVALIDITY")
            if expected_validity is not None and validity != expected_validity:
                raise MailReadError("메일함이 변경되었습니다. 목록을 다시 조회한 뒤 메일을 선택하세요.")
            yield client, validity, check
            check()
        finally:
            # LOGOUT only: no STORE, CLOSE, EXPUNGE or read-flag mutation.
            try:
                client.logout()
            except Exception:
                pass

    def _fetch(self, client, uid, section, limit, check):
        check()
        data = self._ok(client.uid("FETCH", str(uid), f"(UID RFC822.SIZE BODY.PEEK[{section}]<0.{limit + 1}>)"))
        check()
        records = [item for item in (data or []) if isinstance(item, tuple)]
        if len(records) != 1 or len(records[0]) != 2:
            raise MailReadError("메일이 삭제되었거나 조회 응답이 불완전합니다. 목록을 다시 확인하세요.")
        metadata, raw = records[0]
        if not isinstance(metadata, bytes) or not isinstance(raw, bytes):
            raise MailReadError("메일 조회 응답 형식이 올바르지 않습니다.")
        matches = re.findall(rb"\bUID ([0-9]+)\b", metadata)
        sizes = re.findall(rb"\bRFC822\.SIZE ([0-9]+)\b", metadata)
        if len(matches) != 1 or int(matches[0]) != uid or len(sizes) != 1:
            raise MailReadError("요청한 메일과 응답의 UID가 일치하지 않습니다.")
        truncated = len(raw) > limit or (section == "" and int(sizes[0]) > len(raw))
        return raw[:limit], truncated

    @staticmethod
    def _headers(raw):
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        return {name: str(message.get(name, ""))[:2000]
                for name in ("From", "To", "Subject", "Date", "Message-ID")}

    def list_inbox(self, *, limit=20, unread_only=False, before_uid=None, uid_validity=None):
        if type(limit) is not int or not 1 <= limit <= 50 or type(unread_only) is not bool:
            raise MailReadError("메일 조회 개수는 1~50, unread_only는 참/거짓이어야 합니다.")
        if uid_validity is not None:
            _uid(uid_validity, "UIDVALIDITY")
        if before_uid is not None:
            _uid(before_uid)
            if uid_validity is None:
                raise MailReadError("다음 페이지 조회에는 이전 결과의 uid_validity가 필요합니다.")
        with self._inbox(uid_validity) as (client, validity, check):
            terms = ["UNSEEN" if unread_only else "ALL"]
            if before_uid == 1:
                ids = []
            else:
                if before_uid is not None:
                    terms += ["UID", f"1:{before_uid - 1}"]
                check()
                data = self._ok(client.uid("SEARCH", None, *terms))
                check()
                if not isinstance(data, list) or any(not isinstance(x, bytes) for x in data):
                    raise MailReadError("메일 목록 응답 형식이 올바르지 않습니다.")
                if sum(map(len, data)) > self.SEARCH_LIMIT:
                    raise MailReadError("메일 목록이 조회 한도를 초과했습니다. 읽지 않은 메일로 범위를 줄이세요.")
                tokens = b" ".join(data).split()
                if any(not re.fullmatch(rb"[0-9]{1,10}", x) for x in tokens):
                    raise MailReadError("메일 목록의 UID 형식이 올바르지 않습니다.")
                ids = sorted({_uid(int(x)) for x in tokens}, reverse=True)
                if before_uid is not None and any(x >= before_uid for x in ids):
                    raise MailReadError("서버가 요청한 메일 조회 범위를 벗어났습니다.")
            items = []
            for uid in ids[:limit]:
                raw, truncated = self._fetch(client, uid, "HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)", self.HEADER_LIMIT, check)
                items.append({"uid": uid, "headers": self._headers(raw), "headers_truncated": truncated})
            return {"mailbox": "INBOX", "uid_validity": validity, "items": items,
                    "more": len(ids) > limit, "next_before_uid": items[-1]["uid"] if len(ids) > limit else None,
                    "unread_only": unread_only, "read_only": True, "content_trust": "untrusted_external",
                    "snapshot": False, "body_included": False}

    def read_message(self, *, uid, uid_validity):
        _uid(uid)
        _uid(uid_validity, "UIDVALIDITY")
        with self._inbox(uid_validity) as (client, validity, check):
            raw, truncated = self._fetch(client, uid, "", self.BODY_LIMIT, check)
            message = BytesParser(policy=policy.default).parsebytes(raw)
            # Never expose attachments or execute/render HTML. Outlook-style
            # attached messages can contain text/plain too; get_body excludes them.
            part = message.get_body(preferencelist=("plain",))
            body = part.get_content() if part is not None else ""
            if not isinstance(body, str):
                body = ""
            text_limit = 16000
            return {"mailbox": "INBOX", "uid_validity": validity, "uid": uid,
                    "headers": self._headers(raw), "body": body[:text_limit],
                    "body_available": part is not None, "body_format": "text/plain",
                    "truncated": truncated or len(body) > text_limit,
                    "read_only": True, "attachments_included": False,
                    "content_trust": "untrusted_external"}
