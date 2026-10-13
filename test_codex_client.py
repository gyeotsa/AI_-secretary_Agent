"""Protocol checks without credentials, network or native Codex tools."""
import json
import queue
import subprocess

import pytest

from core.codex_client import (
    CodexClient, CodexRuntime, CodexRuntimeError, _DISABLED_FEATURES,
    _prepare_messages,
)
from core.llm import ModelCallError
from core.local_inference import InferenceDeadlineError, inference_deadline
from core.plugin import ToolCancelledError


MODELS = [
    {"model": "gpt-a", "displayName": "GPT A", "isDefault": False,
     "defaultReasoningEffort": "low"},
    {"model": "gpt-b", "displayName": "GPT B", "isDefault": True,
     "defaultReasoningEffort": "medium"},
]


class Settings:
    def __init__(self, model="auto"):
        self.model = model

    def get(self, key):
        assert key == "codex_model"
        return self.model

    def set(self, key, value):
        assert key == "codex_model"
        self.model = value


class Output:
    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        value = self.lines.get(timeout=5)
        if value is None:
            raise StopIteration
        return value

    def close(self):
        self.lines.put(None)


class Input:
    def __init__(self, process):
        self.process = process

    def write(self, line):
        self.process.handle(json.loads(line))

    def flush(self):
        pass

    def close(self):
        pass


class Process:
    def __init__(self, *, account="chatgpt", answer="Hello", failure=None,
                 native=False, stalled=False, provider="openai", config_overrides=None,
                 rpc_results=None, rpc_errors=None, stalled_methods=()):
        self.stdout = Output()
        self.stdin = Input(self)
        self.returncode = None
        self.calls = []
        self.account, self.answer, self.failure = account, answer, failure
        self.native, self.stalled, self.provider = native, stalled, provider
        self.config_overrides = config_overrides or {}
        self.rpc_results = rpc_results or {}
        self.rpc_errors = rpc_errors or {}
        self.stalled_methods = stalled_methods
        self.terminated = False

    def emit(self, payload):
        self.stdout.lines.put(json.dumps(payload))

    def handle(self, payload):
        self.calls.append(payload)
        method, request_id = payload.get("method"), payload.get("id")
        if request_id is None or not method:
            return
        if method in self.stalled_methods:
            return
        if method in self.rpc_errors:
            self.emit({"id": request_id, "error": self.rpc_errors[method]})
            return
        params = payload.get("params", {})
        result = {}
        if method == "config/read":
            result = {"config": {"features": {name: False for name in _DISABLED_FEATURES},
                                 "mcp_servers": {"personal": {"enabled": True}}, **self.config_overrides}}
        elif method == "account/read":
            result = {"account": {"type": self.account, "planType": "plus"}}
        elif method == "model/list":
            result = {"data": MODELS, "nextCursor": None}
        elif method == "thread/start":
            result = {"thread": {"id": "thread"}, "model": params["model"],
                      "modelProvider": self.provider}
        elif method == "turn/start":
            result = {"turn": {"id": "turn"}}
            if self.native:
                self.emit({"method": "item/started", "params": {
                    "threadId": "thread", "turnId": "turn",
                    "item": {"id": "native", "type": "commandExecution"},
                }})
            if not self.stalled:
                item = {"id": "answer", "type": "agentMessage", "text": self.answer,
                        "phase": "final_answer"}
                # Notifications arriving before the RPC result must survive request dispatch.
                self.emit({"method": "item/completed", "params": {
                    "threadId": "thread", "turnId": "turn", "item": item,
                }})
                self.emit({"method": "turn/completed", "params": {
                    "threadId": "thread", "turn": {"id": "turn", "items": [item],
                       "status": "failed" if self.failure else "completed",
                       "error": self.failure},
                }})
        self.emit({"id": request_id, "result": self.rpc_results.get(method, result)})

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self.stdout.close()

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def runtime_factory(monkeypatch, tmp_path):
    monkeypatch.setattr("core.codex_client._codex_command", lambda: ["codex.exe"])
    import tempfile
    original = tempfile.TemporaryDirectory
    monkeypatch.setattr("core.codex_client.tempfile.TemporaryDirectory",
                        lambda **kwargs: original(dir=tmp_path, **kwargs))
    runtimes = []

    def create(**options):
        process = Process(**options)
        invocation = {}

        def popen(command, **kwargs):
            invocation.update(command=command, **kwargs)
            return process

        runtime = CodexRuntime(settings=Settings(), process_factory=popen)
        runtimes.append(runtime)
        return runtime, process, invocation

    yield create
    for runtime in runtimes:
        runtime.shutdown()


