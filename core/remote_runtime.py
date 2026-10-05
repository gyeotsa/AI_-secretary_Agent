"""OAuth, encrypted credentials, approval drafts and verified remote adapters."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

import requests

from config import Config


class RemoteRuntimeError(RuntimeError):
    """Authentication, transport, or verification failure."""


class RemoteApplyRejected(RemoteRuntimeError):
    """The request was rejected before a remote mutation was attempted."""


class RemoteApplyUncertain(RemoteRuntimeError):
    """A mutation was attempted, but its final remote state is not verified."""

    def __init__(self, message: str, *, remote_id: str = "", stage: str = ""):
        super().__init__(message)
        self.remote_id = str(remote_id or "")
        self.stage = str(stage or "")


class RemoteApplyReceipt(str):
    """Backward-compatible remote ID carrying semantic read-back evidence."""

    def __new__(cls, remote_id: str, verification: Dict[str, Any]):
        value = str(remote_id or "")
        instance = super().__new__(cls, value)
        instance.remote_id = value
        instance.verification = dict(verification)
        return instance


class CatalogSyncResult(list):
    """List-compatible metadata batch; pagination is not snapshot completeness.

    A continuation contains only the pages fetched by that call. Without a
    persisted full-scan accumulator, even its terminal page is merge-only.
    """

    def __init__(self, provider: str, account: str, *, query: str = "", resumed: bool = False):
        super().__init__()
        self.provider, self.account = provider, account
        self.query, self.resumed = query, resumed
        self.complete = False
        self.pages_fetched = 0
        self.next_cursor = ""
        self.error = ""
        self.cancelled = False
        self.incomplete_search = False
        self.deleted_ids: set[str] = set()

    @property
    def full_snapshot(self) -> bool:
        return (self.complete and not self.resumed and not self.query
                and not self.error and not self.cancelled and not self.incomplete_search)

    def metadata(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "account": self.account,
            "scope": "filtered_metadata" if self.query else "accessible_metadata",
            "pagination_complete": self.complete, "snapshot_complete": self.full_snapshot,
            "resumed": self.resumed, "pages_fetched": self.pages_fetched,
            "next_cursor": self.next_cursor, "error": self.error,
            "cancelled": self.cancelled, "incomplete_search": self.incomplete_search,
            "observed_deleted_count": len(self.deleted_ids),
            "content_synced": False,
        }


class SecureTokenVault:
    """DPAPI tokens bound to exact provider/account labels in a v2 envelope.

    Legacy filenames lost punctuation, case and long suffixes, and their token
    payloads contain no trusted owner binding. They are never loaded, migrated
    or deleted implicitly: reconnect the intended account to create a v2 token.
    """

    FORMAT = "anis.oauth.v2"

    def __init__(self, directory: Optional[str] = None):
        self.directory = Path(directory or Path(Config.DB_PATH).with_name("oauth_tokens"))
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _scope(provider: str, account: str) -> tuple[str, str]:
        # Labels are exact. Do not casefold/normalize user aliases or guess that
        # two spellings identify the same remote account.
        for value in (provider, account):
            if (not isinstance(value, str) or not value or value != value.strip()
                    or len(value) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise RemoteRuntimeError("자격증명 제공자와 계정 이름 형식이 올바르지 않습니다.")
        return provider, account

    @classmethod
    def _name(cls, provider: str, account: str) -> str:
        scope = cls._scope(provider, account)
        # JSON array boundaries distinguish e.g. ("a_b", "c") and ("a", "b_c").
        encoded = json.dumps(scope, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return "v2-" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _legacy_name(provider: str, account: str) -> str:
        raw = f"{provider.casefold()}_{account.casefold()}"
        return "".join(c if c.isalnum() or c in "_-" else "_" for c in raw)[:100]

    def _require_no_legacy(self, provider: str, account: str) -> None:
        # Check only the caller's computed legacy path, never enumerate or
        # decrypt other accounts to try to infer ownership.
        legacy = self.directory / f"{self._legacy_name(provider, account)}.dpapi"
        if legacy.exists():
            raise RemoteRuntimeError(
                "기존 자격증명은 계정 소유자를 안전하게 구분할 수 없어 사용하지 않았습니다. "
                "기존 파일은 보존했으니 해당 계정을 다시 연결하세요."
            )

    def save(self, provider: str, account: str, token: Dict[str, Any]) -> None:
        path = self.directory / f"{self._name(provider, account)}.dpapi"
        if not isinstance(token, dict):
            raise RemoteRuntimeError("저장할 자격증명은 객체여야 합니다.")
        import win32crypt
        envelope = {"format": self.FORMAT, "provider": provider, "account": account, "token": token}
        data = json.dumps(envelope, ensure_ascii=True, allow_nan=False).encode("ascii")
        protected = win32crypt.CryptProtectData(data, "Jarvis OAuth", None, None, None, 0)
        encrypted = protected[1] if isinstance(protected, tuple) else protected
        temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
        created = False
        try:
            with temporary.open("xb") as handle:
                created = True
                handle.write(encrypted)
            os.replace(temporary, path)
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    def load(self, provider: str, account: str) -> Optional[Dict[str, Any]]:
        path = self.directory / f"{self._name(provider, account)}.dpapi"
        if not path.is_file():
            self._require_no_legacy(provider, account)
            return None
        import win32crypt
        try:
            unprotected = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)
            data = unprotected[1] if isinstance(unprotected, tuple) else unprotected
            envelope = json.loads(data.decode("utf-8"))
            if (not isinstance(envelope, dict)
                    or set(envelope) != {"format", "provider", "account", "token"}
                    or envelope["format"] != self.FORMAT
                    or envelope["provider"] != provider or envelope["account"] != account
                    or not isinstance(envelope["token"], dict)):
                raise ValueError("credential scope mismatch")
            return envelope["token"]
        except Exception:
            # Neither decrypted payloads nor cryptographic exception details
            # belong in a user-facing error or log.
            raise RemoteRuntimeError("저장된 자격증명의 무결성 또는 계정 소유자를 확인하지 못했습니다.") from None

    def delete(self, provider: str, account: str) -> bool:
        path = self.directory / f"{self._name(provider, account)}.dpapi"
        if not path.exists():
            self._require_no_legacy(provider, account)
            return False
        path.unlink()
        return True


class OAuthCoordinator:
    SPECS = {
        "google": {
            "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
            "token": "https://oauth2.googleapis.com/token",
            "client_id": "GOOGLE_OAUTH_CLIENT_ID",
            "client_secret": "GOOGLE_OAUTH_CLIENT_SECRET",
            "scopes": ["openid", "email", "https://www.googleapis.com/auth/gmail.modify",
                       "https://www.googleapis.com/auth/calendar",
                       "https://www.googleapis.com/auth/drive.readonly"],
        },
        "microsoft": {
            "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "client_id": "MICROSOFT_OAUTH_CLIENT_ID",
            "client_secret": "MICROSOFT_OAUTH_CLIENT_SECRET",
            "scopes": ["openid", "profile", "offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send",
                       "Calendars.ReadWrite", "Files.Read", "ChannelMessage.Read.All",
                       "ChannelMessage.Send"],
        },
    }

    def __init__(self, vault: Optional[SecureTokenVault] = None, session=None):
        self.vault = vault or SecureTokenVault()
        self.session = session or requests.Session()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._inflight: Dict[str, Dict[str, Any]] = {}
        self._oauth_lock = threading.RLock()

    def begin(self, provider: str, account: str, redirect_uri: str, *,
              client_id: Optional[str] = None, client_secret: Optional[str] = None,
              expected_identity: str = "") -> Dict[str, str]:
        if provider not in self.SPECS:
            raise RemoteRuntimeError(f"지원하지 않는 OAuth Provider: {provider}")
        SecureTokenVault._scope(provider, account)
        try:
            redirect = urlsplit(redirect_uri)
            valid_redirect = (redirect.scheme == "http" and redirect.hostname == "127.0.0.1"
                              and not redirect.username and not redirect.password
                              and not redirect.query and not redirect.fragment
                              and redirect.path.startswith("/") and "\\" not in redirect_uri
                              and (redirect.port is None or 1 <= redirect.port <= 65535)
                              and not any(ord(c) < 33 or ord(c) == 127 for c in redirect_uri))
        except (ValueError, TypeError):
            valid_redirect = False
        if not valid_redirect:
            raise RemoteRuntimeError("OAuth 콜백은 127.0.0.1 HTTP 주소여야 합니다.")
        if expected_identity and not re.fullmatch(r"[^\s@]{1,160}@[^\s@]{1,160}", expected_identity):
            raise RemoteRuntimeError("연결할 계정 이메일 형식이 올바르지 않습니다.")
        spec = self.SPECS[provider]
        client_id = (os.getenv(spec["client_id"], "") if client_id is None else client_id).strip()
        client_secret = os.getenv(spec["client_secret"], "") if client_secret is None else client_secret
        if not client_id:
            raise RemoteRuntimeError(f"{spec['client_id']} 설정이 필요합니다.")
        if (len(client_id) > 2048 or len(client_secret) > 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in client_id + client_secret)):
            raise RemoteRuntimeError("OAuth 앱 자격증명 형식이 올바르지 않습니다.")
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self._oauth_lock:
            for old_state, old in list(self._pending.items()) + list(self._inflight.items()):
                if (time.monotonic() >= old["deadline"]
                        or (old["provider"], old["account"]) == (provider, account)):
                    self.cancel(old_state)
            if len(self._pending) + len(self._inflight) >= 32:
                raise RemoteRuntimeError("진행 중인 OAuth 연결이 너무 많습니다.")
            self._pending[state] = {"provider": provider, "account": account,
                                    "redirect_uri": redirect_uri, "verifier": verifier,
                                    "created_at": time.time(), "deadline": time.monotonic() + 600,
                                    "client_id": client_id, "client_secret": client_secret,
                                    "expected_identity": expected_identity,
                                    "cancelled": threading.Event()}
        params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "scope": " ".join(spec["scopes"]), "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        if provider == "google":
            params.update({"access_type": "offline", "prompt": "consent select_account"})
        else:
            params["prompt"] = "select_account"
        if expected_identity:
            params["login_hint"] = expected_identity
        return {"authorization_url": spec["authorize"] + "?" + urlencode(params), "state": state}

    def cancel(self, state: str) -> bool:
        with self._oauth_lock:
            pending = self._pending.pop(state, None) or self._inflight.get(state)
            if pending is None:
                return False
            pending["cancelled"].set()
            return True

    @staticmethod
    def _token_payload(payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise RemoteRuntimeError("OAuth 토큰 응답 형식이 올바르지 않습니다.")
        access = payload.get("access_token")
        if (not isinstance(access, str) or not access or len(access) > 32768
                or any(ord(c) < 33 or ord(c) == 127 for c in access)
                or str(payload.get("token_type", "")).casefold() != "bearer"):
            raise RemoteRuntimeError("OAuth Bearer 토큰을 확인하지 못했습니다.")
        expires = payload.get("expires_in")
        if isinstance(expires, bool):
            raise RemoteRuntimeError("OAuth 토큰 만료 정보를 확인하지 못했습니다.")
        try:
            seconds = float(expires)
        except (ValueError, TypeError):
            seconds = 0
        if not math.isfinite(seconds) or not 0 < seconds <= 31536000:
            raise RemoteRuntimeError("OAuth 토큰 만료 정보를 확인하지 못했습니다.")
        scope = payload.get("scope")
        if scope is not None and (not isinstance(scope, str) or len(scope) > 16384
                                  or any(ord(c) < 32 or ord(c) == 127 for c in scope)):
            raise RemoteRuntimeError("OAuth 허용 범위를 확인하지 못했습니다.")
        token = dict(payload)
        refresh = token.get("refresh_token")
        if refresh is not None and (not isinstance(refresh, str) or not refresh or len(refresh) > 32768
                                    or any(ord(c) < 33 or ord(c) == 127 for c in refresh)):
            raise RemoteRuntimeError("OAuth 갱신 토큰 형식이 올바르지 않습니다.")
        token["expires_at"] = time.time() + seconds
        token["granted_scopes"] = sorted(set(scope.split())) if scope is not None else []
        token["scopes_verified"] = scope is not None
        return token

    def _identity(self, provider: str, access: str) -> tuple[str, str, List[str]]:
        url = ("https://openidconnect.googleapis.com/v1/userinfo" if provider == "google"
               else "https://graph.microsoft.com/v1.0/me")
        kwargs = {"headers": {"Authorization": f"Bearer {access}"},
                  "timeout": 15, "allow_redirects": False}
        if provider == "microsoft":
            kwargs["params"] = {"$select": "id,mail,userPrincipalName"}
        response = self.session.get(url, **kwargs)
        self._oauth_raise(response)
        identity = response.json()
        if not isinstance(identity, dict):
            raise RemoteRuntimeError("OAuth 계정 신원을 확인하지 못했습니다.")
        subject = identity.get("sub" if provider == "google" else "id")
        emails = [identity.get("email")] if provider == "google" else [identity.get("mail"), identity.get("userPrincipalName")]
        emails = [value for value in emails if isinstance(value, str)
                  and re.fullmatch(r"[^\s@]{1,160}@[^\s@]{1,160}", value)]
        if (not isinstance(subject, str) or not subject or len(subject) > 512 or not emails
                or any(ord(c) < 32 or ord(c) == 127 for c in subject)
                or (provider == "google" and identity.get("email_verified") is not True)):
            raise RemoteRuntimeError("OAuth 계정 신원을 확인하지 못했습니다.")
        return subject, emails[0], emails

    def complete(self, state: str, code: str, *, check_cancelled=None) -> Dict[str, Any]:
        with self._oauth_lock:
            pending = self._pending.pop(state, None)
            if (not pending or time.time() - pending["created_at"] > 600
                    or time.monotonic() >= pending["deadline"]):
                raise RemoteRuntimeError("OAuth state가 없거나 만료되었습니다.")
            self._inflight[state] = pending
        def checkpoint():
            if check_cancelled is not None:
                check_cancelled()
            if pending["cancelled"].is_set():
                from core.plugin import ToolCancelledError
                raise ToolCancelledError("OAuth 연결이 취소되었습니다.")
            if time.monotonic() >= pending["deadline"]:
                raise RemoteRuntimeError("OAuth 연결 시간이 만료되었습니다.")
        try:
            checkpoint()
            if (not isinstance(code, str) or not code or len(code) > 8192
                    or any(ord(c) < 33 or ord(c) == 127 for c in code)):
                raise RemoteRuntimeError("OAuth 인증 코드 형식이 올바르지 않습니다.")
            provider, spec = pending["provider"], self.SPECS[pending["provider"]]
            payload = {"grant_type": "authorization_code", "code": code,
                       "client_id": pending["client_id"], "redirect_uri": pending["redirect_uri"],
                       "code_verifier": pending["verifier"]}
            if pending["client_secret"]:
                payload["client_secret"] = pending["client_secret"]
            response = self.session.post(spec["token"], data=payload, timeout=15, allow_redirects=False)
            self._oauth_raise(response)
            token = self._token_payload(response.json())
            checkpoint()
            subject, email, emails = self._identity(provider, token["access_token"])
            checkpoint()
            expected = pending["expected_identity"]
            if expected and expected.casefold() not in {value.casefold() for value in emails}:
                raise RemoteRuntimeError("로그인한 계정이 연결하려는 계정과 다릅니다. 저장하지 않았습니다.")
            with self._oauth_lock:
                checkpoint()
                current_exists = (not isinstance(self.vault, SecureTokenVault)
                                  or (self.vault.directory / f"{self.vault._name(provider, pending['account'])}.dpapi").is_file())
                old = self.vault.load(provider, pending["account"]) if current_exists else None
                if old and old.get("remote_subject") and old["remote_subject"] != subject:
                    raise RemoteRuntimeError("기존 연결과 다른 계정입니다. 먼저 로컬 연결을 해제하세요.")
                token.update(remote_subject=subject, identity_email=email, identity_verified_at=time.time(),
                             requested_scopes=list(spec["scopes"]), oauth_client_id=pending["client_id"],
                             oauth_client_secret=pending["client_secret"])
                self.vault.save(provider, pending["account"], token)
            return self.status(provider, pending["account"])
        except RemoteRuntimeError:
            raise
        except Exception as exc:
            from core.plugin import ToolCancelledError
            if isinstance(exc, ToolCancelledError):
                raise
            raise RemoteRuntimeError("OAuth 연결을 완료하지 못했습니다. 저장소와 네트워크를 확인하세요.") from None
        finally:
            with self._oauth_lock:
                self._inflight.pop(state, None)

    def status(self, provider: str, account: str) -> Dict[str, Any]:
        if provider not in self.SPECS:
            raise RemoteRuntimeError("지원하지 않는 OAuth 제공자입니다.")
        token = self.vault.load(provider, account)
        expires = token.get("expires_at", 0) if token else 0
        valid_expiry = isinstance(expires, (int, float)) and math.isfinite(expires)
        return {"provider": provider, "account": account, "configured": bool(token),
                "authenticated": bool(token and token.get("remote_subject") and token.get("access_token")
                                      and valid_expiry and expires > time.time()),
                "expires_at": expires if valid_expiry and token else None,
                "identity_email": token.get("identity_email", "") if token else "",
                "identity_verified_at": token.get("identity_verified_at") if token else None,
                "refresh_available": bool(token and token.get("refresh_token")),
                "granted_scopes": token.get("granted_scopes", []) if token else [],
                "scopes_verified": bool(token and token.get("scopes_verified") is True)}

    def access_token(self, provider: str, account: str) -> str:
        token = self.vault.load(provider, account)
        if not token:
            raise RemoteRuntimeError(f"{provider}/{account} OAuth 인증이 필요합니다.")
        if token.get("expires_at", 0) <= time.time() + 60:
            refresh = token.get("refresh_token")
            if not refresh:
                raise RemoteRuntimeError("OAuth refresh token이 없습니다. 다시 인증하세요.")
            spec = self.SPECS[provider]
            payload = {"grant_type": "refresh_token", "refresh_token": refresh,
                       "client_id": token.get("oauth_client_id") or os.getenv(spec["client_id"], "")}
            secret = token.get("oauth_client_secret") or os.getenv(spec["client_secret"], "")
            if secret:
                payload["client_secret"] = secret
            response = self.session.post(spec["token"], data=payload, timeout=15, allow_redirects=False)
            self._oauth_raise(response)
            updated = self._token_payload(response.json())
            updated.setdefault("refresh_token", refresh)
            if "scope" not in updated:
                updated["granted_scopes"] = token.get("granted_scopes", [])
                updated["scopes_verified"] = token.get("scopes_verified", False)
            token.update(updated)
            self.vault.save(provider, account, token)
        return str(token["access_token"])

    @staticmethod
    def _oauth_raise(response) -> None:
        if not 200 <= response.status_code < 300:
            # OAuth error bodies can echo authorization codes/client secrets.
            raise RemoteRuntimeError(f"OAuth 요청을 완료하지 못했습니다 (HTTP {response.status_code}).")

    @staticmethod
    def _raise(response) -> None:
        if not 200 <= response.status_code < 300:
            raise RemoteRuntimeError(f"원격 API 오류 {response.status_code}: {response.text[:500]}")


@dataclass
class PendingRemoteAction:
    action_id: str
    provider: str
    account: str
    operation: str
    payload: Dict[str, Any]
    status: str = "draft"
    remote_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""


class RemoteActionStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or Path(Config.DB_PATH).with_name("remote_actions.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS remote_actions(
                action_id TEXT PRIMARY KEY, provider TEXT, account TEXT, operation TEXT,
                payload TEXT, status TEXT, remote_id TEXT, created_at REAL, updated_at REAL,
                last_error TEXT NOT NULL DEFAULT '')""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(remote_actions)")}
            if "last_error" not in columns:
                conn.execute("ALTER TABLE remote_actions ADD COLUMN last_error TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _session(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(self, provider: str, account: str, operation: str, payload: Dict[str, Any]) -> PendingRemoteAction:
        now, action_id = time.time(), uuid.uuid4().hex
        with self._session() as conn:
            conn.execute("""INSERT INTO remote_actions(
                action_id,provider,account,operation,payload,status,remote_id,
                created_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         (action_id, provider, account, operation, json.dumps(payload, ensure_ascii=False),
                          "draft", "", now, now, ""))
        return self.get(action_id)

    def get(self, action_id: str) -> Optional[PendingRemoteAction]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM remote_actions WHERE action_id=?", (action_id,)).fetchone()
        if not row:
            return None
        return PendingRemoteAction(row["action_id"], row["provider"], row["account"], row["operation"],
                                   json.loads(row["payload"]), row["status"], row["remote_id"],
                                   row["created_at"], row["updated_at"], row["last_error"])

    def claim_for_apply(self, action_id: str) -> PendingRemoteAction:
        """Atomically reserve a draft before any remote side effect is attempted."""
        with self._session() as conn:
            cursor = conn.execute(
                "UPDATE remote_actions SET status='applying',last_error='',updated_at=? "
                "WHERE action_id=? AND status='draft'",
                (time.time(), action_id),
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT status FROM remote_actions WHERE action_id=?", (action_id,)
                ).fetchone()
                status = current["status"] if current else "missing"
                raise RemoteRuntimeError(
                    f"원격 작업을 적용할 수 없습니다. 현재 상태: {status}. "
                    "applying/uncertain 작업은 중복 전송 방지를 위해 자동 재시도하지 않습니다."
                )
        claimed = self.get(action_id)
        if claimed is None:  # pragma: no cover - defensive after a successful guarded update
            raise RemoteRuntimeError("적용 예약 직후 원격 작업 원장을 다시 읽지 못했습니다.")
        return claimed

    def release_rejected(self, action_id: str, error: str) -> PendingRemoteAction:
        """Return to draft only when the provider proved no mutation was attempted."""
        with self._session() as conn:
            cursor = conn.execute(
                "UPDATE remote_actions SET status='draft',last_error=?,updated_at=? "
                "WHERE action_id=? AND status='applying'",
                (str(error), time.time(), action_id),
            )
            if cursor.rowcount != 1:
                raise RemoteRuntimeError("적용 전 거절 상태를 원장에 기록하지 못했습니다.")
        return self.get(action_id)

    def mark_uncertain(self, action_id: str, error: str, remote_id: str = "") -> PendingRemoteAction:
        """Persist an ambiguous outcome so a retry cannot duplicate the side effect."""
        with self._session() as conn:
            cursor = conn.execute(
                "UPDATE remote_actions SET status='uncertain',remote_id=?,last_error=?,updated_at=? "
                "WHERE action_id=? AND status='applying'",
                (str(remote_id or ""), str(error), time.time(), action_id),
            )
            if cursor.rowcount != 1:
                raise RemoteRuntimeError("불확실한 원격 작업 상태를 원장에 기록하지 못했습니다.")
        return self.get(action_id)

    def mark_applied(self, action_id: str, remote_id: str) -> Optional[PendingRemoteAction]:
        with self._session() as conn:
            cursor = conn.execute("UPDATE remote_actions SET status='applied',remote_id=?,last_error='',updated_at=? "
                                  "WHERE action_id=? AND status='applying'",
                                  (remote_id, time.time(), action_id))
            if cursor.rowcount != 1:
                raise RemoteRuntimeError("초안이 없거나 이미 적용된 작업입니다.")
        return self.get(action_id)


class RemoteCatalogStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or Path(Config.DB_PATH).with_name("remote_catalog.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS remote_catalog(
                provider TEXT, account TEXT, item_id TEXT, payload TEXT, synced_at REAL,
                PRIMARY KEY(provider, account, item_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS remote_catalog_revisions(
                provider TEXT, account TEXT, revision INTEGER NOT NULL,
                PRIMARY KEY(provider, account))""")

    @contextmanager
    def _session(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def replace(self, provider: str, account: str, items: List[Dict[str, Any]]) -> List[str]:
        # Kept for explicit callers with an already complete local snapshot.
        # The cloud plugin must use apply_sync(), never this unchecked boundary.
        return self._write(provider, account, items, replace=True)

    @staticmethod
    def _serialized_items(items: List[Dict[str, Any]]) -> Dict[str, str]:
        rows = {}
        for item in items:
            if not isinstance(item, dict):
                raise RemoteRuntimeError("카탈로그 항목은 객체여야 합니다.")
            item_id = item.get("id") or item.get("remote_id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise RemoteRuntimeError("동기화 항목에 원격 ID가 없습니다.")
            rows[item_id] = json.dumps(item, ensure_ascii=False, allow_nan=False)
        return rows

    def revision(self, provider: str, account: str) -> int:
        with self._session() as conn:
            row = conn.execute(
                "SELECT revision FROM remote_catalog_revisions WHERE provider=? AND account=?",
                (provider, account),
            ).fetchone()
        return int(row[0]) if row else 0

    def apply_sync(self, provider: str, account: str, result: CatalogSyncResult, *,
                   expected_revision: int,
                   check_cancelled: Optional[Callable[[], None]] = None) -> List[str]:
        if (not isinstance(result, CatalogSyncResult) or result.provider != provider
                or result.account != account):
            raise RemoteRuntimeError("조회 범위와 카탈로그 저장 대상이 일치하지 않습니다.")
        if result.cancelled:
            return []
        return self._write(provider, account, result, replace=result.full_snapshot,
                           expected_revision=expected_revision, check_cancelled=check_cancelled)

    def _write(self, provider: str, account: str, items: List[Dict[str, Any]], *, replace: bool,
               expected_revision: Optional[int] = None,
               check_cancelled: Optional[Callable[[], None]] = None) -> List[str]:
        # Validate/serialize every item before DELETE. A malformed tail cannot
        # destroy the previous complete catalog, even with an empty new page.
        rows = self._serialized_items(items)
        checkpoint = check_cancelled or (lambda: None)
        checkpoint()
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM remote_catalog_revisions WHERE provider=? AND account=?",
                (provider, account),
            ).fetchone()
            revision = int(row[0]) if row else 0
            if expected_revision is not None and expected_revision != revision:
                raise RemoteRuntimeError("조회 중 카탈로그가 변경되어 오래된 결과를 저장하지 않았습니다.")
            checkpoint()
            if replace:
                conn.execute("DELETE FROM remote_catalog WHERE provider=? AND account=?", (provider, account))
            conn.executemany(
                "INSERT INTO remote_catalog VALUES(?,?,?,?,?) "
                "ON CONFLICT(provider,account,item_id) DO UPDATE SET "
                "payload=excluded.payload,synced_at=excluded.synced_at",
                [(provider, account, item_id, payload, time.time()) for item_id, payload in rows.items()],
            )
            if rows or replace:
                conn.execute(
                    "INSERT INTO remote_catalog_revisions VALUES(?,?,?) "
                    "ON CONFLICT(provider,account) DO UPDATE SET revision=excluded.revision",
                    (provider, account, revision + 1),
                )
            # Cancellation during local publication rolls back this transaction.
            checkpoint()
        return list(rows)

    def list(self, provider: str, account: str) -> List[Dict[str, Any]]:
        with self._session() as conn:
            rows = conn.execute("SELECT payload FROM remote_catalog WHERE provider=? AND account=?",
                                (provider, account)).fetchall()
        return [json.loads(row["payload"]) for row in rows]


class ProviderApi:
    def __init__(self, oauth: OAuthCoordinator, session=None):
        self.oauth, self.session = oauth, session or requests.Session()

    def _request(self, provider: str, account: str, method: str, url: str, **kwargs):
        headers = {**kwargs.pop("headers", {}),
                   "Authorization": f"Bearer {self.oauth.access_token(provider, account)}"}
        response = self.session.request(method, url, headers=headers, timeout=30, **kwargs)
        OAuthCoordinator._raise(response)
        return response

    @staticmethod
    def _require_payload(data: Dict[str, Any], *keys: str) -> None:
        if not isinstance(data, dict):
            raise RemoteApplyRejected("원격 작업 payload는 객체여야 합니다.")
        missing = [key for key in keys if key not in data or data[key] in (None, "")]
        if missing:
            raise RemoteApplyRejected(f"원격 작업 필수 필드가 없습니다: {', '.join(missing)}")

    @staticmethod
    def _require_provider(action: PendingRemoteAction, expected: str) -> None:
        if action.provider.casefold() != expected.casefold():
            raise RemoteApplyRejected(
                f"{action.operation} 작업의 provider는 {expected}여야 합니다: {action.provider}"
            )

    def _preflight_oauth(self, provider: str, account: str) -> None:
        try:
            self.oauth.access_token(provider, account)
        except Exception as exc:
            raise RemoteApplyRejected(
                f"{provider}/{account} 인증을 원격 변경 전에 확인하지 못했습니다: {exc}"
            ) from exc

    @staticmethod
    def _canonical_text(value: Any, *, strip_html: bool = False) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        if strip_html:
            text = re.sub(r"<[^>]+>", " ", html.unescape(text))
        return " ".join(text.split())

    @classmethod
    def _assert_subset(cls, expected: Any, actual: Any, path: str) -> None:
        """Require every user-controlled expected field in the provider read-back."""
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                raise RemoteRuntimeError(f"{path} 응답 형식이 요청과 다릅니다.")
            for key, value in expected.items():
                if key not in actual:
                    raise RemoteRuntimeError(f"{path}.{key}가 원격 재조회 결과에 없습니다.")
                cls._assert_subset(value, actual[key], f"{path}.{key}")
            return
        if isinstance(expected, list):
            if not isinstance(actual, list):
                raise RemoteRuntimeError(f"{path} 응답 형식이 요청과 다릅니다.")
            unmatched = list(actual)
            for index, expected_item in enumerate(expected):
                for actual_index, actual_item in enumerate(unmatched):
                    try:
                        cls._assert_subset(expected_item, actual_item, f"{path}[{index}]")
                    except RemoteRuntimeError:
                        continue
                    unmatched.pop(actual_index)
                    break
                else:
                    raise RemoteRuntimeError(f"{path}[{index}]가 원격 재조회 결과와 일치하지 않습니다.")
            return
        if cls._canonical_text(expected) != cls._canonical_text(actual):
            if path.casefold().endswith((".address", ".email", ".contenttype")):
                if cls._canonical_text(expected).casefold() == cls._canonical_text(actual).casefold():
                    return
            raise RemoteRuntimeError(f"{path}가 원격 재조회 결과와 일치하지 않습니다.")

    @staticmethod
    def _decode_gmail_raw(raw: Any) -> bytes:
        if not isinstance(raw, str) or not raw.strip():
            raise RemoteRuntimeError("Gmail raw 메시지가 비어 있거나 문자열이 아닙니다.")
        try:
            encoded = raw.strip().encode("ascii")
            return base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        except Exception as exc:
            raise RemoteRuntimeError("Gmail raw 메시지가 유효한 base64url이 아닙니다.") from exc

    @classmethod
    def _gmail_signature(cls, raw: Any) -> Dict[str, Any]:
        message: Message = BytesParser(policy=policy.default).parsebytes(cls._decode_gmail_raw(raw))
        recipients: Dict[str, List[str]] = {}
        for header in ("to", "cc", "bcc"):
            addresses = sorted(
                address.casefold() for _, address in getaddresses(message.get_all(header, [])) if address
            )
            if addresses:
                recipients[header] = addresses
        part = message.get_body(preferencelist=("plain", "html")) if message.is_multipart() else message
        try:
            body = part.get_content() if part is not None else ""
        except (KeyError, LookupError, UnicodeError):
            body = part.get_payload(decode=True) if part is not None else b""
            if isinstance(body, bytes):
                body = body.decode(part.get_content_charset() or "utf-8", errors="replace")
        return {
            "subject": cls._canonical_text(message.get("subject", "")),
            "recipients": recipients,
            "body": cls._canonical_text(body, strip_html=(part is not None and part.get_content_subtype() == "html")),
        }

    @classmethod
    def _verify_gmail(cls, expected_raw: Any, actual: Dict[str, Any], remote_id: str) -> Dict[str, Any]:
        if str(actual.get("id") or "") != remote_id:
            raise RemoteRuntimeError("Gmail 재조회 ID가 전송 결과 ID와 다릅니다.")
        expected, observed = cls._gmail_signature(expected_raw), cls._gmail_signature(actual.get("raw"))
        if expected != observed:
            changed = [key for key in expected if expected.get(key) != observed.get(key)]
            raise RemoteRuntimeError(
                "Gmail 재조회에서 요청한 수신자/제목/본문이 일치하지 않습니다: " + ", ".join(changed)
            )
        return {"provider": "gmail", "remote_id": remote_id,
                "verified_fields": ["id", "recipients", "subject", "body"]}

    @classmethod
    def _verify_google_event(cls, expected: Dict[str, Any], actual: Dict[str, Any],
                             remote_id: str, calendar_id: str) -> Dict[str, Any]:
        if str(actual.get("id") or "") != remote_id:
            raise RemoteRuntimeError("Google Calendar 재조회 ID가 생성 결과 ID와 다릅니다.")
        fields = [key for key in ("summary", "description", "location", "start", "end", "attendees")
                  if key in expected]
        cls._assert_subset({key: expected[key] for key in fields}, actual, "event")
        return {"provider": "google_calendar", "remote_id": remote_id,
                "calendar_id": calendar_id, "verified_fields": ["id", *fields]}

    @classmethod
    def _verify_outlook_message(cls, expected: Dict[str, Any], actual: Dict[str, Any],
                                remote_id: str) -> Dict[str, Any]:
        if str(actual.get("id") or "") != remote_id:
            raise RemoteRuntimeError("Outlook 재조회 ID가 전송 결과 ID와 다릅니다.")
        fields = [key for key in ("subject", "body", "toRecipients", "ccRecipients", "bccRecipients")
                  if key in expected]
        cls._assert_subset({key: expected[key] for key in fields}, actual, "message")
        return {"provider": "outlook", "remote_id": remote_id,
                "verified_fields": ["id", *fields]}

    @classmethod
    def _verify_outlook_event(cls, expected: Dict[str, Any], actual: Dict[str, Any],
                              remote_id: str) -> Dict[str, Any]:
        if str(actual.get("id") or "") != remote_id:
            raise RemoteRuntimeError("Outlook Calendar 재조회 ID가 생성 결과 ID와 다릅니다.")
        fields = [key for key in ("subject", "body", "start", "end", "location", "attendees")
                  if key in expected]
        cls._assert_subset({key: expected[key] for key in fields}, actual, "event")
        return {"provider": "outlook_calendar", "remote_id": remote_id,
                "verified_fields": ["id", *fields]}

    @staticmethod
    def _uncertain(operation: str, stage: str, exc: Exception, remote_id: str = ""):
        if isinstance(exc, RemoteApplyUncertain):
            raise exc
        raise RemoteApplyUncertain(
            f"{operation} 작업은 원격 변경을 시도했지만 {stage} 단계에서 결과를 검증하지 못했습니다: {exc}",
            remote_id=remote_id, stage=stage,
        ) from exc

    def apply(self, action: PendingRemoteAction) -> str:
        account, operation, data = action.account, action.operation, action.payload
        if operation == "gmail_send":
            self._require_provider(action, "google")
            self._require_payload(data, "raw")
            try:
                self._gmail_signature(data["raw"])
            except RemoteRuntimeError as exc:
                raise RemoteApplyRejected(str(exc)) from exc
            self._preflight_oauth("google", account)
            remote_id, stage = "", "초안 생성"
            try:
                draft = self._request(
                    "google", account, "POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                    json={"message": {"raw": data["raw"]}},
                ).json()
                draft_id = str(draft.get("id") or "")
                if not draft_id:
                    raise RemoteRuntimeError("Gmail 초안 생성 응답에 ID가 없습니다.")
                stage = "전송"
                sent = self._request(
                    "google", account, "POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send",
                    json={"id": draft_id},
                ).json()
                remote_id = str(sent.get("id") or "")
                if not remote_id:
                    raise RemoteRuntimeError("Gmail 전송 응답에 메시지 ID가 없습니다.")
                stage = "재조회 검증"
                observed = self._request(
                    "google", account, "GET",
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{remote_id}",
                    params={"format": "raw"},
                ).json()
                verification = self._verify_gmail(data["raw"], observed, remote_id)
                return RemoteApplyReceipt(remote_id, verification)
            except Exception as exc:
                self._uncertain(operation, stage, exc, remote_id)
        if operation == "google_calendar_create":
            self._require_provider(action, "google")
            self._require_payload(data, "event")
            if not isinstance(data["event"], dict):
                raise RemoteApplyRejected("Google Calendar event는 객체여야 합니다.")
            self._preflight_oauth("google", account)
            calendar_id = data.get("calendar_id", "primary")
            remote_id, stage = "", "일정 생성"
            try:
                event = self._request(
                    "google", account, "POST",
                    f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                    json=data["event"],
                ).json()
                remote_id = str(event.get("id") or "")
                if not remote_id:
                    raise RemoteRuntimeError("Google Calendar 생성 응답에 ID가 없습니다.")
                stage = "재조회 검증"
                observed = self._request(
                    "google", account, "GET",
                    f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{remote_id}",
                ).json()
                verification = self._verify_google_event(
                    data["event"], observed, remote_id, str(calendar_id)
                )
                return RemoteApplyReceipt(remote_id, verification)
            except Exception as exc:
                self._uncertain(operation, stage, exc, remote_id)
        graph = "https://graph.microsoft.com/v1.0"
        immutable = {"Prefer": 'IdType="ImmutableId"'}
        if operation == "outlook_send":
            self._require_provider(action, "microsoft")
            self._require_payload(data, "message")
            if not isinstance(data["message"], dict):
                raise RemoteApplyRejected("Outlook message는 객체여야 합니다.")
            self._preflight_oauth("microsoft", account)
            remote_id, stage = "", "초안 생성"
            try:
                draft = self._request(
                    "microsoft", account, "POST", graph + "/me/messages",
                    json=data["message"], headers=immutable,
                ).json()
                remote_id = str(draft.get("id") or "")
                if not remote_id:
                    raise RemoteRuntimeError("Outlook 초안 생성 응답에 ID가 없습니다.")
                stage = "전송"
                self._request(
                    "microsoft", account, "POST", graph + f"/me/messages/{remote_id}/send",
                    headers=immutable,
                )
                stage = "재조회 검증"
                observed = self._request(
                    "microsoft", account, "GET", graph + f"/me/messages/{remote_id}",
                    headers=immutable,
                    params={"$select": "id,subject,body,toRecipients,ccRecipients,bccRecipients"},
                ).json()
                verification = self._verify_outlook_message(
                    data["message"], observed, remote_id
                )
                return RemoteApplyReceipt(remote_id, verification)
            except Exception as exc:
                self._uncertain(operation, stage, exc, remote_id)
        if operation == "outlook_calendar_create":
            self._require_provider(action, "microsoft")
            self._require_payload(data, "event")
            if not isinstance(data["event"], dict):
                raise RemoteApplyRejected("Outlook Calendar event는 객체여야 합니다.")
            self._preflight_oauth("microsoft", account)
            remote_id, stage = "", "일정 생성"
            try:
                event = self._request(
                    "microsoft", account, "POST", graph + "/me/events",
                    json=data["event"], headers=immutable,
                ).json()
                remote_id = str(event.get("id") or "")
                if not remote_id:
                    raise RemoteRuntimeError("Outlook Calendar 생성 응답에 ID가 없습니다.")
                stage = "재조회 검증"
                observed = self._request(
                    "microsoft", account, "GET", graph + f"/me/events/{remote_id}",
                    headers=immutable,
                ).json()
                verification = self._verify_outlook_event(data["event"], observed, remote_id)
                return RemoteApplyReceipt(remote_id, verification)
            except Exception as exc:
                self._uncertain(operation, stage, exc, remote_id)
        if operation == "slack_send":
            self._require_provider(action, "slack")
            self._require_payload(data, "channel", "text")
            token = os.getenv("SLACK_BOT_TOKEN", "")
            if not token:
                raise RemoteApplyRejected("SLACK_BOT_TOKEN 설정이 필요합니다.")
            headers = {"Authorization": f"Bearer {token}"}
            remote_id, stage = "", "메시지 전송"
            try:
                response = self.session.post(
                    "https://slack.com/api/chat.postMessage", headers=headers,
                    json={"channel": data["channel"], "text": data["text"]}, timeout=30,
                )
                OAuthCoordinator._raise(response)
                sent = response.json()
                if not sent.get("ok"):
                    raise RemoteRuntimeError(str(sent))
                remote_id = str(sent.get("ts") or "")
                if not remote_id:
                    raise RemoteRuntimeError("Slack 전송 응답에 Message ID(ts)가 없습니다.")
                stage = "재조회 검증"
                response = self.session.get(
                    "https://slack.com/api/conversations.history", headers=headers,
                    params={"channel": data["channel"], "latest": remote_id,
                            "inclusive": True, "limit": 1}, timeout=30,
                )
                OAuthCoordinator._raise(response)
                observed = response.json()
                messages = [message for message in observed.get("messages", [])
                            if str(message.get("ts") or "") == remote_id]
                if not observed.get("ok") or len(messages) != 1:
                    raise RemoteRuntimeError("Slack Message ID(ts) 사후 검증에 실패했습니다.")
                if self._canonical_text(messages[0].get("text")) != self._canonical_text(data["text"]):
                    raise RemoteRuntimeError("Slack 재조회 본문이 전송 요청과 다릅니다.")
                verification = {"provider": "slack", "remote_id": remote_id,
                                "channel": str(data["channel"]),
                                "verified_fields": ["ts", "channel", "text"]}
                return RemoteApplyReceipt(remote_id, verification)
            except Exception as exc:
                self._uncertain(operation, stage, exc, remote_id)
        if operation == "teams_send":
            self._require_provider(action, "microsoft")
            self._require_payload(data, "team_id", "channel_id", "text")
            self._preflight_oauth("microsoft", account)
            url = graph + f"/teams/{data['team_id']}/channels/{data['channel_id']}/messages"
            remote_id, stage = "", "메시지 전송"
            try:
                message = self._request(
                    "microsoft", account, "POST", url,
                    json={"body": {"content": data["text"]}},
                ).json()
                remote_id = str(message.get("id") or "")
                if not remote_id:
                    raise RemoteRuntimeError("Teams 전송 응답에 Message ID가 없습니다.")
                stage = "재조회 검증"
                observed = self._request(
                    "microsoft", account, "GET", url + f"/{remote_id}"
                ).json()
                if str(observed.get("id") or "") != remote_id:
                    raise RemoteRuntimeError("Teams 재조회 ID가 전송 결과 ID와 다릅니다.")
                observed_body = observed.get("body", {}).get("content", "")
                if self._canonical_text(observed_body, strip_html=True) != self._canonical_text(data["text"]):
                    raise RemoteRuntimeError("Teams 재조회 본문이 전송 요청과 다릅니다.")
                verification = {"provider": "teams", "remote_id": remote_id,
                                "team_id": str(data["team_id"]),
                                "channel_id": str(data["channel_id"]),
                                "verified_fields": ["id", "team_id", "channel_id", "body"]}
                return RemoteApplyReceipt(remote_id, verification)
            except Exception as exc:
                self._uncertain(operation, stage, exc, remote_id)
        raise RemoteApplyRejected(f"지원하지 않는 원격 작업: {operation}")

    CATALOG_URLS = {
        "google_drive": "https://www.googleapis.com/drive/v3/files",
        "onedrive": "https://graph.microsoft.com/v1.0/me/drive/root/delta",
        "notion": "https://api.notion.com/v1/search",
    }

    @staticmethod
    def _catalog_token(value: Any) -> str:
        if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 16384
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise RemoteRuntimeError("카탈로그 페이지 커서 형식이 올바르지 않습니다.")
        return value

    @classmethod
    def _catalog_next_url(cls, value: Any) -> str:
        value = cls._catalog_token(value)
        try:
            parsed = urlsplit(value)
            # Graph documents both root/delta?$skiptoken=... and the OData
            # /me/drive/delta(token=...) form. Keep both on this delegated drive.
            base_path = re.fullmatch(r"/v1\.0/me/drive/(?:root/)?delta", parsed.path)
            token_path = re.fullmatch(
                r"/v1\.0/me/drive/(?:root/)?delta\(token=(?:'[A-Za-z0-9._~%+=-]+'|[A-Za-z0-9._~%+=-]+)\)",
                parsed.path,
            )
            allowed = (value == value.strip() and "\\" not in value
                       and parsed.scheme == "https" and parsed.hostname == "graph.microsoft.com"
                       and parsed.port in {None, 443} and not parsed.username and not parsed.password
                       and ((base_path and bool(parsed.query)) or token_path) and not parsed.fragment)
        except ValueError:
            allowed = False
        if not allowed:
            # Never include the untrusted URL or bearer credentials in errors.
            raise RemoteRuntimeError("허용되지 않은 OneDrive 페이지 URL입니다.")
        return value

    @classmethod
    def _catalog_cursor(cls, provider: str, account: str, query: str, page: str) -> str:
        payload = {"v": 1, "provider": provider,
                   "account_hash": hashlib.sha256(account.encode("utf-8")).hexdigest(),
                   "query": query, "page": cls._catalog_token(page)}
        return base64.urlsafe_b64encode(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def _read_catalog_cursor(cls, cursor: Any, provider: str, account: str, query: str) -> str:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 65536:
            raise RemoteRuntimeError("카탈로그 재개 커서 형식이 올바르지 않습니다.")
        try:
            payload = json.loads(base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
            ).decode("utf-8"))
            if (not isinstance(payload, dict)
                    or set(payload) != {"v", "provider", "account_hash", "query", "page"}
                    or type(payload["v"]) is not int or payload["v"] != 1
                    or payload["provider"] != provider or payload["query"] != query
                    or payload["account_hash"] != hashlib.sha256(account.encode("utf-8")).hexdigest()):
                raise ValueError("scope mismatch")
            page = cls._catalog_token(payload["page"])
        except (ValueError, TypeError, UnicodeError, KeyError) as exc:
            raise RemoteRuntimeError("재개 커서가 손상되었거나 제공자·계정·조회 범위가 다릅니다.") from exc
        return cls._catalog_next_url(page) if provider == "onedrive" else page

    def _catalog_page(self, provider: str, account: str, *, page: str, query: str,
                      limit: int, timeout: float) -> Dict[str, Any]:
        # Redirects are disabled for *all* pages. Checking only the first URL
        # would still allow a nextLink/302 to leak a Notion or OAuth bearer token.
        url = self.CATALOG_URLS[provider]
        kwargs: Dict[str, Any] = {"timeout": timeout, "allow_redirects": False}
        if provider == "google_drive":
            kwargs["params"] = {
                "pageSize": limit,
                "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,modifiedTime,md5Checksum,trashed)",
                "q": query or "trashed = false",
            }
            if page:
                kwargs["params"]["pageToken"] = page
        elif provider == "onedrive":
            if page:
                url = self._catalog_next_url(page)
            else:
                kwargs["params"] = {"$top": limit}
        else:
            kwargs["json"] = {"page_size": limit}
            if query:
                kwargs["json"]["query"] = query
            if page:
                kwargs["json"]["start_cursor"] = page
        if provider == "notion":
            token = os.getenv("NOTION_API_TOKEN", "")
            if not token:
                raise RemoteRuntimeError("NOTION_API_TOKEN 설정이 필요합니다.")
            kwargs["headers"] = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
            response = self.session.post(url, **kwargs)
        else:
            oauth_provider = "google" if provider == "google_drive" else "microsoft"
            kwargs["headers"] = {"Authorization": f"Bearer {self.oauth.access_token(oauth_provider, account)}"}
            response = self.session.request("GET", url, **kwargs)
        try:
            if not 200 <= response.status_code < 300:
                raise RemoteRuntimeError(f"카탈로그 페이지 HTTP 상태: {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise RemoteRuntimeError("카탈로그 페이지가 객체가 아닙니다.")
            return payload
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    @classmethod
    def _catalog_next_page(cls, provider: str, payload: Dict[str, Any]) -> str:
        if provider == "google_drive":
            if "incompleteSearch" in payload and type(payload["incompleteSearch"]) is not bool:
                raise RemoteRuntimeError("Drive incompleteSearch 형식이 올바르지 않습니다.")
            token = payload.get("nextPageToken")
            return "" if token is None else cls._catalog_token(token)
        if provider == "notion":
            if type(payload.get("has_more")) is not bool:
                raise RemoteRuntimeError("Notion has_more 상태가 없습니다.")
            token = payload.get("next_cursor")
            if payload["has_more"]:
                return cls._catalog_token(token)
            if token is not None:
                raise RemoteRuntimeError("Notion 마지막 페이지와 커서가 모순됩니다.")
            return ""
        next_url, delta_url = payload.get("@odata.nextLink"), payload.get("@odata.deltaLink")
        if next_url is not None and delta_url is not None:
            raise RemoteRuntimeError("OneDrive 페이지와 종료 링크가 동시에 존재합니다.")
        if next_url is not None:
            return cls._catalog_next_url(next_url)
        # A bare value array is not proof that a delta enumeration finished.
        cls._catalog_next_url(delta_url)
        return ""

    def sync(self, provider: str, account: str, options: Dict[str, Any], *,
             check_cancelled: Optional[Callable[[], None]] = None) -> CatalogSyncResult:
        """Bounded metadata enumeration, with explicit merge-only continuations.

        No body download/RAG or incremental delta replay is performed. Graph's
        deltaLink proves the end of a fresh enumeration; it is not followed as a
        new full snapshot. Cancellation is checked around each bounded HTTP call.
        """
        from core.plugin import ToolCancelledError
        from core.turn_context import check_turn_cancelled

        if provider not in self.CATALOG_URLS or not isinstance(account, str) or not account.strip():
            raise RemoteRuntimeError("지원하는 제공자와 비어 있지 않은 계정이 필요합니다.")
        allowed_options = {"provider", "account", "limit", "max_pages", "cursor", "query"}
        if not isinstance(options, dict) or set(options) - allowed_options:
            raise RemoteRuntimeError("지원하지 않는 카탈로그 조회 옵션입니다.")
        if options.get("provider", provider) != provider or options.get("account", account) != account:
            raise RemoteRuntimeError("카탈로그 조회 대상이 일치하지 않습니다.")
        limit, max_pages = options.get("limit", 50), options.get("max_pages", 5)
        if (type(limit) is not int or not 1 <= limit <= 100
                or type(max_pages) is not int or not 1 <= max_pages <= 10):
            raise RemoteRuntimeError("페이지 크기는 1~100, 페이지 예산은 1~10이어야 합니다.")
        query, cursor = options.get("query", ""), options.get("cursor", "")
        if (not isinstance(query, str) or len(query) > 4096 or query != query.strip()
                or (provider == "onedrive" and query)):
            raise RemoteRuntimeError("조회 필터는 Drive/Notion에서만 지원하며 앞뒤 공백을 허용하지 않습니다.")
        if not isinstance(cursor, str):
            raise RemoteRuntimeError("재개 커서는 문자열이어야 합니다.")
        page = self._read_catalog_cursor(cursor, provider, account, query) if cursor else ""
        result = CatalogSyncResult(provider, account, query=query, resumed=bool(cursor))
        result.next_cursor = cursor
        deadline = time.monotonic() + 60
        seen = set()
        items: Dict[str, Dict[str, Any]] = {}

        def checkpoint():
            check_turn_cancelled()
            if check_cancelled is not None:
                check_cancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("카탈로그 조회 시간 예산을 초과했습니다.")

        try:
            for _ in range(max_pages):
                checkpoint()
                if page in seen:
                    raise RemoteRuntimeError("카탈로그 커서가 반복되어 조회를 중단했습니다.")
                seen.add(page)
                payload = self._catalog_page(provider, account, page=page, query=query, limit=limit,
                                             timeout=max(0.1, min(15.0, deadline - time.monotonic())))
                checkpoint()
                field = {"google_drive": "files", "onedrive": "value", "notion": "results"}[provider]
                page_items = payload.get(field)
                if not isinstance(page_items, list) or len(page_items) > 1000:
                    raise RemoteRuntimeError("카탈로그 페이지 항목이 없거나 허용 크기를 초과했습니다.")
                # Reject the whole malformed page, not just its bad tail.
                RemoteCatalogStore._serialized_items(page_items)
                if provider == "onedrive" and any(
                        "deleted" in item and not isinstance(item["deleted"], dict)
                        for item in page_items):
                    raise RemoteRuntimeError("OneDrive 삭제 항목 형식이 올바르지 않습니다.")
                for item in page_items:
                    item_id = item.get("id") or item.get("remote_id")
                    if provider == "onedrive" and "deleted" in item:
                        items.pop(item_id, None)
                        result.deleted_ids.add(item_id)
                    else:
                        items[item_id] = item
                        result.deleted_ids.discard(item_id)
                result.pages_fetched += 1
                next_page = self._catalog_next_page(provider, payload)
                result.incomplete_search |= payload.get("incompleteSearch") is True
                if not next_page:
                    result.complete = True
                    result.next_cursor = ""
                    break
                if next_page in seen:
                    raise RemoteRuntimeError("카탈로그 커서가 반복되어 조회를 중단했습니다.")
                page = next_page
                result.next_cursor = self._catalog_cursor(provider, account, query, page)
            checkpoint()
        except ToolCancelledError:
            result.cancelled, result.error, result.complete = True, "cancelled", False
        except (TimeoutError, requests.Timeout):
            result.error, result.complete = "page_timeout", False
        except Exception as exc:
            # Do not expose provider error bodies, URLs or bearer secrets. The
            # navigation cursor encodes its query/page, is NOT encryption or an
            # authenticated credential, and still names the unconsumed page.
            result.error, result.complete = f"page_error:{type(exc).__name__}", False
        result.extend(items.values())
        return result

    def read_messages(self, provider: str, account: str, options: Dict[str, Any]) -> List[Dict[str, Any]]:
        if provider == "slack":
            token = os.getenv("SLACK_BOT_TOKEN", "")
            if not token:
                raise RemoteRuntimeError("SLACK_BOT_TOKEN 설정이 필요합니다.")
            response = self.session.get("https://slack.com/api/conversations.history",
                                        headers={"Authorization": f"Bearer {token}"},
                                        params={"channel": options["channel"], "limit": options.get("limit", 50)},
                                        timeout=30).json()
            if not response.get("ok"):
                raise RemoteRuntimeError(str(response))
            return response.get("messages", [])
        if provider == "teams":
            url = ("https://graph.microsoft.com/v1.0/teams/"
                   f"{options['team_id']}/channels/{options['channel_id']}/messages")
            return self._request("microsoft", account, "GET", url,
                                 params={"$top": options.get("limit", 50)}).json().get("value", [])
        raise RemoteRuntimeError(f"지원하지 않는 메시지 Provider: {provider}")

    def read_calendar(self, provider: str, account: str, start: str, end: str,
                      *, limit: int = 50) -> List[Dict[str, Any]]:
        """Read a real calendar range through the provider's official API."""
        bounded_limit = max(1, min(int(limit), 100))
        if provider == "google":
            payload = self._request(
                "google", account, "GET",
                "https://www.googleapis.com/calendar/v3/calendars/primary/events",
                params={"timeMin": start, "timeMax": end, "singleEvents": "true",
                        "orderBy": "startTime", "maxResults": bounded_limit},
            ).json()
            return list(payload.get("items", []))
        if provider == "microsoft":
            payload = self._request(
                "microsoft", account, "GET",
                "https://graph.microsoft.com/v1.0/me/calendarView",
                params={"startDateTime": start, "endDateTime": end,
                        "$top": bounded_limit,
                        "$select": "id,subject,start,end,location,webLink"},
                headers={"Prefer": 'outlook.timezone="Asia/Seoul"'},
            ).json()
            return list(payload.get("value", []))
        raise RemoteRuntimeError(f"지원하지 않는 캘린더 Provider: {provider}")
