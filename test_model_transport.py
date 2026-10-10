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
from core.local_inference import InferenceDeadlineError, current_inference_deadline, inference_deadline
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
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", state.url + "/forwarded")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            if self.path == "/trickle":
                self.send_response(200)
                self.send_header("Content-Length", "4096")
                self.end_headers()
                state.started.set()
                try:
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(.01)
                except OSError:
                    state.disconnected.set()
                self.close_connection = True
                return
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


@pytest.mark.parametrize("path", ["/headers", "/body", "/trickle"])
@pytest.mark.parametrize("in_event_loop", [False, True])
@pytest.mark.parametrize("has_turn", [False, True])
def test_shared_deadline_closes_pending_io_without_cancelling_parent(local_http, path, in_event_loop, has_turn):
    context = TurnExecutionContext("deadline", "session") if has_turn else None
    def call():
        with inference_deadline(.25):
            return _invoke(context, local_http.url + path, in_event_loop=in_event_loop, timeout=5)
    started = time.monotonic()
    with pytest.raises(InferenceDeadlineError):
        call()
    assert time.monotonic() - started < .8
    assert local_http.started.is_set() and local_http.disconnected.wait(.5)
    assert len(local_http.requests) == 1
    assert context is None or not context.cancelled
    assert current_inference_deadline() is None
    assert not any(t.name == transport._HELPER_THREAD_NAME for t in threading.enumerate())
    assert _invoke(context, local_http.url + "/ok", in_event_loop=in_event_loop).status_code == 200


def test_turn_cancelled_during_deadline_cleanup_still_has_priority(monkeypatch):
    context = TurnExecutionContext("deadline-cleanup", "session")
    real_client = httpx.AsyncClient
    released = threading.Event()
    async def wait(request):
        try:
            await asyncio.Event().wait()
        finally:
            context.cancel()
            released.set()
    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **kw: real_client(
        **kw, transport=httpx.MockTransport(wait), trust_env=False))
    with pytest.raises(ToolCancelledError), inference_deadline(.15):
        _invoke(context, "http://mock.test", in_event_loop=True)
    assert released.is_set() and context.cancelled
    assert current_inference_deadline() is None
    assert not any(t.name == transport._HELPER_THREAD_NAME for t in threading.enumerate())


@pytest.mark.parametrize("mode", ["direct", "deadline", "event_loop"])
def test_local_only_ignores_environment_proxies_and_keeps_response_contract(local_http, monkeypatch, mode):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
        monkeypatch.setenv(key.lower(), "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    def call():
        return transport.post_json(local_http.url.replace("127.0.0.1", "localhost") + "/ok",
                                   json={"prompt": "private fixture"}, timeout=2, local_only=True)

    async def async_call():
        return call()

    if mode == "direct":
        response = call()
    else:
        with inference_deadline(2):
            response = asyncio.run(async_call()) if mode == "event_loop" else call()
    assert isinstance(response, requests.Response)
    assert response.json()["echo"] == {"prompt": "private fixture"}
    assert response.request.url.startswith(local_http.url)
    assert len(local_http.requests) == 1


@pytest.mark.parametrize("mode", ["direct", "deadline", "event_loop"])
def test_local_only_rejects_redirect_without_forwarding_mail_payload(local_http, mode):
    def call():
        return transport.post_json(local_http.url + "/redirect", json={"prompt": "private fixture"},
                                   timeout=2, local_only=True)

    async def async_call():
        return call()

    with pytest.raises(requests.HTTPError) as error:
        if mode == "direct":
            call()
        else:
            with inference_deadline(2):
                asyncio.run(async_call()) if mode == "event_loop" else call()
    assert [path for path, _payload in local_http.requests] == ["/redirect"]
    assert error.value.response is None
    assert "private fixture" not in str(error.value)
    assert local_http.url not in str(error.value)


@pytest.mark.parametrize("url", [
    "https://cloud.example/api/chat", "http://127.0.0.2/api/chat", "http://127.1/api/chat",
    "http://2130706433/api/chat", "http://localhost.example/api/chat", "file:///api/chat",
    "http://user:secret@127.0.0.1/api/chat", "http://127.0.0.1:0/api/chat",
    "http://127.0.0.1:/api/chat", "http://localhost:/api/chat",
    "http://127.0.0.1:65536/api/chat", "http://127.0.0.1:secret/api/chat",
    "http://127.0.0.1/api/chat?secret", "http://127.0.0.1/api/chat#secret",
    "http://127.0.0.1/api/chat?", "http://127.0.0.1/api/chat#",
    "http://127.0.0.1\\@cloud.example/api/chat", "\nhttp://127.0.0.1/api/chat",
    "http://127.0.0.1 /api/chat", None,
])
def test_local_only_refuses_non_loopback_or_ambiguous_url_before_dispatch(monkeypatch, url):
    monkeypatch.setattr(transport.requests, "Session", lambda: pytest.fail("HTTP client created"))
    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **_: pytest.fail("HTTP client created"))
    with pytest.raises(requests.RequestException) as error:
        transport.post_json(url, json={"prompt": "private fixture"}, timeout=1, local_only=True)
    assert "secret" not in str(error.value)
    assert "private fixture" not in str(error.value)


def test_local_only_accepts_explicit_ipv6_loopback():
    assert transport._local_url("http://[::1]:11434/api/chat") == "http://[::1]:11434/api/chat"


def test_ollama_local_only_flag_reaches_transport_and_does_not_expose_http_response(monkeypatch):
    from core.llm import ModelCallError, OllamaClient
    from core.model_registry import get_model_registry
    import core.llm as llm

    client = OllamaClient.__new__(OllamaClient)
    client.base_url, client.model, client.system_prompt = "http://127.0.0.1:11434", "fixture", ""
    client.profile = get_model_registry().resolve("tool_selection")
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        response = requests.Response()
        response.status_code = 500
        response._content = b'{"error":"private fixture mail body"}'
        return response

    monkeypatch.setattr(llm, "post_json", post)
    with pytest.raises(ModelCallError) as error:
        client.chat_structured([{"role": "user", "content": "private fixture mail body"}],
                               {"type": "object"}, local_only=True)
    assert calls[0][1]["local_only"] is True
    assert "private fixture" not in str(error.value)
    assert error.value.__cause__ is None
