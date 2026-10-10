import asyncio
from concurrent.futures import Future
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.browser_extension as extension


TOKEN = "test-mail-connection-placeholder"


class Vault:
    def __init__(self):
        self.record = None
        self.calls = []

    def load(self, provider, account):
        self.calls.append(("load", provider, account))
        return self.record

    def save(self, provider, account, record):
        self.calls.append(("save", provider, account))
        self.record = record.copy()

    def delete(self, provider, account):
        self.calls.append(("delete", provider, account))
        self.record = None


def test_token_is_scoped_to_mail_vault_and_only_returned_to_child_environment():
    vault = Vault()
    credentials = extension.BrowserExtensionCredentials(vault)
    original_environment = dict(os.environ)
    assert credentials.status() == {"configured": False}
    assert credentials.environment() == {}
    assert credentials.save(TOKEN) == {"configured": True}
    assert vault.record == {"version": 1, "token": TOKEN}
    assert credentials.status() == {"configured": True}
    assert credentials.environment() == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": TOKEN}
    assert credentials.delete() == {"configured": False}
    assert credentials.environment() == {}
    assert all(call[1:] == ("anis-mail-extension", "chrome") for call in vault.calls)
    assert dict(os.environ) == original_environment


def test_real_dpapi_roundtrip_keeps_plaintext_out_of_saved_file(tmp_path):
    pytest.importorskip("win32crypt")
    from core.remote_runtime import SecureTokenVault
    vault = SecureTokenVault(str(tmp_path))
    credentials = extension.BrowserExtensionCredentials(vault)
    assert credentials.save(TOKEN) == {"configured": True}
    saved = list(tmp_path.glob("*.dpapi"))
    assert len(saved) == 1
    assert TOKEN.encode() not in saved[0].read_bytes()
    assert extension.BrowserExtensionCredentials(SecureTokenVault(str(tmp_path))).environment() == {
        "PLAYWRIGHT_MCP_EXTENSION_TOKEN": TOKEN}
    assert credentials.delete() == {"configured": False}
    assert not saved[0].exists()


def test_installed_stdio_transport_omits_parent_debug_environment(monkeypatch):
    from mcp.client.stdio import get_default_environment
    for name in ("DEBUG", "PWDEBUG", "PLAYWRIGHT_MCP_EXTENSION_TOKEN"):
        monkeypatch.setenv(name, TOKEN)
    inherited = get_default_environment()
    assert not {"DEBUG", "PWDEBUG", "PLAYWRIGHT_MCP_EXTENSION_TOKEN"} & inherited.keys()


@pytest.mark.parametrize("token", [TOKEN, "PLAYWRIGHT_MCP_EXTENSION_TOKEN=" + TOKEN])
def test_raw_and_official_copied_assignment_save_only_canonical_token(token):
    vault = Vault()
    credentials = extension.BrowserExtensionCredentials(vault)
    assert credentials.save(token) == {"configured": True}
    assert vault.record == {"version": 1, "token": TOKEN}
    assert credentials.environment() == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": TOKEN}


def test_previously_saved_official_assignment_is_normalized_on_load():
    vault = Vault()
    vault.record = {"version": 1, "token": "PLAYWRIGHT_MCP_EXTENSION_TOKEN=" + TOKEN}
    credentials = extension.BrowserExtensionCredentials(vault)
    assert credentials.status() == {"configured": True}
    assert credentials.environment() == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": TOKEN}


@pytest.mark.parametrize("token", [
    "OTHER_TOKEN=" + TOKEN, "playwright_mcp_extension_token=" + TOKEN,
    "PLAYWRIGHT_MCP_EXTENSION_TOKENX=" + TOKEN, "PLAYWRIGHT_MCP_EXTENSION_TOKEN=",
    "PLAYWRIGHT_MCP_EXTENSION_TOKEN==" + TOKEN,
    "PLAYWRIGHT_MCP_EXTENSION_TOKEN=PLAYWRIGHT_MCP_EXTENSION_TOKEN=" + TOKEN,
    "PLAYWRIGHT_MCP_EXTENSION_TOKEN= " + TOKEN,
    "PLAYWRIGHT_MCP_EXTENSION_TOKEN=" + TOKEN + "\n",
])
def test_malformed_assignment_is_rejected_on_save_and_load(token):
    vault = Vault()
    credentials = extension.BrowserExtensionCredentials(vault)
    with pytest.raises(extension.BrowserExtensionCredentialsError) as raised:
        credentials.save(token)
    assert TOKEN not in str(raised.value) and not vault.calls
    vault.record = {"version": 1, "token": token}
    with pytest.raises(extension.BrowserExtensionCredentialsError) as raised:
        credentials.environment()
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize("token", [None, "", " ", "abc def", "a\tb", "a\nb", "a\0b",
                                   "a\x7fb", "a\u00a0b", "x" * 4097])
def test_invalid_token_is_rejected_without_changing_vault_or_echoing_input(token):
    vault = Vault()
    credentials = extension.BrowserExtensionCredentials(vault)
    with pytest.raises(extension.BrowserExtensionCredentialsError) as raised:
        credentials.save(token)
    assert str(raised.value).startswith("확장의 연결 토큰만 입력하세요.")
    assert vault.calls == []


