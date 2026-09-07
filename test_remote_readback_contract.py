import base64
import sqlite3
import time
from email.message import EmailMessage

import pytest

from core.remote_runtime import (
    OAuthCoordinator,
    PendingRemoteAction,
    ProviderApi,
    RemoteActionStore,
    RemoteApplyReceipt,
    RemoteApplyRejected,
    RemoteApplyUncertain,
    RemoteRuntimeError,
)
from core.tool_result import ToolRunStatus
from plugins.cloud_communication import CloudCommunicationPlugin


class Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


class QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, f"unexpected request: {method} {url}"
        expected_method, fragment, response = self.responses.pop(0)
        assert method == expected_method and fragment in url
        return response

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class Vault:
    def __init__(self):
        expires = time.time() + 3600
        self.tokens = {
            ("google", "owner"): {"access_token": "google", "expires_at": expires},
            ("microsoft", "owner"): {"access_token": "microsoft", "expires_at": expires},
        }

    def load(self, provider, account):
        return self.tokens.get((provider, account))

    def save(self, provider, account, token):
        self.tokens[(provider, account)] = token


def _raw_mail(*, subject="검토", recipient="person@example.com", body="내용입니다"):
    message = EmailMessage()
    message["From"] = "owner@example.com"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def _provider_case(operation, *, mismatch=False):
    if operation == "gmail_send":
        expected = _raw_mail()
        observed = _raw_mail(subject="다른 제목") if mismatch else expected
        action = PendingRemoteAction("a", "google", "owner", operation, {"raw": expected})
        responses = [
            ("POST", "/drafts", Response({"id": "draft-1"})),
            ("POST", "/drafts/send", Response({"id": "mail-1"})),
            ("GET", "/messages/mail-1", Response({"id": "mail-1", "raw": observed})),
        ]
    elif operation == "google_calendar_create":
        event = {"summary": "회의", "start": {"dateTime": "2026-09-01T10:00:00+09:00"},
                 "end": {"dateTime": "2026-09-01T11:00:00+09:00"}}
        observed = {"id": "event-1", **event}
        if mismatch:
            observed["end"] = {"dateTime": "2026-09-01T12:00:00+09:00"}
        action = PendingRemoteAction("a", "google", "owner", operation, {"event": event})
        responses = [
            ("POST", "/events", Response({"id": "event-1"})),
            ("GET", "/events/event-1", Response(observed)),
        ]
    elif operation == "outlook_send":
        message = {"subject": "검토", "body": {"contentType": "Text", "content": "내용"},
                   "toRecipients": [{"emailAddress": {"address": "person@example.com"}}]}
        observed = {"id": "mail-1", **message}
        if mismatch:
            observed["toRecipients"] = [{"emailAddress": {"address": "other@example.com"}}]
        action = PendingRemoteAction("a", "microsoft", "owner", operation, {"message": message})
        responses = [
            ("POST", "/me/messages", Response({"id": "mail-1"})),
            ("POST", "/mail-1/send", Response({}, 202)),
            ("GET", "/me/messages/mail-1", Response(observed)),
        ]
    elif operation == "outlook_calendar_create":
        event = {"subject": "회의", "start": {"dateTime": "2026-09-01T10:00:00", "timeZone": "Asia/Seoul"},
                 "end": {"dateTime": "2026-09-01T11:00:00", "timeZone": "Asia/Seoul"}}
        observed = {"id": "event-1", **event}
        if mismatch:
            observed["subject"] = "다른 회의"
        action = PendingRemoteAction("a", "microsoft", "owner", operation, {"event": event})
        responses = [
            ("POST", "/me/events", Response({"id": "event-1"})),
            ("GET", "/me/events/event-1", Response(observed)),
        ]
    elif operation == "slack_send":
        text = "검토 부탁드립니다"
        observed_text = "다른 본문" if mismatch else text
        action = PendingRemoteAction("a", "slack", "owner", operation,
                                     {"channel": "C1", "text": text})
        responses = [
            ("POST", "chat.postMessage", Response({"ok": True, "ts": "171.42"})),
            ("GET", "conversations.history", Response(
                {"ok": True, "messages": [{"ts": "171.42", "text": observed_text}]}
            )),
        ]
    elif operation == "teams_send":
        text = "검토 부탁드립니다"
        observed_text = "<p>다른 본문</p>" if mismatch else f"<p>{text}</p>"
        action = PendingRemoteAction("a", "microsoft", "owner", operation,
                                     {"team_id": "T1", "channel_id": "C1", "text": text})
        responses = [
            ("POST", "/messages", Response({"id": "message-1"})),
            ("GET", "/messages/message-1", Response(
                {"id": "message-1", "body": {"content": observed_text}}
            )),
        ]
    else:  # pragma: no cover - test helper guard
        raise AssertionError(operation)
    return action, QueueSession(responses)


@pytest.mark.parametrize("operation", [
    "gmail_send", "google_calendar_create", "outlook_send",
    "outlook_calendar_create", "slack_send", "teams_send",
])
def test_each_provider_returns_semantic_readback_receipt(monkeypatch, operation):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    action, session = _provider_case(operation)
    receipt = ProviderApi(OAuthCoordinator(Vault(), session), session).apply(action)
    assert isinstance(receipt, RemoteApplyReceipt)
    assert receipt.remote_id
    assert "verified_fields" in receipt.verification
    assert not session.responses


