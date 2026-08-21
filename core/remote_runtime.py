"""OAuth, encrypted credentials, approval drafts and verified remote adapters."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from config import Config


class RemoteRuntimeError(RuntimeError):
    """Authentication, transport, or verification failure."""


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


class RemoteActionStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or Path(Config.DB_PATH).with_name("remote_actions.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS remote_actions(
                action_id TEXT PRIMARY KEY, provider TEXT, account TEXT, operation TEXT,
                payload TEXT, status TEXT, remote_id TEXT, created_at REAL, updated_at REAL)""")

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
            conn.execute("INSERT INTO remote_actions VALUES(?,?,?,?,?,?,?,?,?)",
                         (action_id, provider, account, operation, json.dumps(payload, ensure_ascii=False),
                          "draft", "", now, now))
        return self.get(action_id)

    def get(self, action_id: str) -> Optional[PendingRemoteAction]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM remote_actions WHERE action_id=?", (action_id,)).fetchone()
        if not row:
            return None
        return PendingRemoteAction(row["action_id"], row["provider"], row["account"], row["operation"],
                                   json.loads(row["payload"]), row["status"], row["remote_id"],
                                   row["created_at"], row["updated_at"])

    def mark_applied(self, action_id: str, remote_id: str) -> Optional[PendingRemoteAction]:
        with self._session() as conn:
            cursor = conn.execute("UPDATE remote_actions SET status='applied',remote_id=?,updated_at=? "
                                  "WHERE action_id=? AND status='draft'", (remote_id, time.time(), action_id))
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

    def apply(self, action: PendingRemoteAction) -> str:
        account, operation, data = action.account, action.operation, action.payload
        if operation == "gmail_send":
            draft = self._request("google", account, "POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                                  json={"message": {"raw": data["raw"]}}).json()
            sent = self._request("google", account, "POST", "https://gmail.googleapis.com/gmail/v1/users/me/drafts/send",
                                 json={"id": draft["id"]}).json()
            remote_id = sent["id"]
            self._request("google", account, "GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{remote_id}")
            return remote_id
        if operation == "google_calendar_create":
            calendar_id = data.get("calendar_id", "primary")
            event = self._request("google", account, "POST",
                                  f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                                  json=data["event"]).json()
            remote_id = event["id"]
            self._request("google", account, "GET",
                          f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{remote_id}")
            return remote_id
        graph = "https://graph.microsoft.com/v1.0"
        immutable = {"Prefer": 'IdType="ImmutableId"'}
        if operation == "outlook_send":
            draft = self._request("microsoft", account, "POST", graph + "/me/messages",
                                  json=data["message"], headers=immutable).json()
            remote_id = draft["id"]
            self._request("microsoft", account, "POST", graph + f"/me/messages/{remote_id}/send", headers=immutable)
            self._request("microsoft", account, "GET", graph + f"/me/messages/{remote_id}", headers=immutable)
            return remote_id
        if operation == "outlook_calendar_create":
            event = self._request("microsoft", account, "POST", graph + "/me/events",
                                  json=data["event"], headers=immutable).json()
            remote_id = event["id"]
            self._request("microsoft", account, "GET", graph + f"/me/events/{remote_id}", headers=immutable)
            return remote_id
        if operation == "slack_send":
            token = os.getenv("SLACK_BOT_TOKEN", "")
            if not token:
                raise RemoteRuntimeError("SLACK_BOT_TOKEN 설정이 필요합니다.")
            headers = {"Authorization": f"Bearer {token}"}
            sent = self.session.post("https://slack.com/api/chat.postMessage", headers=headers,
                                     json={"channel": data["channel"], "text": data["text"]}, timeout=30).json()
            if not sent.get("ok"):
                raise RemoteRuntimeError(str(sent))
            remote_id = sent["ts"]
            verified = self.session.get("https://slack.com/api/conversations.history", headers=headers,
                                        params={"channel": data["channel"], "latest": remote_id,
                                                "inclusive": True, "limit": 1}, timeout=30).json()
            if not verified.get("ok") or not any(m.get("ts") == remote_id for m in verified.get("messages", [])):
                raise RemoteRuntimeError("Slack Message ID(ts) 사후 검증에 실패했습니다.")
            return remote_id
        if operation == "teams_send":
            url = graph + f"/teams/{data['team_id']}/channels/{data['channel_id']}/messages"
            message = self._request("microsoft", account, "POST", url,
                                    json={"body": {"content": data["text"]}}).json()
            remote_id = message["id"]
            self._request("microsoft", account, "GET", url + f"/{remote_id}")
            return remote_id
        raise RemoteRuntimeError(f"지원하지 않는 원격 작업: {operation}")

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
