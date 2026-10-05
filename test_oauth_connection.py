"""OAuth protocol boundaries with fake providers and real local callbacks only."""
import http.client
import json
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from core.oauth_connection import OAuthConsentDenied, OAuthLoopbackConnection
from core.plugin import ToolCancelledError
from core.remote_runtime import OAuthCoordinator, RemoteRuntimeError, SecureTokenVault


class Vault:
    def __init__(self):
        self.tokens = {}

    def save(self, provider, account, token):
        self.tokens[provider, account] = dict(token)

    def load(self, provider, account):
        return self.tokens.get((provider, account))

    def delete(self, provider, account):
        return self.tokens.pop((provider, account), None) is not None


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.text = "fixture-client-secret fixture-code fixture-access"

    def json(self):
        return self.payload


class Session:
    def __init__(self, provider="google"):
        self.provider = provider
        self.calls = []
        self.token = {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
                      "expires_in": 3600, "token_type": "Bearer",
                      "scope": " ".join(OAuthCoordinator.SPECS[provider]["scopes"])}
        self.identity = ({"sub": "subject-1", "email": "owner@example.com", "email_verified": True}
                         if provider == "google" else {"id": "subject-1", "mail": "owner@example.com",
                                                       "userPrincipalName": "owner@example.com"})
        self.on_post = self.on_get = None
        self.status = 200

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self.on_post:
            self.on_post()
        return Response(dict(self.token), self.status)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.on_get:
            self.on_get()
        return Response(dict(self.identity), self.status)


def begin(oauth, provider="google", account="owner@example.com"):
    return oauth.begin(provider, account, "http://127.0.0.1:8765/oauth/callback",
                       client_id="fixture-client", client_secret="fixture-client-secret",
                       expected_identity=account)


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_identity_bound_token_scope_and_restart_refresh(provider, tmp_path, monkeypatch):
    vault = SecureTokenVault(str(tmp_path / provider))
    session = Session(provider)
    oauth = OAuthCoordinator(vault, session)
    started = begin(oauth, provider)
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["login_hint"] == ["owner@example.com"]
    assert "fixture-client-secret" not in started["authorization_url"]
    result = oauth.complete(started["state"], "fixture-code")
    assert result["authenticated"] and result["identity_email"] == "owner@example.com"
    assert result["scopes_verified"] and result["granted_scopes"]
    assert all(secret not in json.dumps(result) for secret in ("fixture-access", "fixture-client-secret", "fixture-refresh"))
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    saved = vault.load(provider, "owner@example.com")
    assert saved["remote_subject"] == "subject-1"
    assert saved["oauth_client_id"] == "fixture-client"
    assert all(b"fixture-client-secret" not in path.read_bytes() for path in vault.directory.glob("*.dpapi"))
    saved["expires_at"] = 0
    vault.save(provider, "owner@example.com", saved)
    monkeypatch.delenv(OAuthCoordinator.SPECS[provider]["client_id"], raising=False)
    restarted = OAuthCoordinator(vault, session)
    assert restarted.access_token(provider, "owner@example.com") == "fixture-access"
    refresh = session.calls[-1][2]["data"]
    assert refresh["client_id"] == "fixture-client" and refresh["client_secret"] == "fixture-client-secret"
    with pytest.raises(RemoteRuntimeError):
        restarted.complete(started["state"], "fixture-code")


def test_duplicate_expired_cancelled_and_restart_states_never_exchange():
    vault, session = Vault(), Session()
    oauth = OAuthCoordinator(vault, session)
    old = begin(oauth)
    fresh = begin(oauth)
    with pytest.raises(RemoteRuntimeError):
        oauth.complete(old["state"], "fixture-code")
    oauth._pending[fresh["state"]]["created_at"] -= 601
    with pytest.raises(RemoteRuntimeError):
        oauth.complete(fresh["state"], "fixture-code")
    cancelled = begin(oauth)
    assert oauth.cancel(cancelled["state"])
    assert not oauth.cancel(cancelled["state"])
    with pytest.raises(RemoteRuntimeError):
        oauth.complete(cancelled["state"], "fixture-code")
    restart = begin(oauth)
    with pytest.raises(RemoteRuntimeError):
        OAuthCoordinator(vault, session).complete(restart["state"], "fixture-code")
    assert not session.calls and not vault.tokens


