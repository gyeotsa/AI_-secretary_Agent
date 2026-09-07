import time

import pytest

from core.remote_runtime import (OAuthCoordinator, PendingRemoteAction, ProviderApi,
                                 RemoteActionStore, RemoteApplyReceipt,
                                 RemoteCatalogStore, SecureTokenVault)
from plugins.cloud_communication import CloudCommunicationPlugin


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self.payload


class Session:
    def __init__(self):
        self.calls = []
        self.slack_text = ""

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if "oauth" in url:
            return Response({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        if "chat.postMessage" in url:
            self.slack_text = kwargs["json"]["text"]
            return Response({"ok": True, "ts": "171.42"})
        return Response({"id": "remote-42"})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "conversations.history" in url:
            return Response({"ok": True, "messages": [{"ts": "171.42", "text": self.slack_text, "user": "U1"}]})
        if "/events/" in url:
            return Response({"id": "remote-42", "summary": "회의"})
        return Response({"id": "remote-42"})

    def request(self, method, url, **kwargs):
        return self.post(url, **kwargs) if method == "POST" else self.get(url, **kwargs)


class Vault:
    def __init__(self):
        self.tokens = {}

    def save(self, provider, account, token):
        self.tokens[(provider, account)] = token

    def load(self, provider, account):
        return self.tokens.get((provider, account))


def test_dpapi_vault_round_trip_does_not_store_plaintext(tmp_path):
    vault = SecureTokenVault(str(tmp_path))
    vault.save("google", "boss@example.com", {"access_token": "plain-secret"})
    files = list(tmp_path.glob("*.dpapi"))
    assert len(files) == 1 and b"plain-secret" not in files[0].read_bytes()
    assert vault.load("google", "boss@example.com")["access_token"] == "plain-secret"


def test_oauth_pkce_complete_and_secret_free_status(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client")
    vault, session = Vault(), Session()
    oauth = OAuthCoordinator(vault, session)
    begun = oauth.begin("google", "boss", "http://127.0.0.1/callback")
    assert "code_challenge=" in begun["authorization_url"]
    completed = oauth.complete(begun["state"], "code")
    assert completed["account"] == "boss"
    status = oauth.status("google", "boss")
    assert status["authenticated"] and "access_token" not in status


def test_expired_oauth_token_is_refreshed(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client")
    vault, session = Vault(), Session()
    vault.save("google", "boss", {"access_token": "old", "refresh_token": "refresh", "expires_at": time.time() - 1})
    assert OAuthCoordinator(vault, session).access_token("google", "boss") == "access"


def test_remote_draft_and_catalog_are_persisted(tmp_path):
    actions = RemoteActionStore(str(tmp_path / "actions.db"))
    draft = actions.create("google", "boss", "google_calendar_create", {"event": {"summary": "회의"}})
    assert draft.status == "draft" and not draft.remote_id
    assert actions.claim_for_apply(draft.action_id).status == "applying"
    assert actions.mark_applied(draft.action_id, "event-1").remote_id == "event-1"
    catalog = RemoteCatalogStore(str(tmp_path / "catalog.db"))
    assert catalog.replace("google_drive", "boss", [{"id": "file-1", "name": "보고서"}]) == ["file-1"]
    assert catalog.list("google_drive", "boss")[0]["name"] == "보고서"


def test_google_calendar_and_slack_requery_remote_ids(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    vault, session = Vault(), Session()
    vault.save("google", "boss", {"access_token": "token", "expires_at": time.time() + 3600})
    api = ProviderApi(OAuthCoordinator(vault, session), session)
    event = PendingRemoteAction("a", "google", "boss", "google_calendar_create",
                                {"event": {"summary": "회의"}})
    assert api.apply(event) == "remote-42"
    assert any(method == "GET" and url.endswith("/remote-42") for method, url, _ in session.calls)
    slack = PendingRemoteAction("b", "slack", "boss", "slack_send",
                                {"channel": "C1", "text": "완료"})
    assert api.apply(slack) == "171.42"


def test_plugin_keeps_draft_and_apply_separate(tmp_path, monkeypatch):
    plugin = CloudCommunicationPlugin()
    plugin.actions = RemoteActionStore(str(tmp_path / "actions.db"))
    monkeypatch.setattr(plugin.api, "apply", lambda action: RemoteApplyReceipt(
        "message-9", {"provider": "slack", "remote_id": "message-9",
                      "verified_fields": ["ts", "channel", "text"]},
    ))
    draft = plugin.execute_tool("remote_create_draft", {
        "provider": "slack", "account": "boss", "operation": "slack_send",
        "payload": {"channel": "C1", "text": "검토 요청"},
    })
    assert draft.succeeded
    action_id = draft.evidence[0].data["action_id"]
    assert plugin.actions.get(action_id).status == "draft"
    applied = plugin.execute_tool("remote_apply_draft", {"action_id": action_id})
    assert applied.succeeded and applied.evidence[0].data["remote_id"] == "message-9"
    assert not plugin.execute_tool("remote_apply_draft", {"action_id": action_id}).succeeded


def test_communication_summary_preserves_message_ids(monkeypatch):
    plugin = CloudCommunicationPlugin()
    monkeypatch.setattr(plugin.api, "read_messages", lambda *_args: [
        {"ts": "1.2", "text": "첫 메시지", "user": "U1"},
        {"ts": "1.3", "text": "둘째 메시지", "user": "U2"},
    ])
    result = plugin.execute_tool("communication_read_summary", {
        "provider": "slack", "account": "boss", "channel": "C1", "limit": 10,
    })
    assert result.succeeded
    assert [item["remote_id"] for item in result.evidence[0].data["messages"]] == ["1.2", "1.3"]