def test_discovery_uses_returned_default_and_persists_only_model(runtime_factory):
    runtime, process, invocation = runtime_factory()
    assert runtime.status()[0] is False
    snapshot = runtime.discover()
    assert snapshot["authenticated"] is True
    assert snapshot["account_label"] == "ChatGPT · plus"
    assert snapshot["selected_model"] == "gpt-b"
    runtime.set_model("gpt-a")
    assert runtime.discover()["selected_model"] == "gpt-a"
    with pytest.raises(CodexRuntimeError):
        runtime.set_model("unknown")
    assert runtime.settings.model == "gpt-a"
    assert invocation["stderr"] == subprocess.DEVNULL
    assert "forced_login_method" not in " ".join(invocation["command"])


def test_usage_prefers_multiple_buckets_and_only_reads_account_counters(runtime_factory):
    from datetime import datetime
    from core.local_calendar import CALENDAR_TIMEZONE

    today = datetime.now(CALENDAR_TIMEZONE).date().isoformat()
    runtime, process, _ = runtime_factory(rpc_results={
        "account/rateLimits/read": {
            "rateLimits": {"limitId": "legacy", "primary": {"usedPercent": 90}},
            "rateLimitsByLimitId": {
                "codex": {"limitName": "Codex", "primary": {
                    "usedPercent": 120, "windowDurationMins": 300, "resetsAt": 1234},
                    "secondary": {"usedPercent": -5, "windowDurationMins": 10080}},
                "codex_other": {"limitName": "Other", "primary": {"usedPercent": 0}},
            }, "accountId": "private-account", "credits": {"secret": "private-credit"},
        },
        "account/usage/read": {"summary": {"lifetimeTokens": 0},
                               "dailyUsageBuckets": [{"startDate": today, "tokens": 0}]},
    })
    result = runtime.usage()
    assert result["authenticated"] is True
    assert result["error_code"] == ""
    assert [bucket["limit_id"] for bucket in result["rate_limits"]] == ["codex", "codex_other"]
    assert result["rate_limits"][0]["primary"] == {
        "used_percent": 100, "remaining_percent": 0, "window_duration_mins": 300, "resets_at": 1234}
    assert result["rate_limits"][0]["secondary"]["remaining_percent"] == 100
    assert result["token_usage"] == {"lifetime_tokens": 0, "today_tokens": 0}
    assert isinstance(result["fetched_at"], float)
    methods = [call.get("method") for call in process.calls]
    assert methods == ["initialize", "initialized", "config/read", "account/read",
                       "account/rateLimits/read", "account/usage/read"]
    assert "private" not in json.dumps(result)
    assert next(call for call in process.calls if call.get("method") == "account/read")["params"] == {"refreshToken": False}


def test_usage_legacy_fallback_keeps_missing_windows_unknown(runtime_factory):
    runtime, process, _ = runtime_factory(rpc_results={
        "account/rateLimits/read": {"rateLimitsByLimitId": {}, "rateLimits": {
            "limitId": "codex", "primary": None, "secondary": {"usedPercent": 0}}},
        "account/usage/read": {"summary": None, "dailyUsageBuckets": None},
    })
    result = runtime.usage()
    assert result["rate_limits"][0]["primary"] is None
    assert result["rate_limits"][0]["secondary"]["used_percent"] == 0
    assert result["rate_limits"][0]["secondary"]["resets_at"] is None
    assert result["token_usage"] == {"lifetime_tokens": None, "today_tokens": None}


def test_usage_rejects_malformed_numbers_and_redacts_labels(runtime_factory):
    from datetime import datetime
    from core.local_calendar import CALENDAR_TIMEZONE

    today = datetime.now(CALENDAR_TIMEZONE).date().isoformat()
    runtime, process, _ = runtime_factory(rpc_results={
        "account/rateLimits/read": {"rateLimitsByLimitId": {
            "codex": {"limitName": "password=private-secret user@example.com", "primary": {
                "usedPercent": float("nan"), "windowDurationMins": True, "resetsAt": float("inf")},
                "secondary": {"usedPercent": "40", "windowDurationMins": -1, "resetsAt": -1}},
            "broken": "private-invalid-bucket",
        }},
        "account/usage/read": {"summary": {"lifetimeTokens": True},
                               "dailyUsageBuckets": [{"startDate": today, "tokens": -1}]},
    })
    result = runtime.usage()
    assert len(result["rate_limits"]) == 1
    assert all(value is None for value in result["rate_limits"][0]["primary"].values())
    assert all(value is None for value in result["rate_limits"][0]["secondary"].values())
    assert result["token_usage"] == {"lifetime_tokens": None, "today_tokens": None}
    encoded = json.dumps(result, allow_nan=False)
    assert "private" not in encoded and "user@example.com" not in encoded


