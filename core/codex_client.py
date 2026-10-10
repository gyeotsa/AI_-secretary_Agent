"""ChatGPT-managed Codex inference; Anis remains the only tool executor."""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import copy
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid
import webbrowser

from jsonschema import Draft202012Validator

from core import auxiliary_models
from core.assistant_settings import get_assistant_settings
from core.llm import BaseLLMClient, ModelCallError, ProseResponse
from core.local_inference import (
    InferenceDeadlineError, check_inference_deadline, current_inference_deadline, inference_deadline,
)
from core.model_registry import get_model_registry
from core.plugin import ToolCancelledError


class CodexRuntimeError(RuntimeError):
    """Safe details only: protocol errors and stderr can contain credentials."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "hooks",
    "browser_use", "computer_use", "multi_agent", "image_generation", "view_image",
    "code_mode", "code_mode_host", "memories", "goals", "skill_search",
    "skill_mcp_dependency_install", "sleep_tool", "workspace_dependencies",
)
_SAFE_CONFIG = {
    **{f"features.{name}": False for name in _DISABLED_FEATURES},
    "web_search": "disabled", "tools.view_image": False, "notify": [],
    "project_doc_max_bytes": 0, "agents.enabled": False,
    "model_provider": "openai",
}
_APPROVAL_POLICY = {"granular": {
    "sandbox_approval": False, "rules": False, "mcp_elicitations": False,
    "request_permissions": False, "skill_approval": False,
}}
_PASSIVE_ITEMS = {"userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"}


def _codex_command():
    """Use the native npm binary on Windows, without launching a command shell."""
    executable = shutil.which("codex.exe") or shutil.which("codex")
    if not executable:
        raise CodexRuntimeError("unavailable", "Codex CLI를 설치한 뒤 다시 연결해 주세요.")
    path = Path(executable)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".ps1", ".bat"}:
        package = path.parent / "node_modules" / "@openai" / "codex"
        for binary in (package / "node_modules" / "@openai").glob("codex-win32-*/vendor/*/bin/codex.exe"):
            return [str(binary)]
        for binary in (package / "vendor").glob("*/bin/codex.exe"):
            return [str(binary)]
        raise CodexRuntimeError("unavailable", "Codex CLI의 Windows 실행 파일을 찾을 수 없습니다.")
    return [str(path)]


class CodexRuntime:
    def __init__(self, *, settings=None, process_factory=None):
        self._settings = settings
        self._process_factory = process_factory or subprocess.Popen
        self._lock = threading.RLock()
        self._shutdown_requested = threading.Event()
        self._process = None
        self._events = queue.Queue()
        self._next_id = 0
        self._pending = []
        self._models = []
        self._selected_model = ""
        self._authenticated = False
        self._account_label = ""
        self._reason = "ChatGPT 연결 상태를 확인해 주세요."
        self._thread_config = dict(_SAFE_CONFIG)
        self._cwd = None

    @property
    def settings(self):
        return self._settings or get_assistant_settings()

    @property
    def selected_model(self):
        return self._selected_model

    def status(self):
        return self._authenticated, self._reason

    def _checkpoint(self, deadline, cancellation_check=None):
        if self._shutdown_requested.is_set():
            raise ToolCancelledError("아니스가 종료되어 Codex 요청을 중단했습니다.")
        if cancellation_check:
            cancellation_check()
        check_inference_deadline()
        if time.monotonic() >= deadline:
            raise CodexRuntimeError("timeout", "Codex 응답 시간이 초과되었습니다.")

    @contextmanager
    def _lease(self, deadline, cancellation_check=None):
        while True:
            self._checkpoint(deadline, cancellation_check)
            if self._lock.acquire(timeout=0.05):
                break
        try:
            yield
        except BaseException:
            self._stop()
            raise
        finally:
            self._lock.release()

    def _start(self, deadline, cancellation_check=None):
        if self._process and self._process.poll() is None:
            return
        self._stop()
        self._cwd = tempfile.TemporaryDirectory(prefix="anis-codex-")
        command = _codex_command() + ["app-server", "--stdio"]
        for key, value in _SAFE_CONFIG.items():
            command.extend(["-c", f"{key}={json.dumps(value)}"])
        # API credentials cannot silently change subscription calls into paid API calls.
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY"}}
        try:
            self._process = self._process_factory(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                cwd=self._cwd.name, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, ValueError):
            raise CodexRuntimeError("unavailable", "Codex 엔진을 시작할 수 없습니다.") from None
        events = self._events
        process = self._process

        def read():
            try:
                for line in process.stdout:
                    if len(line) > 8_000_000:
                        events.put(None)
                        return
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        events.put(None)
                        return
                    events.put(event if isinstance(event, dict) else None)
            except (OSError, ValueError):
                pass
            finally:
                events.put(None)

        threading.Thread(target=read, name="AnisCodexProtocol", daemon=True).start()
        self._rpc("initialize", {
            "clientInfo": {"name": "anis", "title": "Anis", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        }, deadline, cancellation_check)
        self._send({"method": "initialized", "params": {}})
        config = self._rpc("config/read", {"includeLayers": False}, deadline, cancellation_check)
        effective = config.get("config", {})
        provider = (effective.get("model_providers") or {}).get("openai") or {}
        for endpoint in (effective.get("openai_base_url"), provider.get("base_url")):
            if endpoint:
                address = urlparse(endpoint)
                if (address.scheme != "https" or address.hostname not in {"chatgpt.com", "api.openai.com"}
                        or address.username or address.password or address.query or address.fragment):
                    raise CodexRuntimeError("provider", "GPT 연결은 공식 OpenAI 서버만 사용할 수 있습니다.")
        if (provider.get("env_key") or provider.get("experimental_bearer_token")
                or provider.get("requires_openai_auth") is False):
            raise CodexRuntimeError("authentication", "GPT 연결에는 ChatGPT 계정 인증이 필요합니다.")
        self._thread_config = dict(_SAFE_CONFIG)
        for server in effective.get("mcp_servers", {}):
            if not re.fullmatch(r"[A-Za-z0-9_-]+", server):
                raise CodexRuntimeError("tools_disabled", "Codex MCP 도구 차단 설정을 적용할 수 없습니다.")
            self._thread_config[f"mcp_servers.{server}.enabled"] = False
        # Fail closed if managed configuration has kept a native executor enabled.
        features = effective.get("features", {})
        if any(features.get(name) is True for name in _DISABLED_FEATURES):
            raise CodexRuntimeError("tools_disabled", "Codex 도구 차단 설정을 적용할 수 없어 연결을 중단했습니다.")

    def _send(self, payload):
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise CodexRuntimeError("connection", "Codex 엔진과의 연결이 끊어졌습니다.") from None

    def _event(self, deadline, cancellation_check=None):
        while True:
            self._checkpoint(deadline, cancellation_check)
            try:
                event = self._events.get(timeout=0.05)
            except queue.Empty:
                continue
            if event is None:
                raise CodexRuntimeError("connection", "Codex 엔진과의 연결이 끊어졌습니다.")
            if "method" in event and "id" in event:
                # The engine may never acquire approval, credentials or tool results from Anis.
                self._send({"id": event["id"], "error": {
                    "code": -32601, "message": "Anis inference engine does not execute tools",
                }})
                raise CodexRuntimeError("native_tool", "Codex 자체 도구 실행 요청을 차단했습니다.")
            return event

    def _rpc(self, method, params, deadline, cancellation_check=None):
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            event = self._event(deadline, cancellation_check)
            if event.get("id") == request_id:
                if "error" in event:
                    raise CodexRuntimeError("protocol", "Codex 요청에 실패했습니다. 연결과 모델 접근 권한을 확인해 주세요.")
                result = event.get("result")
                if not isinstance(result, dict):
                    raise CodexRuntimeError("protocol", "Codex가 올바르지 않은 응답을 반환했습니다.")
                return result
            if "method" in event:
                self._pending.append(event)

    def _account(self, deadline, cancellation_check=None):
        result = self._rpc("account/read", {"refreshToken": False}, deadline, cancellation_check)
        account = result.get("account") or {}
        self._authenticated = account.get("type") == "chatgpt"
        self._account_label = (f"ChatGPT · {account.get('planType') or '구독'}"
                               if self._authenticated else "")
        self._reason = ("ChatGPT 구독으로 연결됨" if self._authenticated
                        else "ChatGPT 계정으로 로그인해 주세요. API 키 인증은 사용할 수 없습니다.")

    def _discover(self, deadline, cancellation_check=None):
        self._start(deadline, cancellation_check)
        self._account(deadline, cancellation_check)
        models = []
        cursor = None
        cursors = set()
        while True:
            result = self._rpc("model/list", {
                "limit": 100, "includeHidden": False, **({"cursor": cursor} if cursor else {}),
            }, deadline, cancellation_check)
            models.extend(model for model in result.get("data", [])
                          if isinstance(model, dict) and isinstance(model.get("model"), str)
                          and not model.get("hidden"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in cursors or len(cursors) >= 20:
                raise CodexRuntimeError("protocol", "Codex 모델 목록을 읽을 수 없습니다.")
            cursors.add(cursor)
        if not models:
            raise CodexRuntimeError("model_unavailable", "선택할 수 있는 Codex 모델이 없습니다.")
        self._models = models
        persisted = self.settings.get("codex_model")
        available = {model["model"] for model in models}
        default = next((model["model"] for model in models if model.get("isDefault")), models[0]["model"])
        self._selected_model = persisted if persisted in available else default
        return self._snapshot()

    def _snapshot(self):
        return {"authenticated": self._authenticated, "account_label": self._account_label,
                "models": copy.deepcopy(self._models), "selected_model": self._selected_model}

    def discover(self):
        deadline = time.monotonic() + 30
        with self._lease(deadline):
            return self._discover(deadline)

    def set_model(self, model):
        if self._models and model not in {entry["model"] for entry in self._models}:
            raise CodexRuntimeError("model_unavailable", "현재 계정에서 사용할 수 없는 Codex 모델입니다.")
        # Invalidate before waiting for the runtime lock so an active old-model turn stops.
        if model != self._selected_model:
            auxiliary_models.invalidate()
        deadline = time.monotonic() + 30
        with self._lease(deadline):
            if not self._models:
                self._discover(deadline)
            if model not in {entry["model"] for entry in self._models}:
                raise CodexRuntimeError("model_unavailable", "현재 계정에서 사용할 수 없는 Codex 모델입니다.")
            self.settings.set("codex_model", model)
            self._selected_model = model
            return model

    def login(self):
        deadline = time.monotonic() + 180
        with self._lease(deadline):
            self._start(deadline)
            self._account(deadline)
            if self._authenticated:
                return self._discover(deadline)
            result = self._rpc("account/login/start", {"type": "chatgpt"}, deadline)
            login_id = result.get("loginId")
            url = result.get("authUrl", "")
            parsed = urlparse(url)
            if not login_id or parsed.scheme != "https" or parsed.hostname not in {"auth.openai.com", "chatgpt.com"}:
                raise CodexRuntimeError("authentication", "ChatGPT 로그인 주소를 확인할 수 없습니다.")
            if not webbrowser.open(url):
                raise CodexRuntimeError("authentication", "ChatGPT 로그인 브라우저를 열 수 없습니다.")
            while True:
                event = self._pending.pop(0) if self._pending else self._event(deadline)
                params = event.get("params", {})
                if event.get("method") == "account/login/completed" and params.get("loginId") == login_id:
                    if not params.get("success"):
                        raise CodexRuntimeError("authentication", "ChatGPT 로그인이 완료되지 않았습니다.")
                    result = self._discover(deadline)
                    if not result["authenticated"]:
                        raise CodexRuntimeError("authentication", "ChatGPT 인증된 계정을 확인하지 못했습니다.")
                    return result

    def complete(self, instructions, inputs, *, schema=None, timeout=120, cancellation_check=None):
        deadline = time.monotonic() + timeout
        with self._lease(deadline, cancellation_check):
            self._start(deadline, cancellation_check)
            self._account(deadline, cancellation_check)
            if not self._authenticated:
                raise CodexRuntimeError("authentication", self._reason)
            if not self._models:
                self._discover(deadline, cancellation_check)
            self._pending.clear()
            model = next(model for model in self._models if model["model"] == self._selected_model)
            started = self._rpc("thread/start", {
                "model": self._selected_model, "modelProvider": "openai",
                "allowProviderModelFallback": False, "ephemeral": True,
                "cwd": self._cwd.name, "sandbox": "read-only", "approvalPolicy": _APPROVAL_POLICY,
                "baseInstructions": instructions, "developerInstructions":
                "Return only the requested response. No native tool calls. Anis executes all actions.",
                "dynamicTools": [], "environments": [], "config": self._thread_config,
            }, deadline, cancellation_check)
            resolved_model = started.get("model", "")
            if started.get("modelProvider") != "openai" or resolved_model != self._selected_model:
                raise CodexRuntimeError("model_unavailable", "Codex가 선택한 모델을 적용하지 못했습니다.")
            thread_id = started.get("thread", {}).get("id")
            if not thread_id:
                raise CodexRuntimeError("protocol", "Codex 대화를 시작할 수 없습니다.")
            turn = self._rpc("turn/start", {
                "threadId": thread_id, "input": inputs,
                "effort": model.get("defaultReasoningEffort"),
                **({"outputSchema": schema} if schema is not None else {}),
            }, deadline, cancellation_check)
            turn_id = turn.get("turn", {}).get("id")
            messages = {}
            while True:
                event = self._pending.pop(0) if self._pending else self._event(deadline, cancellation_check)
                params = event.get("params", {})
                if params.get("threadId") != thread_id:
                    continue
                if event.get("method") in {"item/started", "item/completed"}:
                    item = params.get("item", {})
                    if item.get("type") not in _PASSIVE_ITEMS:
                        raise CodexRuntimeError("native_tool", "Codex 자체 도구 실행을 차단했습니다.")
                    if event["method"] == "item/completed" and item.get("type") == "agentMessage":
                        if item.get("phase") != "commentary":
                            messages[item.get("id")] = item.get("text", "")
                if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    completed = params["turn"]
                    if completed.get("status") != "completed":
                        info = (completed.get("error") or {}).get("codexErrorInfo")
                        code = info if isinstance(info, str) else "provider"
                        safe = {"usageLimitExceeded": "ChatGPT 구독 사용 한도에 도달했습니다.",
                                "rateLimitExceeded": "Codex 요청 한도에 도달했습니다.",
                                "unauthorized": "ChatGPT 로그인을 다시 확인해 주세요."}
                        raise CodexRuntimeError(code, safe.get(code, "Codex 응답이 완료되지 않았습니다."))
                    for item in completed.get("items", []):
                        if item.get("type") not in _PASSIVE_ITEMS:
                            raise CodexRuntimeError("native_tool", "Codex 자체 도구 실행을 차단했습니다.")
                        if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                            messages[item.get("id")] = item.get("text", "")
                    text = "\n".join(messages.values())
                    if not text.strip():
                        raise CodexRuntimeError("empty_output", "Codex가 답변을 반환하지 않았습니다.")
                    self._checkpoint(deadline, cancellation_check)
                    # Ephemeral threads are unloaded without saving conversation history.
                    self._rpc("thread/unsubscribe", {"threadId": thread_id}, deadline, cancellation_check)
                    return text, resolved_model

    def _stop(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError, AttributeError):
                    pass
        self._events = queue.Queue()
        self._pending.clear()
        if self._cwd:
            self._cwd.cleanup()
            self._cwd = None

    def shutdown(self):
        self._shutdown_requested.set()
        with self._lock:
            self._stop()


_runtime = None
_runtime_lock = threading.Lock()


def get_codex_runtime():
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = CodexRuntime()
            atexit.register(_runtime.shutdown)
        return _runtime


def _prepare_messages(messages, system_prompt):
    systems = [str(message.get("content", "")) for message in messages if message.get("role") == "system"]
    instructions = "\n\n".join(systems) if systems else system_prompt
    transcript = []
    images = []
    for message in messages:
        if message.get("role") == "system":
            continue
        converted = dict(message)
        content = converted.get("content", "")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        images.append({"type": "image", "url":
                                       f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"})
                    elif source.get("url"):
                        images.append({"type": "image", "url": source["url"]})
                else:
                    blocks.append(block)
            converted["content"] = blocks
        for data in converted.pop("images", []) or []:
            images.append({"type": "image", "url": data if str(data).startswith("data:")
                           else "data:image/png;base64," + str(data)})
        transcript.append(converted)
    inputs = [{"type": "text", "text":
               "Conversation transcript (JSON with original roles and tool results):\n"
               + json.dumps(transcript, ensure_ascii=False)}]
    return instructions, inputs + images


class CodexClient(BaseLLMClient):
    def __init__(self, role="default", *, cancellation_check=None, runtime=None):
        super().__init__()
        self.role = role
        self.profile = get_model_registry().resolve(role)
        self.runtime = runtime or get_codex_runtime()
        self.cancellation_check = cancellation_check
        self._last_model = ""

    @property
    def model(self):
        return self.runtime.selected_model or self._last_model or "codex-default"

    def chat(self, messages):
        return self.chat_structured(messages)

    def chat_prose(self, messages, **limits):
        text = self.chat_structured(messages, **limits)
        return ProseResponse(text, finish_reason="completed", provider="codex", model=self._last_model)

    def chat_structured(self, messages, json_schema=None, *, context_window=None,
                        request_timeout=None, max_output_tokens=None, local_only=False):
        if local_only:
            raise ModelCallError("codex", self.model, "local_only", "GPT는 ChatGPT 클라우드 모델입니다.")
        for value, name in ((context_window, "context_window"), (max_output_tokens, "max_output_tokens")):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        timeout = 120 if request_timeout is None else request_timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("request_timeout must be a positive finite number")
        instructions, inputs = _prepare_messages(messages, self.system_prompt)
        generation = auxiliary_models.ticket()

        def checkpoint():
            if not auxiliary_models.is_current(generation):
                raise ToolCancelledError("GPT 설정이 변경되어 이전 모델 요청을 중단했습니다.")
            if self.cancellation_check:
                self.cancellation_check()

        # shortcut: app-server has no output-token cap, callers' caps remain advisory until the protocol supports one.
        if max_output_tokens is not None:
            instructions += f"\nKeep the answer within approximately {max_output_tokens} output tokens."
        enclosing_deadline = current_inference_deadline()
        owns_deadline = False
        started = time.perf_counter()
        status, error_code, model = "failed", "", self.model
        try:
            with inference_deadline(timeout):
                owns_deadline = (enclosing_deadline is None
                                 or current_inference_deadline() < enclosing_deadline)
                text, self._last_model = self.runtime.complete(
                    instructions, inputs, schema=json_schema, timeout=timeout,
                    cancellation_check=checkpoint,
                )
                if json_schema is not None:
                    try:
                        valid = Draft202012Validator(json_schema).is_valid(json.loads(text))
                    except (ValueError, TypeError):
                        valid = False
                    if not valid:
                        raise CodexRuntimeError("invalid_output", "Codex 구조화 응답 형식이 올바르지 않습니다.")
                checkpoint()
                status, model = "success", self._last_model
                return text
        except InferenceDeadlineError:
            status, error_code = "failed", "timeout" if owns_deadline else "inference_deadline_exceeded"
            if not owns_deadline:
                raise
            raise ModelCallError("codex", self.model, "timeout", "Codex 응답 시간이 초과되었습니다.",
                                 retryable=False) from None
        except ToolCancelledError:
            status, error_code = "cancelled", "cancelled"
            raise
        except CodexRuntimeError as error:
            status, error_code = "failed", error.code
            raise ModelCallError("codex", self.model, error.code, str(error), retryable=False) from None
        except ModelCallError as error:
            status, error_code = "failed", error.code
            raise
        except Exception as error:
            status, error_code = "failed", type(error).__name__
            raise
        finally:
            try:
                from core.productization import TRACE
                TRACE.emit("model.codex.call", role=self.role, model=model, status=status,
                           error_code=error_code,
                           duration_ms=round((time.perf_counter() - started) * 1000, 2))
            except OSError:
                pass

    def chat_with_tools(self, messages, allowed_tool_names=None):
        tools = {tool["name"]: tool for tool in self.tools
                 if allowed_tool_names is None or tool["name"] in allowed_tool_names}
        if not tools:
            return self.chat(messages), []
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"text": {"type": "string"}, "tool_calls": {"type": "array",
                      "items": {"type": "object", "additionalProperties": False,
                                "properties": {"name": {"type": "string", "enum": list(tools)},
                                               "arguments_json": {"type": "string"}},
                                "required": ["name", "arguments_json"]}}},
                  "required": ["text", "tool_calls"]}
        planning = {"role": "system", "content":
                    ("\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
                     or self.system_prompt) + "\nPlan Anis tool calls without executing them. "
                    "Use tool_calls=[] for an answer. arguments_json must be a JSON object matching the tool input_schema. "
                    "Never claim execution before tool results appear. Available Anis tools:\n"
                    + json.dumps(list(tools.values()), ensure_ascii=False)}
        text = self.chat_structured([planning] + [m for m in messages if m.get("role") != "system"], schema)
        result = json.loads(text)
        calls = []
        for call in result["tool_calls"]:
            try:
                arguments = json.loads(call["arguments_json"])
                tool = tools[call["name"]]
                valid = isinstance(arguments, dict) and Draft202012Validator(tool["input_schema"]).is_valid(arguments)
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise ModelCallError("codex", self.model, "invalid_tool", "Codex 도구 계획의 입력이 올바르지 않습니다.")
            calls.append({"type": "tool_use", "id": "codex_" + uuid.uuid4().hex,
                          "name": call["name"], "input": arguments})
        return result["text"], calls
