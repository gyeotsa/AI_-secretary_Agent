"""Model transport contracts against an isolated loopback HTTP server."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import requests

import core.model_transport as transport
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context, current_turn_context


@pytest.fixture
def local_http():
    state = SimpleNamespace(
        started=threading.Event(), disconnected=threading.Event(), requests=[],
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append((self.path, json.loads(body)))
            if self.path in {"/headers", "/body"}:
                if self.path == "/body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "4096")
                    self.end_headers()
                    self.wfile.write(b'{"message":')
                    self.wfile.flush()
                state.started.set()
                self.connection.settimeout(2)
                try:
                    if self.connection.recv(1) == b"":
                        state.disconnected.set()
                except (ConnectionResetError, ConnectionAbortedError):
                    state.disconnected.set()
                except socket.timeout:
                    pass
                self.close_connection = True
                return

            status = 400 if self.path == "/http400" else 200
            payload = {"error": "does not support tools"} if status == 400 else {
                "message": {"content": "정상 응답 🙂"}, "echo": json.loads(body),
            }
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
        assert not worker.is_alive()


def _invoke(context, url, *, in_event_loop=False, timeout=5):
    def call():
        with bind_turn_context(context):
            return transport.post_json(url, json={"prompt": "local QA"}, timeout=timeout)

    async def async_call():
        return call()

    return asyncio.run(async_call()) if in_event_loop else call()


@pytest.mark.parametrize("in_event_loop", [False, True])
def test_response_is_requests_compatible_and_helper_is_joined(local_http, in_event_loop):
    context = TurnExecutionContext("normal", "session")
    response = _invoke(context, local_http.url + "/ok", in_event_loop=in_event_loop)
    assert isinstance(response, requests.Response)
    response.raise_for_status()
    assert response.json() == {
        "message": {"content": "정상 응답 🙂"}, "echo": {"prompt": "local QA"},
    }
    assert "정상 응답 🙂" in response.text
    assert response.request.method == "POST"
    assert response.headers["Content-Type"].startswith("application/json")
    assert len(local_http.requests) == 1
    response.close()
    assert current_turn_context() is None
    assert not any(t.name == transport._HELPER_THREAD_NAME for t in threading.enumerate())


def test_http400_remains_a_response_for_tool_compatibility_fallback(local_http):
    response = _invoke(TurnExecutionContext("http400", "s"), local_http.url + "/http400")
    assert response.status_code == 400
    assert response.json()["error"] == "does not support tools"
    with pytest.raises(requests.HTTPError) as error:
        response.raise_for_status()
    assert error.value.response is response
    assert len(local_http.requests) == 1


@pytest.mark.parametrize("path", ["/headers", "/body"])
@pytest.mark.parametrize("in_event_loop", [False, True])
def test_cancellation_interrupts_pending_io_and_closes_socket(local_http, path, in_event_loop):
    context = TurnExecutionContext("cancel", "session")
    errors = []

    def run():
        try:
            _invoke(context, local_http.url + path, in_event_loop=in_event_loop)
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=run)
    caller.start()
    try:
        assert local_http.started.wait(3)
        started = time.monotonic()
        context.cancel()
        caller.join(0.5)
        elapsed = time.monotonic() - started
        assert not caller.is_alive(), "cancelled HTTP call is still blocking its caller"
        # 25ms polling target; leave scheduler headroom for loaded Windows CI.
        assert elapsed < 0.3
        assert len(errors) == 1 and isinstance(errors[0], ToolCancelledError)
        assert local_http.disconnected.wait(0.5), "the cancelled socket remained open"
        assert len(local_http.requests) == 1
        assert not any(t.name == transport._HELPER_THREAD_NAME for t in threading.enumerate())
    finally:
        context.cancel()
        caller.join(3)
        assert not caller.is_alive()


@pytest.mark.parametrize("path", ["/headers", "/body"])
def test_timeout_is_requests_timeout_and_closes_socket(local_http, path):
    with pytest.raises(requests.Timeout):
        _invoke(TurnExecutionContext("timeout", "s"), local_http.url + path, timeout=0.05)
    assert local_http.disconnected.wait(0.5)
    assert len(local_http.requests) == 1


def test_cancelled_before_dispatch_does_not_create_client(monkeypatch):
    context = TurnExecutionContext("pre-cancelled", "s")
    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **_: pytest.fail("HTTP client created"))
    monkeypatch.setattr(transport.requests, "post", lambda *a, **k: pytest.fail("request dispatched"))
    with bind_turn_context(context):
        context.cancel()
        with pytest.raises(ToolCancelledError):
            transport.post_json("http://127.0.0.1/unused", json={}, timeout=1)


def test_without_turn_preserves_requests_post_contract(monkeypatch):
    result = requests.Response()
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(transport.requests, "post", post)
    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **_: pytest.fail("async client used"))
    assert transport.post_json("http://local.test", json={"v": 1}, timeout=(1, 2)) is result
    assert calls == [(("http://local.test",), {"json": {"v": 1}, "timeout": (1, 2)})]


@pytest.mark.parametrize("error_type, expected", [
    (httpx.ConnectError, requests.ConnectionError),
    (httpx.ReadError, requests.ConnectionError),
    (httpx.RemoteProtocolError, requests.ConnectionError),
    (httpx.ConnectTimeout, requests.Timeout),
    (httpx.ReadTimeout, requests.Timeout),
    (httpx.WriteTimeout, requests.Timeout),
])
def test_httpx_errors_are_translated_without_retry(monkeypatch, error_type, expected):
    calls = []
    clients = []
    async_client = httpx.AsyncClient
    context = TurnExecutionContext("error", "s")

    async def fail(request):
        calls.append(request)
        assert current_turn_context() is context
        raise error_type("synthetic transport failure", request=request)

    def client_factory(**kwargs):
        client = async_client(**kwargs, transport=httpx.MockTransport(fail), trust_env=False)
        clients.append(client)
        return client

    monkeypatch.setattr(transport.httpx, "AsyncClient", client_factory)
    with pytest.raises(expected):
        _invoke(context, "http://mock.test", in_event_loop=True)
    assert len(calls) == 1
    assert clients[0].is_closed
    assert not any(t.name == transport._HELPER_THREAD_NAME for t in threading.enumerate())


def test_late_transport_error_cannot_override_cancellation(monkeypatch):
    context = TurnExecutionContext("late-error", "s")
    async_client = httpx.AsyncClient

    async def fail(request):
        context.cancel()
        raise httpx.ReadTimeout("late timeout", request=request)

    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **kwargs: async_client(
        **kwargs, transport=httpx.MockTransport(fail), trust_env=False,
    ))
    with pytest.raises(ToolCancelledError):
        _invoke(context, "http://mock.test")
