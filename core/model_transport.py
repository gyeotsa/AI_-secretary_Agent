"""Synchronous model HTTP calls with cooperative, socket-level turn cancellation.

The public result and transport errors retain the ``requests`` contract used by
LLM clients. Calls outside a turn still use requests directly. An active turn
owns an async HTTP task, so cancelling it closes the connection instead of
leaving a blocking generation request running on an abandoned worker thread.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import threading
from typing import Any

import httpx
import requests

from core.turn_context import (
    TurnExecutionContext,
    bind_turn_context,
    current_turn_context,
)


_CANCEL_POLL_SECONDS = 0.025
_HELPER_THREAD_NAME = "model-http-event-loop"


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
    url: str, body: Any, timeout: Any, context: TurnExecutionContext,
) -> requests.Response:
    context.checkpoint()
    async with httpx.AsyncClient(
        timeout=_httpx_timeout(timeout), follow_redirects=True,
    ) as client:
        context.checkpoint()

        async def send() -> requests.Response:
            # Keep ownership through both header and body reads. The stream
            # context closes the response even when aread() is cancelled.
            context.checkpoint()
            async with client.stream("POST", url, json=body) as response:
                await response.aread()
                context.checkpoint()
                return _as_requests_response(response)

        request = asyncio.create_task(send(), name="model-http-request")
        try:
            while not request.done():
                await asyncio.wait({request}, timeout=_CANCEL_POLL_SECONDS)
                context.checkpoint()
            context.checkpoint()
            return request.result()
        finally:
            if not request.done():
                request.cancel()
            # Join cancellation before closing the client or returning. A
            # cancelled caller must not leave an HTTP task/socket behind.
            await asyncio.gather(request, return_exceptions=True)


def _run_for_turn(
    url: str, body: Any, timeout: Any, context: TurnExecutionContext,
) -> requests.Response:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_post_for_turn(url, body, timeout, context))

    # asyncio.run cannot nest in an existing event loop. A single owned helper
    # handles that case; it is joined, never detached on cancellation. Capture
    # and bind the caller's turn explicitly because ContextVars do not travel
    # automatically to a new thread.
    outcome: Future[requests.Response] = Future()

    def run() -> None:
        try:
            with bind_turn_context(context):
                outcome.set_result(asyncio.run(_post_for_turn(url, body, timeout, context)))
        except BaseException as exc:
            outcome.set_exception(exc)

    worker = threading.Thread(target=run, name=_HELPER_THREAD_NAME, daemon=False)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(_CANCEL_POLL_SECONDS)
    except BaseException:
        # Also clean up if the synchronous caller itself is interrupted.
        context.cancel()
        while worker.is_alive():
            worker.join(_CANCEL_POLL_SECONDS)
        raise
    return outcome.result()


def post_json(url: str, *, json: Any, timeout: Any) -> requests.Response:
    """POST JSON once, cancelling in-flight I/O when its captured turn ends.

    HTTP error statuses remain responses for the caller's raise_for_status().
    Cancellation raises ToolCancelledError and takes precedence over a late
    transport error. Disconnecting does not promise server-side rollback or
    immediate GPU release; that remains the model server's responsibility.
    OS hostname resolution can also delay cancellation: asyncio joins an
    already-running getaddrinfo worker before closing its event loop. Numeric
    IP endpoints avoid that DNS stage; no resolver/HTTP worker is abandoned.
    """
    context = current_turn_context()
    if context is None:
        return requests.post(url, json=json, timeout=timeout)

    context.checkpoint()
    try:
        response = _run_for_turn(url, json, timeout, context)
    except httpx.TimeoutException as exc:
        context.checkpoint()
        raise requests.Timeout(str(exc)) from exc
    except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as exc:
        context.checkpoint()
        raise requests.ConnectionError(str(exc)) from exc
    except httpx.TooManyRedirects as exc:
        context.checkpoint()
        raise requests.TooManyRedirects(str(exc)) from exc
    except httpx.HTTPError as exc:
        context.checkpoint()
        raise requests.RequestException(str(exc)) from exc
    context.checkpoint()
    return response