@pytest.mark.parametrize("when", ["on_post", "on_get"])
def test_cancellation_during_http_discards_new_token(when):
    vault, session = Vault(), Session()
    oauth = OAuthCoordinator(vault, session)
    started = begin(oauth)
    setattr(session, when, lambda: oauth.cancel(started["state"]))
    with pytest.raises(ToolCancelledError):
        oauth.complete(started["state"], "fixture-code")
    assert not vault.tokens and not oauth._inflight


@pytest.mark.parametrize("field,value", [("access_token", ""), ("access_token", 123),
                                        ("expires_in", -1), ("expires_in", "nan"),
                                        ("expires_in", True), ("token_type", "MAC"),
                                        ("scope", ["email"]), ("refresh_token", {})])
def test_malformed_token_does_not_replace_existing_connection(field, value):
    vault, session = Vault(), Session()
    vault.save("google", "owner@example.com", {"remote_subject": "subject-1", "access_token": "old"})
    session.token[field] = value
    oauth = OAuthCoordinator(vault, session)
    with pytest.raises(RemoteRuntimeError):
        oauth.complete(begin(oauth)["state"], "fixture-code")
    assert vault.load("google", "owner@example.com")["access_token"] == "old"


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_wrong_identity_and_alias_rebinding_are_rejected(provider):
    vault, session = Vault(), Session(provider)
    oauth = OAuthCoordinator(vault, session)
    with pytest.raises(RemoteRuntimeError, match="다릅니다"):
        oauth.complete(begin(oauth, provider, "other@example.com")["state"], "fixture-code")
    assert not vault.tokens
    vault.save(provider, "alias", {"remote_subject": "original-subject", "access_token": "old"})
    started = oauth.begin(provider, "alias", "http://127.0.0.1/callback", client_id="fixture-client")
    with pytest.raises(RemoteRuntimeError, match="다른 계정"):
        oauth.complete(started["state"], "fixture-code")
    assert vault.load(provider, "alias")["access_token"] == "old"


def test_missing_scope_is_not_inferred_and_http_error_body_is_hidden():
    vault, session = Vault(), Session()
    session.token.pop("scope")
    oauth = OAuthCoordinator(vault, session)
    result = oauth.complete(begin(oauth)["state"], "fixture-code")
    assert result["authenticated"] and not result["scopes_verified"] and not result["granted_scopes"]
    session.status = 400
    with pytest.raises(RemoteRuntimeError) as error:
        oauth.complete(begin(oauth)["state"], "fixture-code")
    assert "fixture" not in str(error.value)


@pytest.mark.parametrize("redirect", ["http://localhost:8765/oauth/callback", "https://127.0.0.1/callback",
                                     "http://evil.test/callback", "http://127.0.0.1:8765/callback#fragment",
                                     "http://user@127.0.0.1:8765/callback", "http://127.0.0.1:8765/callback?q=1"])
def test_unsafe_redirect_is_rejected(redirect):
    oauth = OAuthCoordinator(Vault(), Session())
    with pytest.raises(RemoteRuntimeError):
        oauth.begin("google", "owner", redirect, client_id="fixture-client")
    assert not oauth._pending


def run_loopback(*, timeout=5, browser_result=True):
    vault, session = Vault(), Session()
    oauth = OAuthCoordinator(vault, session)
    launched = threading.Event()
    urls, result = [], {}

    def launch(url):
        urls.append(url)
        launched.set()
        return browser_result

    connection = OAuthLoopbackConnection(oauth, browser_open=launch, timeout=timeout)

    def work():
        try:
            result["value"] = connection.connect("google", "owner@example.com", client_id="fixture-client")
        except Exception as error:
            result["error"] = error

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    assert launched.wait(2)
    return connection, oauth, vault, session, thread, result, urls


def callback(connection, query, *, host=None, path=None):
    parsed = urlsplit(connection.redirect_uri)
    client = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=2)
    try:
        client.request("GET", (path or parsed.path) + "?" + urlencode(query),
                       headers={"Host": host or parsed.netloc})
        response = client.getresponse()
        return response.status, response.read().decode()
    finally:
        client.close()


