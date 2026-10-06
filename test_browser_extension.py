import asyncio
from concurrent.futures import Future
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import core.browser_extension as extension


def test_finished_future_timeout_preserves_the_original_failure(monkeypatch):
    future = Future()
    error = TimeoutError("source-timeout")
    future.set_exception(error)
    monkeypatch.setattr(extension.time, "monotonic", Mock(side_effect=[0, 10]))
    with pytest.raises(TimeoutError) as raised:
        extension.BrowserExtensionSession._wait(future, 5, lambda: None)
    assert raised.value is error


def session_without_process(monkeypatch):
    server = Mock()
    server.is_file.return_value = True
    server.with_name.return_value.read_text.return_value = json.dumps(
        {"name": "@playwright/mcp", "version": extension.VERSION})
    monkeypatch.setattr(extension, "SERVER", server)
    monkeypatch.setattr(extension.shutil, "which", lambda _: "node")
    return extension.BrowserExtensionSession()


def close_during_start(session, reached, release):
    session._start()
    assert reached.wait(3), "worker did not reach the startup barrier"
    closer = threading.Thread(target=session.close)
    closer.start()
    try:
        assert session._close_requested.wait(3), "shutdown intent was not recorded"
    finally:
        release.set()
        closer.join(3)
    assert not closer.is_alive()
    assert not session._thread.is_alive()
    with pytest.raises(RuntimeError, match="시작하지 못"):
        session._ready.result()
    assert session._client is None


def test_close_before_async_loop_does_not_start_mcp(monkeypatch):
    import mcp
    client = Mock(side_effect=AssertionError("MCP must not start after shutdown"))
    monkeypatch.setattr(mcp, "Client", client)
    session = session_without_process(monkeypatch)
    reached, release = threading.Event(), threading.Event()
    run = session._run

    def delayed_run():
        reached.set()
        assert release.wait(3)
        run()

    monkeypatch.setattr(session, "_run", delayed_run)
    try:
        close_during_start(session, reached, release)
        client.assert_not_called()
    finally:
        release.set()
        session.close()


def test_close_during_client_start_cleans_up_without_becoming_ready(monkeypatch):
    import mcp
    reached, release = threading.Event(), threading.Event()
    entered, exited = [], []

    class Client:
        list_tools = AsyncMock(return_value=SimpleNamespace(
            tools=[SimpleNamespace(name="browser_run_code_unsafe")]))

        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            reached.set()
            while not release.is_set():
                await asyncio.sleep(.001)
            entered.append(True)
            return self

        async def __aexit__(self, *_args):
            exited.append(True)

    monkeypatch.setattr(mcp, "Client", Client)
    session = session_without_process(monkeypatch)
    try:
        close_during_start(session, reached, release)
        assert entered == exited == [True]
        Client.list_tools.assert_not_called()
    finally:
        release.set()
        session.close()
