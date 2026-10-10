"""Synchronous model HTTP calls with cooperative, socket-level turn cancellation.

The public result and transport errors retain the ``requests`` contract used by
LLM clients. Calls outside a turn/deadline still use requests directly. A turn
owns an async HTTP task, so cancelling it closes the connection instead of
leaving a blocking generation request running on an abandoned worker thread.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from contextvars import copy_context
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import requests

from core.local_inference import InferenceDeadlineError, check_inference_deadline, current_inference_deadline

from core.turn_context import (
    TurnExecutionContext,
    bind_turn_context,
    current_turn_context,
)


_CANCEL_POLL_SECONDS = 0.025
_HELPER_THREAD_NAME = "model-http-event-loop"


def _local_url(url: str) -> str:
    """Validate before dispatch; never include the supplied URL in errors."""
    try:
        if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url) or any(c in url for c in "\\?#"):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
        authority = ("[::1]" if parsed.hostname == "::1" else parsed.hostname)
        authority += f":{parsed.port}" if parsed.port is not None else ""
        if parsed.netloc.casefold() != authority:
            raise ValueError()
        if parsed.hostname == "localhost":
            # Numeric loopback avoids resolver aliases and cancellation delays.
            authority = "127.0.0.1" + (f":{parsed.port}" if parsed.port is not None else "")
            return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
        return url
    except (ValueError, TypeError):
        raise requests.RequestException("로컬 AI 요청에는 인증 정보 없는 loopback HTTP(S) 주소만 사용할 수 있습니다.") from None


def _reject_redirect(response) -> None:
    if 300 <= response.status_code < 400:
        raise requests.HTTPError("로컬 AI 요청의 주소 이동을 허용하지 않습니다.")


def _as_requests_response(response: httpx.Response) -> requests.Response:
    """Detach the fully read HTTP result from the soon-to-be-closed client."""
    result = requests.Response()
    result.status_code = response.status_code
    result.headers = requests.structures.CaseInsensitiveDict(response.headers)
    result.url = str(response.url)
    result.reason = response.reason_phrase
    result.encoding = requests.utils.get_encoding_from_headers(result.headers)
    result._content = response.content
    result._content_consumed = True
    result.cookies.update(response.cookies.jar)
    result.request = requests.Request(
        method=response.request.method,
        url=str(response.request.url),
        headers=dict(response.request.headers),
        data=response.request.content,
    ).prepare()
    result.history = [_as_requests_response(item) for item in response.history]
    return result


def _httpx_timeout(timeout: Any) -> httpx.Timeout:
    # requests accepts either one inactivity timeout or (connect, read).
    if isinstance(timeout, tuple):
        connect, read = timeout
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect)
    return httpx.Timeout(timeout)


async def _post_for_turn(
    url: str, body: Any, timeout: Any, context: TurnExecutionContext | None,
    *, local_only: bool = False,
) -> requests.Response:
    check_inference_deadline()
    options = {"timeout": _httpx_timeout(timeout), "follow_redirects": not local_only}
    if local_only:
        options["trust_env"] = False
    async with httpx.AsyncClient(**options) as client:
        check_inference_deadline()

        async def send() -> requests.Response:
            # Keep ownership through both header and body reads. The stream
            # context closes the response even when aread() is cancelled.
            check_inference_deadline()
            async with client.stream("POST", url, json=body) as response:
                if local_only:
                    _reject_redirect(response)
                await response.aread()
                check_inference_deadline()
                return _as_requests_response(response)

        request = asyncio.create_task(send(), name="model-http-request")
        try:
            while not request.done():
                await asyncio.wait({request}, timeout=_CANCEL_POLL_SECONDS)
                check_inference_deadline()
            check_inference_deadline()
            return request.result()
        finally:
            if not request.done():
                request.cancel()
            # Join cancellation before closing the client or returning. A
            # cancelled caller must not leave an HTTP task/socket behind.
            await asyncio.gather(request, return_exceptions=True)


def _run_for_turn(
    url: str, body: Any, timeout: Any, context: TurnExecutionContext | None,
    *, local_only: bool = False,
) -> requests.Response:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_post_for_turn(url, body, timeout, context, local_only=local_only))

    # asyncio.run cannot nest in an existing event loop. A single owned helper
    # handles that case; it is joined, never detached on cancellation. Capture
    # the context so both turn cancellation and the deadline travel together.
    outcome: Future[requests.Response] = Future()

    def run() -> None:
        try:
            with bind_turn_context(context):
                outcome.set_result(asyncio.run(_post_for_turn(url, body, timeout, context, local_only=local_only)))
        except BaseException as exc:
            outcome.set_exception(exc)

    captured = copy_context()
    worker = threading.Thread(target=lambda: captured.run(run), name=_HELPER_THREAD_NAME, daemon=False)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(_CANCEL_POLL_SECONDS)
    except BaseException:
        # Also clean up if the synchronous caller itself is interrupted.
        if context is not None:
            context.cancel()
        while worker.is_alive():
            worker.join(_CANCEL_POLL_SECONDS)
        raise
    return outcome.result()


def post_json(url: str, *, json: Any, timeout: Any, local_only: bool = False) -> requests.Response:
    """POST JSON once, cancelling in-flight I/O when its captured turn ends.

    HTTP error statuses remain responses for the caller's raise_for_status().
    Cancellation raises ToolCancelledError and takes precedence over a late
    transport error or inference deadline. A deadline closes only this request,
    not its parent turn. Disconnecting does not promise server-side rollback or
    immediate GPU release; that remains the model server's responsibility.
    OS hostname resolution can also delay cancellation: asyncio joins an
    already-running getaddrinfo worker before closing its event loop. Numeric
    IP endpoints avoid that DNS stage; no resolver/HTTP worker is abandoned.
    local_only also rejects non-loopback URLs, redirects and environment proxies.
    """
    context = current_turn_context()
    if local_only:
        url = _local_url(url)
    if context is None and current_inference_deadline() is None:
        if local_only:
            with requests.Session() as client:
                client.trust_env = False
                response = client.post(url, json=json, timeout=timeout, allow_redirects=False, stream=True)
                try:
                    _reject_redirect(response)
                    response.content  # Detach the response body before closing the session.
                    return response
                finally:
                    response.close()
        return requests.post(url, json=json, timeout=timeout)

    check_inference_deadline()
    try:
        response = _run_for_turn(url, json, timeout, context, local_only=local_only)
    except InferenceDeadlineError:
        if context is not None:
            context.checkpoint()
        raise
    except httpx.TimeoutException as exc:
        check_inference_deadline()
        raise requests.Timeout(str(exc)) from exc
    except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
        check_inference_deadline()
        raise requests.ConnectionError(str(exc)) from exc
    except httpx.TooManyRedirects as exc:
        check_inference_deadline()
        raise requests.TooManyRedirects(str(exc)) from exc
    except httpx.HTTPError as exc:
        check_inference_deadline()
        raise requests.RequestException(str(exc)) from exc
    check_inference_deadline()
    return response