@pytest.mark.parametrize("record", [[], {}, {"version": True, "token": TOKEN},
                                    {"version": 2, "token": TOKEN},
                                    {"version": 1, "token": "a b"},
                                    {"version": 1, "token": TOKEN, "other": "private"}])
def test_corrupt_stored_credential_is_not_used_or_echoed(record):
    vault = Vault()
    vault.record = record
    credentials = extension.BrowserExtensionCredentials(vault)
    with pytest.raises(extension.BrowserExtensionCredentialsError) as raised:
        credentials.environment()
    assert str(raised.value).startswith("암호화된 자동 연결 정보를 확인하지 못했습니다.")
    assert TOKEN not in str(raised.value)


@pytest.mark.parametrize("method,argument,vault_method", [
    ("status", (), "load"), ("environment", (), "load"),
    ("save", (TOKEN,), "save"), ("delete", (), "delete")])
def test_vault_failure_has_only_safe_app_message(method, argument, vault_method):
    vault = Vault()
    setattr(vault, vault_method, Mock(side_effect=RuntimeError(TOKEN)))
    credentials = extension.BrowserExtensionCredentials(vault)
    with pytest.raises(extension.BrowserExtensionCredentialsError) as raised:
        getattr(credentials, method)(*argument)
    assert TOKEN not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("environment,client_name,expected", [
    (None, "ANIS Calendar", None),
    (lambda: {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": TOKEN}, "ANIS Mail",
     {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": TOKEN}),
])
def test_session_passes_token_only_to_child_and_preserves_default_client(
        monkeypatch, environment, client_name, expected):
    import mcp
    captured = {}
    session = extension.BrowserExtensionSession(environment=environment, client_name=client_name)
    session._ready = Future()
    monkeypatch.setattr(extension.shutil, "which", lambda _: "node")

    class Client:
        def __init__(self, params, **kwargs):
            captured["params"] = params
            captured["info"] = kwargs["client_info"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def list_tools(self):
            session._stop.set()
            return SimpleNamespace(tools=[SimpleNamespace(name="browser_run_code_unsafe")])

    monkeypatch.setattr(mcp, "Client", Client)
    before = dict(os.environ)
    asyncio.run(session._serve())
    assert captured["params"].env == expected
    assert TOKEN not in " ".join(captured["params"].args)
    assert captured["info"].name == client_name
    assert dict(os.environ) == before
    assert session._ready.result() is True


@pytest.mark.parametrize("index", [-1, True, 0.0, "0", None])
def test_invalid_selection_never_connects(index):
    session = extension.BrowserExtensionSession()
    session._start = Mock()
    with pytest.raises(ValueError):
        session.select_tab(index)
    session._start.assert_not_called()


def test_tab_selection_returns_no_private_server_snapshot():
    session = extension.BrowserExtensionSession()
    captured = {}

    def call(tool, arguments, decode, **kwargs):
        captured.update(tool=tool, arguments=arguments, kwargs=kwargs)
        return decode(SimpleNamespace(is_error=False, content=[SimpleNamespace(
            type="text", text=f"private tab title {TOKEN}")]))

    session._call_tool = call
    checkpoint = Mock()
    assert session.select_tab(2, checkpoint=checkpoint, timeout=10) == {"selected": True, "index": 2}
    assert captured == {"tool": "browser_tabs", "arguments": {"action": "select", "index": 2},
                        "kwargs": {"checkpoint": checkpoint, "timeout": 10}}


def test_tab_selection_failure_is_sanitized():
    session = extension.BrowserExtensionSession()

    def call(_tool, _arguments, decode, **_kwargs):
        return decode(SimpleNamespace(is_error=True, content=[SimpleNamespace(type="text", text=TOKEN)]))

    session._call_tool = call
    with pytest.raises(RuntimeError) as raised:
        session.select_tab(0)
    assert TOKEN not in str(raised.value)


def test_cancelled_tab_selection_closes_owned_session_before_any_action():
    session = extension.BrowserExtensionSession()
    session._start = Mock()
    session.close = Mock()
    checkpoint = Mock(side_effect=InterruptedError("cancelled"))
    with pytest.raises(InterruptedError):
        session.select_tab(0, checkpoint=checkpoint)
    session._start.assert_not_called()
    session.close.assert_called_once()
    assert session._lock.acquire(blocking=False)
    session._lock.release()


def test_selection_uses_same_bounded_call_and_result_wait_as_code(monkeypatch):
    session = extension.BrowserExtensionSession()
    session._start = Mock()
    session._ready = Future()
    session._ready.set_result(True)
    session._loop = object()
    session._client = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
        is_error=False, content=[])))
    session.close = Mock()
    result = Future()
    result.set_result({"selected": True, "index": 1})

    def submit(coroutine, loop):
        assert loop is session._loop
        assert asyncio.run(coroutine) == {"selected": True, "index": 1}
        return result

    async def bounded(awaitable, _timeout, checkpoint):
        checkpoint()
        return await awaitable

    monkeypatch.setattr(extension.asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(extension, "_bounded", bounded)
    assert session.select_tab(1, checkpoint=lambda: None) == {"selected": True, "index": 1}
    session._client.call_tool.assert_awaited_once_with("browser_tabs", {"action": "select", "index": 1})
    session.close.assert_not_called()
