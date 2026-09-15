"""One explicitly selected Naver mail account, encrypted at rest with DPAPI.

This is account configuration, not an OAuth/session status cache. Verification
authenticates only; no message list, body, recipient or calendar is accessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import imaplib
import re
import smtplib
import ssl
import threading
import time
import uuid

from core.mail_runtime import ImapSettings, MailReadError
from core.plugin import ToolCancelledError
from core.remote_runtime import SecureTokenVault


@dataclass(frozen=True)
class NaverMailCredentials:
    username: str
    password: str = field(repr=False)
    revision: str

    @property
    def sender(self):
        return self.username + "@naver.com"

    def imap_settings(self):
        return ImapSettings("imap.naver.com", self.username, self.password, 993)


class MailAccountService:
    PROVIDER = "anis-mail-settings"
    ACCOUNT = "naver-active-v1"
    # Shared by instances in this process, including a dialog and a tool worker.
    # Network operations do not hold this lock. Revisions fence stale results.
    _lock = threading.RLock()

    def __init__(self, vault=None, *, imap_factory=None, smtp_factory=None,
                 clock=time.monotonic, now=None):
        self._vault = vault
        self.imap_factory = imap_factory or imaplib.IMAP4_SSL
        self.smtp_factory = smtp_factory or smtplib.SMTP
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def vault(self):
        if self._vault is None:
            self._vault = SecureTokenVault()
        return self._vault

    @staticmethod
    def _username(value):
        if not isinstance(value, str):
            raise MailReadError("네이버 아이디 또는 @naver.com 주소를 입력하세요.")
        value = value.strip()
        if value.lower().endswith("@naver.com"):
            value = value[:-10]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise MailReadError("네이버 아이디 또는 @naver.com 주소를 입력하세요.")
        return value

    @staticmethod
    def _password(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise MailReadError("앱 비밀번호 형식이 올바르지 않습니다.")
        return value

    def _load(self):
        try:
            record = self.vault.load(self.PROVIDER, self.ACCOUNT)
            if record is None:
                return None
            if (not isinstance(record, dict) or type(record.get("version")) is not int
                    or record["version"] != 1 or type(record.get("enabled")) is not bool
                    or not isinstance(record.get("revision"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", record["revision"])):
                raise ValueError("invalid record")
            expected = {"version", "enabled", "revision"}
            if record["enabled"]:
                expected |= {"username", "password", "verified_at"}
                if self._username(record["username"]) != record["username"]:
                    raise ValueError("invalid identity")
                self._password(record["password"])
                if record["verified_at"] is not None:
                    stamp = datetime.fromisoformat(record["verified_at"])
                    if stamp.tzinfo is None:
                        raise ValueError("invalid verification time")
            if set(record) != expected:
                raise ValueError("invalid fields")
            return dict(record)
        except Exception:
            raise MailReadError("암호화된 메일 연결 정보를 확인하지 못했습니다. 계정을 다시 연결하세요.") from None

    def _store(self, record):
        try:
            self.vault.save(self.PROVIDER, self.ACCOUNT, record)
        except Exception:
            raise MailReadError("메일 연결 정보를 암호화 저장하지 못했습니다.") from None

    @staticmethod
    def _status(record):
        configured = bool(record and record["enabled"])
        return {"configured": configured,
                "username": record["username"] if configured else "",
                "verified_at": record["verified_at"] if configured else None,
                "reason": ("saved" if configured else "disconnected" if record else "not_configured")}

    def status(self):
        with self._lock:
            return self._status(self._load())

    def save(self, username, password):
        record = {"version": 1, "enabled": True, "revision": uuid.uuid4().hex,
                  "username": self._username(username), "password": self._password(password),
                  "verified_at": None}
        with self._lock:
            self._store(record)
            return self._status(record)

    def disconnect(self):
        # Replace the credential payload with a credential-free tombstone. It
        # deliberately disables legacy environment fallback until the user saves
        # a new account. Never revoke remote passwords or delete other accounts.
        record = {"version": 1, "enabled": False, "revision": uuid.uuid4().hex}
        with self._lock:
            self._store(record)
            return self._status(record)

    def credentials(self):
        """None means never configured (legacy env allowed); disabled fails shut."""
        with self._lock:
            record = self._load()
            if record is None:
                return None
            if not record["enabled"]:
                raise MailReadError("메일 연결이 해제되어 있습니다. 설정에서 계정을 다시 연결하세요.")
            return NaverMailCredentials(record["username"], record["password"], record["revision"])

    def check_revision(self, credentials):
        with self._lock:
            record = self._load()
            if credentials is None and record is None:
                return
            if (credentials is None or not record or not record["enabled"]
                    or record["revision"] != credentials.revision):
                raise ToolCancelledError("메일 계정 설정이 변경되어 진행 중인 작업을 중단했습니다.")

    def verify(self, *, checkpoint=None):
        checkpoint = checkpoint or (lambda: None)
        checkpoint()
        credentials = self.credentials()
        if credentials is None:
            raise MailReadError("검사할 로컬 메일 계정을 먼저 저장하세요.")
        deadline = self.clock() + 60

        def check():
            checkpoint()
            self.check_revision(credentials)
            if self.clock() >= deadline:
                raise ToolCancelledError("메일 연결 검사 제한 시간을 초과했습니다.")

        result = {"imap_authenticated": False, "smtp_authenticated": False,
                  "verified_at": None, "reason": "authentication_unverified"}
        imap = None
        try:
            check()
            imap = self.imap_factory("imap.naver.com", 993,
                                     ssl_context=ssl.create_default_context(), timeout=10)
            check()
            reply = imap.login(credentials.username, credentials.password)
            check()
            result["imap_authenticated"] = isinstance(reply, tuple) and reply[0] == "OK"
        except ToolCancelledError:
            raise
        except Exception:
            # Neither exception text nor authentication replies leave this scope.
            pass
        finally:
            if imap is not None:
                try:
                    imap.logout()
                except Exception:
                    try:
                        imap.shutdown()
                    except Exception:
                        pass
        check()
        smtp = None
        try:
            smtp = self.smtp_factory("smtp.naver.com", 587, timeout=10)
            check()
            smtp.ehlo()
            check()
            smtp.starttls(context=ssl.create_default_context())
            check()
            smtp.ehlo()
            check()
            reply = smtp.login(credentials.username, credentials.password)
            check()
            result["smtp_authenticated"] = isinstance(reply, tuple) and reply[0] == 235
        except ToolCancelledError:
            raise
        except Exception:
            pass
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    pass
                finally:
                    try:
                        smtp.close()
                    except Exception:
                        pass
        check()
        with self._lock:
            check()
            record = self._load()
            # Persist only successful checks of this exact credential revision.
            # A failed recheck clears old success instead of presenting it as new.
            if result["imap_authenticated"] and result["smtp_authenticated"]:
                result["verified_at"] = self.now()
                result["reason"] = "authenticated_at_time"
            record["verified_at"] = result["verified_at"]
            self._store(record)
        return result
