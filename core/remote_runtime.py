"""OAuth, encrypted credentials, approval drafts and verified remote adapters."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

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


class SecureTokenVault:
    """User-bound Windows DPAPI token storage; OAuth tokens are never stored as plain text."""

    def __init__(self, directory: Optional[str] = None):
        self.directory = Path(directory or Path(Config.DB_PATH).with_name("oauth_tokens"))
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _name(provider: str, account: str) -> str:
        raw = f"{provider.casefold()}_{account.casefold()}"
        return "".join(c if c.isalnum() or c in "_-" else "_" for c in raw)[:100]

    def save(self, provider: str, account: str, token: Dict[str, Any]) -> None:
        import win32crypt
        data = json.dumps(token, ensure_ascii=False).encode("utf-8")
        protected = win32crypt.CryptProtectData(data, "Jarvis OAuth", None, None, None, 0)
        encrypted = protected[1] if isinstance(protected, tuple) else protected
        path = self.directory / f"{self._name(provider, account)}.dpapi"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        os.replace(temporary, path)

    def load(self, provider: str, account: str) -> Optional[Dict[str, Any]]:
        import win32crypt
        path = self.directory / f"{self._name(provider, account)}.dpapi"
        if not path.is_file():
            return None
        unprotected = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)
        data = unprotected[1] if isinstance(unprotected, tuple) else unprotected
        return json.loads(data.decode("utf-8"))

    def delete(self, provider: str, account: str) -> bool:
        path = self.directory / f"{self._name(provider, account)}.dpapi"
        if not path.exists():
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
            "scopes": ["openid", "profile", "offline_access", "Mail.ReadWrite", "Mail.Send",
                       "Calendars.ReadWrite", "Files.Read", "ChannelMessage.Read.All",
                       "ChannelMessage.Send"],
        },
    }

    def __init__(self, vault: Optional[SecureTokenVault] = None, session=None):
        self.vault = vault or SecureTokenVault()
        self.session = session or requests.Session()
        self._pending: Dict[str, Dict[str, Any]] = {}

    def begin(self, provider: str, account: str, redirect_uri: str) -> Dict[str, str]:
        if provider not in self.SPECS:
            raise RemoteRuntimeError(f"지원하지 않는 OAuth Provider: {provider}")
        spec = self.SPECS[provider]
        client_id = os.getenv(spec["client_id"], "").strip()
        if not client_id:
            raise RemoteRuntimeError(f"{spec['client_id']} 설정이 필요합니다.")
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(24)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self._pending[state] = {"provider": provider, "account": account,
                                "redirect_uri": redirect_uri, "verifier": verifier,
                                "created_at": time.time()}
        params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "scope": " ".join(spec["scopes"]), "state": state,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        if provider == "google":
            params.update({"access_type": "offline", "prompt": "consent"})
        return {"authorization_url": spec["authorize"] + "?" + urlencode(params), "state": state}

    def complete(self, state: str, code: str) -> Dict[str, Any]:
        pending = self._pending.pop(state, None)
        if not pending or time.time() - pending["created_at"] > 600:
            raise RemoteRuntimeError("OAuth state가 없거나 만료되었습니다.")
        provider, spec = pending["provider"], self.SPECS[pending["provider"]]
        payload = {"grant_type": "authorization_code", "code": code,
                   "client_id": os.getenv(spec["client_id"], ""),
                   "redirect_uri": pending["redirect_uri"], "code_verifier": pending["verifier"]}
        if os.getenv(spec["client_secret"]):
            payload["client_secret"] = os.environ[spec["client_secret"]]
        response = self.session.post(spec["token"], data=payload, timeout=30)
        self._raise(response)
        token = response.json()
        token["expires_at"] = time.time() + float(token.get("expires_in", 3600))
        self.vault.save(provider, pending["account"], token)
        return {"provider": provider, "account": pending["account"], "expires_at": token["expires_at"]}

    def status(self, provider: str, account: str) -> Dict[str, Any]:
        token = self.vault.load(provider, account)
        return {"provider": provider, "account": account, "authenticated": bool(token),
                "expires_at": token.get("expires_at") if token else None}

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
                       "client_id": os.getenv(spec["client_id"], "")}
            if os.getenv(spec["client_secret"]):
                payload["client_secret"] = os.environ[spec["client_secret"]]
            response = self.session.post(spec["token"], data=payload, timeout=30)
            self._raise(response)
            updated = response.json()
            updated.setdefault("refresh_token", refresh)
            updated["expires_at"] = time.time() + float(updated.get("expires_in", 3600))
            token.update(updated)
            self.vault.save(provider, account, token)
        return str(token["access_token"])

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
        ids = [str(item.get("id") or item.get("remote_id") or "") for item in items]
        if any(not item_id for item_id in ids):
            raise RemoteRuntimeError("동기화 항목에 원격 ID가 없습니다.")
        with self._session() as conn:
            conn.execute("DELETE FROM remote_catalog WHERE provider=? AND account=?", (provider, account))
            conn.executemany("INSERT INTO remote_catalog VALUES(?,?,?,?,?)",
                             [(provider, account, item_id, json.dumps(item, ensure_ascii=False), time.time())
                              for item_id, item in zip(ids, items)])
        return ids

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

    def sync(self, provider: str, account: str, options: Dict[str, Any]) -> List[Dict[str, Any]]:
        limit = int(options.get("limit", 50))
        if provider == "google_drive":
            return self._request("google", account, "GET", "https://www.googleapis.com/drive/v3/files",
                                 params={"pageSize": limit,
                                         "fields": "files(id,name,mimeType,modifiedTime,md5Checksum)"}).json().get("files", [])
        if provider == "onedrive":
            return self._request("microsoft", account, "GET",
                                 "https://graph.microsoft.com/v1.0/me/drive/root/delta").json().get("value", [])
        if provider == "notion":
            token = os.getenv("NOTION_API_TOKEN", "")
            if not token:
                raise RemoteRuntimeError("NOTION_API_TOKEN 설정이 필요합니다.")
            response = self.session.post("https://api.notion.com/v1/search",
                                         headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
                                         json={"page_size": limit}, timeout=30)
            OAuthCoordinator._raise(response)
            return response.json().get("results", [])
        raise RemoteRuntimeError(f"지원하지 않는 동기화 Provider: {provider}")

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