@pytest.mark.parametrize("operation", [
    "gmail_send", "google_calendar_create", "outlook_send",
    "outlook_calendar_create", "slack_send", "teams_send",
])
def test_each_provider_rejects_id_only_or_mismatched_readback(monkeypatch, operation):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    action, session = _provider_case(operation, mismatch=True)
    with pytest.raises(RemoteApplyUncertain) as captured:
        ProviderApi(OAuthCoordinator(Vault(), session), session).apply(action)
    assert captured.value.stage == "재조회 검증"
    assert captured.value.remote_id


def test_action_store_migrates_old_schema_and_claim_is_single_use(tmp_path):
    path = tmp_path / "actions.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE remote_actions(
            action_id TEXT PRIMARY KEY, provider TEXT, account TEXT, operation TEXT,
            payload TEXT, status TEXT, remote_id TEXT, created_at REAL, updated_at REAL)""")
    store = RemoteActionStore(str(path))
    action = store.create("slack", "owner", "slack_send", {"channel": "C1", "text": "안녕"})
    with pytest.raises(RemoteRuntimeError):
        store.mark_applied(action.action_id, "must-claim-first")
    assert store.claim_for_apply(action.action_id).status == "applying"
    with pytest.raises(RemoteRuntimeError):
        store.claim_for_apply(action.action_id)
    reopened = RemoteActionStore(str(path)).get(action.action_id)
    assert reopened.status == "applying" and reopened.last_error == ""


def test_provider_and_oauth_preflight_fail_before_remote_mutation(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "token")
    session = QueueSession([])
    api = ProviderApi(OAuthCoordinator(Vault(), session), session)
    wrong_provider = PendingRemoteAction(
        "a", "google", "owner", "slack_send", {"channel": "C1", "text": "안녕"}
    )
    with pytest.raises(RemoteApplyRejected):
        api.apply(wrong_provider)
    missing_account = PendingRemoteAction(
        "b", "google", "missing", "google_calendar_create", {"event": {"summary": "회의"}}
    )
    with pytest.raises(RemoteApplyRejected):
        api.apply(missing_account)
    assert session.calls == []


def test_plugin_persists_uncertain_and_never_resends_same_action(tmp_path, monkeypatch):
    plugin = CloudCommunicationPlugin()
    plugin.actions = RemoteActionStore(str(tmp_path / "actions.db"))
    action = plugin.actions.create("slack", "owner", "slack_send",
                                   {"channel": "C1", "text": "한 번만"})
    calls = []

    def ambiguous(_action):
        calls.append(_action.action_id)
        raise TimeoutError("전송 응답을 받기 전에 연결이 끊겼습니다")

    monkeypatch.setattr(plugin.api, "apply", ambiguous)
    first = plugin.execute_tool("remote_apply_draft", {"action_id": action.action_id})
    second = plugin.execute_tool("remote_apply_draft", {"action_id": action.action_id})
    assert first.status == ToolRunStatus.UNVERIFIED
    assert second.status == ToolRunStatus.UNVERIFIED
    assert calls == [action.action_id]
    persisted = plugin.actions.get(action.action_id)
    assert persisted.status == "uncertain"
    assert persisted.last_error and not first.evidence[0].data["retry_allowed"]


def test_preflight_rejection_returns_to_draft_but_plain_id_is_not_success(tmp_path, monkeypatch):
    plugin = CloudCommunicationPlugin()
    plugin.actions = RemoteActionStore(str(tmp_path / "actions.db"))
    rejected = plugin.actions.create("slack", "owner", "slack_send",
                                     {"channel": "C1", "text": "내용"})
    monkeypatch.setattr(plugin.api, "apply", lambda _action: (_ for _ in ()).throw(
        RemoteApplyRejected("토큰 설정이 필요합니다.")
    ))
    result = plugin.execute_tool("remote_apply_draft", {"action_id": rejected.action_id})
    assert result.status == ToolRunStatus.FAILED
    assert plugin.actions.get(rejected.action_id).status == "draft"
    assert result.evidence[0].data["retry_allowed"]

    id_only = plugin.actions.create("slack", "owner", "slack_send",
                                    {"channel": "C1", "text": "내용"})
    monkeypatch.setattr(plugin.api, "apply", lambda _action: "message-id-only")
    result = plugin.execute_tool("remote_apply_draft", {"action_id": id_only.action_id})
    assert result.status == ToolRunStatus.UNVERIFIED
    assert plugin.actions.get(id_only.action_id).status == "uncertain"


def test_verified_receipt_is_the_only_success_path(tmp_path, monkeypatch):
    plugin = CloudCommunicationPlugin()
    plugin.actions = RemoteActionStore(str(tmp_path / "actions.db"))
    action = plugin.actions.create("slack", "owner", "slack_send",
                                   {"channel": "C1", "text": "내용"})
    monkeypatch.setattr(plugin.api, "apply", lambda _action: RemoteApplyReceipt(
        "171.42", {"provider": "slack", "remote_id": "171.42",
                   "verified_fields": ["ts", "channel", "text"]},
    ))
    result = plugin.execute_tool("remote_apply_draft", {"action_id": action.action_id})
    assert result.status == ToolRunStatus.SUCCEEDED
    assert result.evidence[0].kind == "remote_content_readback"
    assert result.evidence[0].data["read_back"]["verified_fields"] == ["ts", "channel", "text"]
    assert plugin.actions.get(action.action_id).status == "applied"
