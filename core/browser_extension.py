"""Owned, lazy stdio session to the pinned official Playwright extension server.

Only application-authored snippets are accepted by callers. This session is not
registered as a generic code/DOM tool and never copies a browser profile.
"""
from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import Future, TimeoutError as FutureTimeout
import json
from pathlib import Path
import re
import shutil
import threading
import time

from core.mcp_bridge import _bounded
from core.turn_context import check_turn_cancelled


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "data/tools/playwright-mcp/node_modules/@playwright/mcp/cli.js"
VERSION = "0.0.83"
CONNECT_TIMEOUT = 300  # Manual tab approval; normal screen operations stay at 90s.


class BrowserExtensionUnavailableError(RuntimeError):
    """The official server could not find its extension in Chrome."""


class BrowserExtensionCredentialsError(ValueError):
    """Only application-authored credential errors may reach the dialog."""


class BrowserExtensionCredentials:
    PROVIDER = "anis-mail-extension"
    ACCOUNT = "chrome"

    def __init__(self, vault=None):
        self._vault = vault

    @property
    def vault(self):
        if self._vault is None:
            from core.remote_runtime import SecureTokenVault
            self._vault = SecureTokenVault()
        return self._vault

    @staticmethod
    def _validate(token):
        if isinstance(token, str) and token.startswith("PLAYWRIGHT_MCP_EXTENSION_TOKEN="):
            token = token.removeprefix("PLAYWRIGHT_MCP_EXTENSION_TOKEN=")
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or "=" in token
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)):
            raise BrowserExtensionCredentialsError(
                "확장의 연결 토큰만 입력하세요. 공백·제어 문자 또는 너무 긴 값은 사용할 수 없습니다.")
        return token

    def _token(self):
        try:
            record = self.vault.load(self.PROVIDER, self.ACCOUNT)
            if record is None:
                return None
            if (not isinstance(record, dict) or set(record) != {"version", "token"}
                    or type(record["version"]) is not int or record["version"] != 1):
                raise ValueError()
            return self._validate(record["token"])
        except Exception:
            raise BrowserExtensionCredentialsError(
                "암호화된 자동 연결 정보를 확인하지 못했습니다. 연결 토큰을 다시 등록하세요.") from None

    def status(self):
        return {"configured": self._token() is not None}

    def save(self, token):
        token = self._validate(token)
        try:
            self.vault.save(self.PROVIDER, self.ACCOUNT, {"version": 1, "token": token})
        except Exception:
            raise BrowserExtensionCredentialsError(
                "연결 토큰을 Windows 암호화 저장소에 저장하지 못했습니다.") from None
        return {"configured": True}

    def delete(self):
        try:
            self.vault.delete(self.PROVIDER, self.ACCOUNT)
        except Exception:
            raise BrowserExtensionCredentialsError(
                "저장된 자동 연결 정보를 삭제하지 못했습니다.") from None
        return {"configured": False}

    def environment(self):
        token = self._token()
        return {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": token} if token is not None else {}


def _check_result(result):
    if result.is_error:
        if any(getattr(item, "type", "") == "text"
               and "Playwright Extension not found" in item.text
               for item in result.content):
            raise BrowserExtensionUnavailableError(
                "현재 Chrome 프로필에 공식 Playwright 확장이 없습니다. "
                "확장 설치·접근 허용을 확인한 뒤 다시 연결하세요.")
        raise RuntimeError("Chrome 연결/화면 작업에 실패했습니다. 로그인·탭 선택 상태를 확인하세요.")


def decode_result(result) -> dict:
    """Do not confuse echoed JavaScript/snapshots with the snippet's result."""
    _check_result(result)
    for item in result.content:
        if getattr(item, "type", "") != "text":
            continue
        match = re.search(r"### Result\s*\n(.*?)(?=\n### |\Z)", item.text, re.S)
        if match:
            value = json.loads(match.group(1).strip())
            if isinstance(value, dict):
                return value
    raise ValueError("캘린더 서버가 검증 가능한 구조화 결과를 반환하지 않았습니다.")


class BrowserExtensionSession:
    def __init__(self, environment=None, client_name="ANIS Calendar"):
        self._environment = environment
        self._client_name = client_name
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._close_requested = threading.Event()
        self._thread = None
        self._loop = None
        self._client = None
        self._stop = None
        self._ready: Future | None = None
        atexit.register(self.close)

    def _start(self):
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                if self._close_requested.is_set():
                    raise RuntimeError("이전 캘린더 연결이 종료 중입니다. 종료 후 다시 연결하세요.")
                return
            if not SERVER.is_file() or not shutil.which("node"):
                raise RuntimeError("공식 Playwright MCP 또는 Node.js가 없습니다. 캘린더 연결 안내를 확인하세요.")
            package = json.loads(SERVER.with_name("package.json").read_text(encoding="utf-8"))
            if package.get("name") != "@playwright/mcp" or package.get("version") != VERSION:
                raise RuntimeError("검증된 Playwright MCP 버전과 설치본이 다릅니다.")
            self._close_requested.clear()
            self._ready = Future()
            self._thread = threading.Thread(target=self._run, name="anis-calendar-extension", daemon=True)
            self._thread.start()

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException:
            if self._ready and not self._ready.done():
                self._ready.set_exception(RuntimeError("공식 Playwright MCP 세션을 시작하지 못했습니다."))
        finally:
            self._client = None
            self._loop = None

    async def _serve(self):
        with self._lifecycle_lock:
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            if self._close_requested.is_set():
                raise RuntimeError("캘린더 연결 시작 중 종료 요청을 받았습니다.")
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters
        params = StdioServerParameters(command=shutil.which("node"), cwd=str(ROOT), args=[
            str(SERVER), "--extension", "--no-webmcp", "--snapshot-mode=none",
            "--image-responses=omit", "--codegen=none", "--timeout-action=5000",
            "--timeout-navigation=15000",
        ], env=self._environment() if self._environment else None)
        # The installed SDK owns process-tree cleanup and hidden Windows launch.
        from mcp.types import Implementation
        async with Client(params, mode="legacy", read_timeout_seconds=CONNECT_TIMEOUT,
                          client_info=Implementation(name=self._client_name, version="1.0")) as client:
            self._client = client
            if self._close_requested.is_set():
                raise RuntimeError("캘린더 연결 시작 중 종료 요청을 받았습니다.")
            tools = await client.list_tools()
            if not any(tool.name == "browser_run_code_unsafe" for tool in tools.tools):
                raise RuntimeError("공식 서버의 코드 실행 계약이 설치본과 일치하지 않습니다.")
            if self._close_requested.is_set():
                raise RuntimeError("캘린더 연결 시작 중 종료 요청을 받았습니다.")
            self._ready.set_result(True)
            await self._stop.wait()

    @staticmethod
    def _wait(future, deadline, checkpoint):
        while True:
            checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("캘린더 연결 제한시간이 지났습니다. 자동 재실행하지 않습니다.")
            try:
                return future.result(timeout=min(.1, remaining))
            except FutureTimeout:
                if future.done():
                    return future.result()
                continue

    def call(self, code: str, *, checkpoint=check_turn_cancelled, timeout=90):
        return self._call_tool("browser_run_code_unsafe", {"code": code}, decode_result,
                               checkpoint=checkpoint, timeout=timeout)

    def select_tab(self, index: int, *, checkpoint=check_turn_cancelled, timeout=90):
        if type(index) is not int or index < 0:
            raise ValueError("선택할 Chrome 탭 번호가 올바르지 않습니다.")

        def selected(result):
            _check_result(result)
            return {"selected": True, "index": index}

        return self._call_tool("browser_tabs", {"action": "select", "index": index}, selected,
                               checkpoint=checkpoint, timeout=timeout)

    def _call_tool(self, tool, arguments, decode, *, checkpoint, timeout):
        deadline = time.monotonic() + timeout
        while not self._lock.acquire(timeout=.1):
            checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError("다른 캘린더 작업의 종료를 기다리다 제한시간이 지났습니다.")
        future = None
        try:
            checkpoint()
            self._start()
            self._wait(self._ready, deadline, checkpoint)
            if self._loop is None or self._client is None:
                raise RuntimeError("캘린더 세션 연결이 종료됐습니다. 다시 연결하세요.")
            async def invoke():
                result = await _bounded(self._client.call_tool(
                    tool, arguments),
                    max(.01, deadline - time.monotonic()), checkpoint)
                return decode(result)
            future = asyncio.run_coroutine_threadsafe(invoke(), self._loop)
            return self._wait(future, deadline, checkpoint)
        except BaseException:
            # An action may already have reached Chrome; never silently replay.
            if future is not None:
                future.cancel()
            self.close()
            raise
        finally:
            self._lock.release()

    def close(self):
        with self._lifecycle_lock:
            # Keep shutdown intent even before the worker has an asyncio loop.
            self._close_requested.set()
            loop, stop, thread = self._loop, self._stop, self._thread
        if loop and stop and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        # Only our MCP process is disposed. Chrome/user tabs stay open.