@pytest.mark.parametrize("endpoint", ["account/rateLimits/read", "account/usage/read"])
def test_usage_unsupported_endpoint_preserves_other_result(runtime_factory, endpoint):
    runtime, process, _ = runtime_factory(
        rpc_results={"account/rateLimits/read": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10}}},
                     "account/usage/read": {"summary": {"lifetimeTokens": 100}, "dailyUsageBuckets": []}},
        rpc_errors={endpoint: {"code": -32601, "message": "password=private-service-error"}},
    )
    result = runtime.usage()
    assert result["error_code"] == "unsupported"
    if endpoint == "account/usage/read":
        assert result["rate_limits"][0]["primary"]["used_percent"] == 10
        assert result["token_usage"] == {"lifetime_tokens": None, "today_tokens": None}
    else:
        assert result["rate_limits"] is None
        assert result["token_usage"]["lifetime_tokens"] == 100
    assert "private" not in json.dumps(result)
    assert not process.terminated


def test_usage_auth_change_cannot_reuse_previous_account_values(runtime_factory):
    runtime, process, _ = runtime_factory(rpc_results={
        "account/rateLimits/read": {"rateLimits": {"primary": {"usedPercent": 10}}},
        "account/usage/read": {"summary": {"lifetimeTokens": 100}},
    })
    assert runtime.usage()["token_usage"]["lifetime_tokens"] == 100
    process.account = "apiKey"
    before = len(process.calls)
    result = runtime.usage()
    assert result["authenticated"] is False
    assert result["error_code"] == "authentication"
    assert result["rate_limits"] is None
    assert result["token_usage"] == {"lifetime_tokens": None, "today_tokens": None}
    assert [call["method"] for call in process.calls[before:]] == ["account/read"]