def test_real_loopback_rejects_wrong_host_path_state_duplicates_then_connects(capsys):
    connection, oauth, vault, session, thread, result, urls = run_loopback()
    state = parse_qs(urlsplit(urls[0]).query)["state"][0]
    try:
        for options in ({"host": "evil.test"}, {"path": "/other"},):
            assert callback(connection, {"state": state, "code": "fixture-code"}, **options)[0] == 400
        assert callback(connection, {"state": "wrong", "code": "fixture-code"})[0] == 400
        assert callback(connection, [("state", state), ("state", state), ("code", "fixture-code")])[0] == 400
        assert not session.calls and not vault.tokens
        status, body = callback(connection, {"state": state, "code": "fixture-code"})
        assert status == 200 and "fixture" not in body and "connected" not in body
        thread.join(3)
        assert not thread.is_alive() and result["value"]["authenticated"]
        assert not oauth._pending and not oauth._inflight
        assert len([call for call in session.calls if call[0] == "POST"]) == 1
        with pytest.raises((ConnectionRefusedError, OSError)):
            callback(connection, {"state": state, "code": "fixture-code"})
        assert "fixture-code" not in capsys.readouterr().err
    finally:
        connection.cancel()
        thread.join(3)


@pytest.mark.parametrize("mode", ["denied", "cancelled", "timeout", "browser-failure"])
def test_listener_failure_paths_close_and_discard_state(mode):
    connection, oauth, vault, session, thread, result, urls = run_loopback(
        timeout=1 if mode == "timeout" else 5, browser_result=mode != "browser-failure")
    try:
        state = parse_qs(urlsplit(urls[0]).query)["state"][0]
        if mode == "denied":
            assert callback(connection, {"state": state, "error": "access_denied",
                                         "error_description": "fixture-private"})[0] == 200
        if mode == "cancelled":
            connection.cancel()
        thread.join(3)
        assert not thread.is_alive() and "error" in result
        expected = OAuthConsentDenied if mode == "denied" else ToolCancelledError if mode == "cancelled" else RemoteRuntimeError
        assert isinstance(result["error"], expected)
        assert not vault.tokens and not session.calls and not oauth._pending
    finally:
        connection.cancel()
        thread.join(3)


@pytest.mark.parametrize("mode", ["cancel", "expiry"])
def test_partial_http_request_cannot_prevent_cancel_or_expiry(mode):
    connection, oauth, _vault, _session, thread, result, _urls = run_loopback(timeout=1)
    parsed = urlsplit(connection.redirect_uri)
    client = socket.create_connection(("127.0.0.1", parsed.port), timeout=1)
    try:
        client.sendall(b"GET /oauth/callback HTTP/1.1\r\nHost: ")
        if mode == "cancel":
            connection.cancel()
        thread.join(2)
        expected = ToolCancelledError if mode == "cancel" else RemoteRuntimeError
        assert not thread.is_alive() and isinstance(result["error"], expected)
        assert not oauth._pending
    finally:
        client.close()
        connection.cancel()
        thread.join(2)


def test_legacy_reconnect_preserves_legacy_file_but_creates_bound_v2(tmp_path):
    vault = SecureTokenVault(str(tmp_path))
    legacy = vault.directory / f"{vault._legacy_name('google', 'owner@example.com')}.dpapi"
    legacy.touch()
    oauth = OAuthCoordinator(vault, Session())
    status = oauth.complete(begin(oauth)["state"], "fixture-code")
    assert status["authenticated"] and legacy.exists()
    assert vault.load("google", "owner@example.com")["remote_subject"] == "subject-1"


def test_unverified_email_and_missing_token_type_do_not_authenticate():
    vault, session = Vault(), Session()
    oauth = OAuthCoordinator(vault, session)
    session.identity["email_verified"] = False
    with pytest.raises(RemoteRuntimeError):
        oauth.complete(begin(oauth)["state"], "fixture-code")
    session.identity["email_verified"] = True
    session.token.pop("token_type")
    with pytest.raises(RemoteRuntimeError):
        oauth.complete(begin(oauth)["state"], "fixture-code")
    assert not vault.tokens