def test_usage_busy_does_not_interrupt_or_consume_active_inference(runtime_factory):
    import threading
    import time

    runtime, process, _ = runtime_factory(stalled=True)
    answers = []
    errors = []

    def infer():
        try:
            answers.append(runtime.complete("system", [], timeout=3))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=infer)
    worker.start()
    deadline = time.monotonic() + 2
    while not any(call.get("method") == "turn/start" for call in process.calls) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert any(call.get("method") == "turn/start" for call in process.calls)
    before = len(process.calls)
    started = time.monotonic()
    result = runtime.usage(timeout=0.01)
    assert time.monotonic() - started < 0.1
    assert result["error_code"] == "busy"
    assert len(process.calls) == before and not process.terminated
    item = {"id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "Hello"}
    process.emit({"method": "turn/completed", "params": {"threadId": "thread", "turn": {
        "id": "turn", "items": [item], "status": "completed"}}})
    worker.join(timeout=2)
    assert not worker.is_alive() and not errors and answers == [("Hello", "gpt-b")]
    assert not process.terminated


def test_usage_reentrant_call_is_busy(runtime_factory):
    import time

    runtime, process, _ = runtime_factory()
    with runtime._lease(time.monotonic() + 1):
        assert runtime.usage()["error_code"] == "busy"
    assert not process.calls


def test_usage_timeout_keeps_idle_engine_available_for_next_inference(runtime_factory):
    runtime, process, _ = runtime_factory(stalled_methods={"account/rateLimits/read"})
    result = runtime.usage(timeout=0.08)
    assert result["error_code"] == "timeout"
    assert not process.terminated
    assert runtime.complete("system", [], timeout=2) == ("Hello", "gpt-b")


def test_usage_after_shutdown_does_not_start_engine(runtime_factory):
    runtime, process, _ = runtime_factory()
    runtime.shutdown()
    assert runtime.usage()["error_code"] == "cancelled"
    assert not process.calls


def test_api_credentials_removed_and_api_auth_cannot_infer(runtime_factory, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("CODEX_API_KEY", "test-secret")
    runtime, process, invocation = runtime_factory(account="apiKey")
    with pytest.raises(CodexRuntimeError, match="API 키 인증"):
        runtime.complete("system", [{"type": "text", "text": "question"}])
    assert not any(call.get("method") == "thread/start" for call in process.calls)
    assert "OPENAI_API_KEY" not in invocation["env"]
    assert "CODEX_API_KEY" not in invocation["env"]
    assert process.terminated


def test_complete_preserves_early_events_disables_tools_and_returns_model(runtime_factory):
    runtime, process, _ = runtime_factory()
    text, model = runtime.complete("system", [{"type": "text", "text": "question"}])
    assert (text, model) == ("Hello", "gpt-b")
    start = next(call["params"] for call in process.calls if call.get("method") == "thread/start")
    assert start["ephemeral"] is True
    assert start["sandbox"] == "read-only"
    assert start["allowProviderModelFallback"] is False
    assert start["dynamicTools"] == start["environments"] == []
    assert start["config"]['mcp_servers.personal.enabled'] is False
    assert all(start["config"][f"features.{name}"] is False for name in _DISABLED_FEATURES)
    assert all(value is False for value in start["approvalPolicy"]["granular"].values())
    assert process.calls[-1]["method"] == "thread/unsubscribe"


def test_native_tool_attempt_terminates_engine(runtime_factory):
    runtime, process, _ = runtime_factory(native=True)
    with pytest.raises(CodexRuntimeError) as error:
        runtime.complete("system", [{"type": "text", "text": "question"}])
    assert error.value.code == "native_tool"
    assert process.terminated


def test_selected_provider_cannot_be_replaced(runtime_factory):
    runtime, process, _ = runtime_factory(provider="custom-api")
    with pytest.raises(CodexRuntimeError) as error:
        runtime.complete("system", [])
    assert error.value.code == "model_unavailable"
    assert not any(call.get("method") == "turn/start" for call in process.calls)


def test_failure_never_leaks_provider_error(runtime_factory):
    runtime, process, _ = runtime_factory(failure={
        "message": "access_token=test-secret", "codexErrorInfo": "usageLimitExceeded"})
    with pytest.raises(CodexRuntimeError) as error:
        runtime.complete("system", [])
    assert "test-secret" not in str(error.value)
    assert "구독 사용 한도" in str(error.value)


def test_cancellation_during_active_turn_stops_process(runtime_factory):
    runtime, process, _ = runtime_factory(stalled=True)

    def cancel():
        if any(call.get("method") == "turn/start" for call in process.calls):
            raise ToolCancelledError("cancelled")

    with pytest.raises(ToolCancelledError):
        runtime.complete("system", [], cancellation_check=cancel)
    assert process.terminated


def test_outer_deadline_covers_protocol_wait(runtime_factory):
    runtime, process, _ = runtime_factory(stalled=True)
    with pytest.raises(InferenceDeadlineError), inference_deadline(0.08):
        runtime.complete("system", [], timeout=10)
    assert process.terminated


def test_client_own_timeout_is_a_typed_model_failure(runtime_factory):
    runtime, process, _ = runtime_factory(stalled=True)
    client = CodexClient(runtime=runtime)
    with pytest.raises(ModelCallError) as error:
        client.chat_structured([], request_timeout=0.08)
    assert error.value.code == "timeout"
    assert error.value.provider == "codex"
    assert process.terminated


def test_client_preserves_shorter_shared_deadline(runtime_factory):
    runtime, process, _ = runtime_factory(stalled=True)
    client = CodexClient(runtime=runtime)
    with pytest.raises(InferenceDeadlineError), inference_deadline(0.08):
        client.chat_structured([], request_timeout=10)
    assert process.terminated


def test_client_timeout_inside_longer_shared_deadline_is_typed(runtime_factory):
    runtime, process, _ = runtime_factory(stalled=True)
    client = CodexClient(runtime=runtime)
    with pytest.raises(ModelCallError) as error, inference_deadline(10):
        client.chat_structured([], request_timeout=0.08)
    assert error.value.code == "timeout"
    assert process.terminated


def test_cancellation_keeps_priority_over_client_timeout(runtime_factory):
    runtime, process, _ = runtime_factory(stalled=True)

    def cancel():
        if any(call.get("method") == "turn/start" for call in process.calls):
            raise ToolCancelledError("cancelled")

    client = CodexClient(runtime=runtime, cancellation_check=cancel)
    with pytest.raises(ToolCancelledError):
        client.chat_structured([], request_timeout=0.08)
    assert process.terminated


@pytest.mark.parametrize("source", ["callback", "generation"])
def test_simultaneous_cancellation_beats_expired_runtime_deadline(source):
    from core import auxiliary_models
    import time

    class DelayedRuntime(CodexRuntime):
        cancel_ready = False

        def _start(self, deadline, cancellation_check=None):
            time.sleep(0.02)
            self.cancel_ready = True
            if source == "generation":
                auxiliary_models.invalidate()
            self._checkpoint(deadline, cancellation_check)

    runtime = DelayedRuntime(settings=Settings())

    def cancel():
        if runtime.cancel_ready:
            raise ToolCancelledError("cancelled")

    client = CodexClient(runtime=runtime, cancellation_check=cancel if source == "callback" else None)
    with pytest.raises(ToolCancelledError):
        client.chat_structured([], request_timeout=0.01)


@pytest.mark.parametrize("outcome, expected_status, expected_code", [
    ("success", "success", ""), ("provider", "failed", "protocol"),
    ("timeout", "failed", "timeout"), ("shared", "failed", "inference_deadline_exceeded"),
    ("cancelled", "cancelled", "cancelled"),
])
def test_trace_records_only_call_metadata(monkeypatch, outcome, expected_status, expected_code):
    from core.local_inference import check_inference_deadline
    from core.productization import TRACE
    from contextlib import nullcontext
    import time

    records = []
    monkeypatch.setattr(TRACE, "emit", lambda event, **fields: records.append({"event": event, **fields}))

    class Runtime:
        selected_model = "gpt-test"

        def complete(self, *args, **kwargs):
            if outcome == "provider":
                raise CodexRuntimeError("protocol", "account=password=private-provider")
            if outcome == "cancelled":
                raise ToolCancelledError("token=private-cancellation")
            if outcome in {"timeout", "shared"}:
                while True:
                    check_inference_deadline()
                    time.sleep(0.002)
            return "private-output", self.selected_model

    client = CodexClient(role="reasoning", runtime=Runtime())
    context = inference_deadline(0.02) if outcome == "shared" else nullcontext()
    try:
        with context:
            client.chat_structured([{"role": "user", "content": "password=private-prompt"}],
                                   request_timeout=0.02 if outcome == "timeout" else 10)
    except (ModelCallError, InferenceDeadlineError, ToolCancelledError):
        assert outcome != "success"
    assert len(records) == 1
    assert set(records[0]) == {"event", "role", "model", "status", "error_code", "duration_ms"}
    assert records[0]["event"] == "model.codex.call"
    assert records[0]["role"] == "reasoning"
    assert records[0]["model"] == "gpt-test"
    assert records[0]["status"] == expected_status
    assert records[0]["error_code"] == expected_code
    assert isinstance(records[0]["duration_ms"], float) and records[0]["duration_ms"] >= 0
    assert "private" not in json.dumps(records)


@pytest.mark.parametrize("outcome", ["success", "timeout", "cancelled"])
def test_trace_write_error_does_not_replace_model_result(monkeypatch, outcome):
    from core.productization import TRACE
    from core.local_inference import check_inference_deadline
    import time

    def fail_trace(*args, **kwargs):
        raise OSError("trace destination unavailable")

    monkeypatch.setattr(TRACE, "emit", fail_trace)

    class Runtime:
        selected_model = "gpt-test"

        def complete(self, *args, **kwargs):
            if outcome == "timeout":
                while True:
                    check_inference_deadline()
                    time.sleep(0.002)
            if outcome == "cancelled":
                raise ToolCancelledError("cancelled")
            return "answer", self.selected_model

    client = CodexClient(runtime=Runtime())
    if outcome == "success":
        assert client.chat([]) == "answer"
    elif outcome == "timeout":
        with pytest.raises(ModelCallError) as error:
            client.chat_structured([], request_timeout=0.01)
        assert error.value.code == "timeout"
    else:
        with pytest.raises(ToolCancelledError):
            client.chat([])


def test_prose_records_actual_selected_model(runtime_factory):
    runtime, process, _ = runtime_factory()
    client = CodexClient(runtime=runtime)
    response = client.chat_prose([{"role": "user", "content": "hello"}])
    assert response.provider == "codex"
    assert response.model == "gpt-b"
    assert response.truncated is False
    assert response.finish_reason == "completed"


@pytest.mark.parametrize("arguments", ['{"path":"test.txt"}', '{"path":4}', '[]'])
def test_tool_plan_validates_anis_input_without_running_tools(runtime_factory, arguments):
    answer = json.dumps({"text": "", "tool_calls": [{"name": "read_file", "arguments_json": arguments}]})
    runtime, process, _ = runtime_factory(answer=answer)
    client = CodexClient(runtime=runtime)
    client.tools = [{"name": "read_file", "description": "Read a file", "input_schema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
    }}]
    if arguments == '{"path":"test.txt"}':
        text, tools = client.chat_with_tools([{"role": "user", "content": "read"}], ["read_file"])
        assert text == ""
        assert tools[0]["name"] == "read_file"
        assert tools[0]["input"] == {"path": "test.txt"}
    else:
        with pytest.raises(ModelCallError) as error:
            client.chat_with_tools([{"role": "user", "content": "read"}])
        assert error.value.code == "invalid_tool"
    turn = next(call["params"] for call in process.calls if call.get("method") == "turn/start")
    assert turn["outputSchema"]["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["read_file"]


def test_output_schema_rejects_incomplete_json(runtime_factory):
    runtime, process, _ = runtime_factory(answer='{"broken":')
    client = CodexClient(runtime=runtime)
    with pytest.raises(ModelCallError) as error:
        client.chat_structured([], {"type": "object"})
    assert error.value.code == "invalid_output"


def test_vision_input_and_tool_history_are_preserved():
    instructions, inputs = _prepare_messages([
        {"role": "system", "content": "Anis system"},
        {"role": "assistant", "content": "", "tool_calls": [{"name": "read"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "done"},
         {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "test"}}]},
        {"role": "user", "content": "question", "images": ["image-data"]},
    ], "fallback")
    assert instructions == "Anis system"
    assert "tool_result" in inputs[0]["text"] and "tool_calls" in inputs[0]["text"]
    assert inputs[1] == {"type": "image", "url": "data:image/jpeg;base64,test"}
    assert inputs[2] == {"type": "image", "url": "data:image/png;base64,image-data"}


def test_shutdown_without_discovery_never_starts_process():
    def fail(*args, **kwargs):
        raise AssertionError("must not start")
    runtime = CodexRuntime(settings=Settings(), process_factory=fail)
    runtime.shutdown()


def test_gpt_toggle_during_turn_rejects_stale_output(runtime_factory):
    from core import auxiliary_models
    runtime, process, _ = runtime_factory(stalled=True)
    client = CodexClient(runtime=runtime)
    invalidated = False

    def toggle():
        nonlocal invalidated
        if not invalidated and any(call.get("method") == "turn/start" for call in process.calls):
            invalidated = True
            auxiliary_models.invalidate()

    client.cancellation_check = toggle
    with pytest.raises(ToolCancelledError):
        client.chat([{"role": "user", "content": "hello"}])
    assert process.terminated


def test_model_switch_invalidates_before_waiting_for_active_turn(runtime_factory, monkeypatch):
    from core import auxiliary_models
    runtime, process, _ = runtime_factory()
    runtime.discover()
    generation = auxiliary_models.ticket()
    runtime.set_model("gpt-a")
    assert not auxiliary_models.is_current(generation)


@pytest.mark.parametrize("overrides", [
    {"openai_base_url": "https://proxy.example/v1"},
    {"model_providers": {"openai": {"base_url": "https://proxy.example/v1"}}},
    {"model_providers": {"openai": {"env_key": "CUSTOM_API_KEY"}}},
    {"model_providers": {"openai": {"requires_openai_auth": False}}},
])
def test_custom_provider_cannot_receive_chatgpt_credentials(runtime_factory, overrides):
    runtime, process, _ = runtime_factory(config_overrides=overrides)
    with pytest.raises(CodexRuntimeError):
        runtime.discover()
    assert process.terminated
    assert not any(call.get("method") in {"account/read", "model/list", "turn/start"}
                   for call in process.calls)


def test_shutdown_interrupts_waiting_inference(runtime_factory):
    import threading
    import time
    runtime, process, _ = runtime_factory(stalled=True)
    stopped = threading.Event()

    def run():
        try:
            runtime.complete("test", [{"type": "text", "text": "hello"}], timeout=30)
        except ToolCancelledError:
            stopped.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 2
    while not any(call.get("method") == "turn/start" for call in process.calls):
        assert time.monotonic() < deadline
        time.sleep(.01)
    runtime.shutdown()
    worker.join(timeout=2)
    assert stopped.is_set() and not worker.is_alive() and process.terminated
