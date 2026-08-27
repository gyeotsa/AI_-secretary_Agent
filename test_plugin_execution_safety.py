import threading
import time

import pytest

from core.plugin import BasePlugin, PluginRegistry, ToolSchema
from core.executor import ExecutionOutcome, Executor
from core.tool_result import Evidence, ToolRunResult, ToolRunStatus


class SafetyPlugin(BasePlugin):
    def __init__(self, tool, handler):
        super().__init__()
        self.name = f"safety_{tool.name}"
        self._tool = tool
        self._handler = handler

    def get_tools(self):
        return [self._tool]

    def execute_tool(self, tool_name, tool_input):
        return self._handler(self, tool_name, tool_input)


def make_schema(name="safe_read", **kwargs):
    return ToolSchema(
        name=name,
        description="execution safety test",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "required": ["value"]},
        **kwargs,
    )


def test_execution_id_isolates_cancellation_for_concurrent_same_tool():
    started = {1: threading.Event(), 2: threading.Event()}
    release = {1: threading.Event(), 2: threading.Event()}
    observed_ids = {}

    def handler(plugin, _name, payload):
        context = plugin.get_execution_context()
        assert context is not None
        observed_ids[payload["value"]] = context.execution_id
        started[payload["value"]].set()
        while not release[payload["value"]].wait(0.005):
            context.raise_if_cancelled()
        return payload

    registry = PluginRegistry()
    registry.register_plugin(SafetyPlugin(make_schema(cancellable=True), handler))
    results = {}
    first = threading.Thread(
        target=lambda: results.setdefault(
            "first",
            registry.execute_tool("safe_read", {"value": 1}, execution_id="run-first"),
        )
    )
    second = threading.Thread(
        target=lambda: results.setdefault(
            "second",
            registry.execute_tool("safe_read", {"value": 2}, execution_id="run-second"),
        )
    )
    first.start()
    second.start()
    assert started[1].wait(1) and started[2].wait(1)
    assert set(registry.get_active_execution_ids("safe_read")) == {"run-first", "run-second"}

    assert registry.cancel_execution("run-first") is True
    release[2].set()
    first.join(1)
    second.join(1)

    assert observed_ids == {1: "run-first", 2: "run-second"}
    assert results["first"].status == ToolRunStatus.CANCELLED
    assert results["second"] == {"value": 2}
    assert registry.get_active_execution_ids() == []


@pytest.mark.parametrize(
    ("tool_name", "explicit_effect", "expected_effect"),
    [
        ("mail_send", "auto", "external_send"),
        ("filesystem_delete_file", "auto", "change"),
        ("payment_charge", "auto", "external_send"),
        ("remote_mutation", "change", "change"),
    ],
)
def test_side_effect_timeout_never_retries(tool_name, explicit_effect, expected_effect):
    calls = {"count": 0}

    def handler(_plugin, _name, payload):
        calls["count"] += 1
        time.sleep(0.08)
        return payload

    registry = PluginRegistry()
    registry.register_plugin(SafetyPlugin(make_schema(
        name=tool_name,
        side_effect=explicit_effect,
        timeout_seconds=0.015,
        max_retries=3,
    ), handler))

    result = registry.execute_tool(tool_name, {"value": 1})
    time.sleep(0.1)
    capability = registry.get_capability(tool_name)

    assert isinstance(result, ToolRunResult)
    assert result.status == ToolRunStatus.FAILED
    assert "자동 재시도하지 않았습니다" in result.error
    assert calls["count"] == 1
    assert capability.side_effect == expected_effect
    assert capability.max_retries == 0
    assert capability.automatic_retry_allowed is False


def test_registry_idempotency_key_collapses_concurrent_side_effects():
    calls = {"count": 0}
    started = threading.Event()
    release = threading.Event()

    def handler(_plugin, _name, payload):
        calls["count"] += 1
        started.set()
        assert release.wait(1)
        return payload

    registry = PluginRegistry()
    registry.register_plugin(SafetyPlugin(make_schema(
        name="external_send_message",
        side_effect="external_send",
        timeout_seconds=1,
    ), handler))
    results = {}
    first = threading.Thread(target=lambda: results.setdefault(
        "first",
        registry.execute_tool(
            "external_send_message", {"value": 7},
            execution_id="send-1", idempotency_key="message-42",
        ),
    ))
    second = threading.Thread(target=lambda: results.setdefault(
        "second",
        registry.execute_tool(
            "external_send_message", {"value": 7},
            execution_id="send-2", idempotency_key="message-42",
        ),
    ))

    first.start()
    assert started.wait(1)
    second.start()
    time.sleep(0.03)
    assert calls["count"] == 1
    release.set()
    first.join(1)
    second.join(1)

    assert calls["count"] == 1
    assert results == {"first": {"value": 7}, "second": {"value": 7}}
    assert registry.get_execution_result("send-1") == {"value": 7}
    assert registry.get_execution_result("send-2") == {"value": 7}


def test_same_idempotency_key_after_uncertain_timeout_does_not_execute_again():
    calls = {"count": 0}

    def handler(_plugin, _name, payload):
        calls["count"] += 1
        time.sleep(0.08)
        return payload

    registry = PluginRegistry()
    registry.register_plugin(SafetyPlugin(make_schema(
        name="mail_send",
        timeout_seconds=0.015,
        max_retries=5,
    ), handler))

    first = registry.execute_tool(
        "mail_send", {"value": 1},
        execution_id="mail-1", idempotency_key="outbox-1",
    )
    second = registry.execute_tool(
        "mail_send", {"value": 1},
        execution_id="mail-2", idempotency_key="outbox-1",
    )
    time.sleep(0.1)

    assert first.status == ToolRunStatus.FAILED
    assert second.status == ToolRunStatus.FAILED
    assert first.error == second.error
    assert calls["count"] == 1


def test_idempotency_key_cannot_be_reused_for_different_input():
    calls = {"count": 0}

    def handler(_plugin, _name, payload):
        calls["count"] += 1
        return payload

    registry = PluginRegistry()
    registry.register_plugin(SafetyPlugin(make_schema(
        name="external_send_message",
        side_effect="external_send",
    ), handler))

    assert registry.execute_tool(
        "external_send_message", {"value": 1}, idempotency_key="same-key",
    ) == {"value": 1}
    rejected = registry.execute_tool(
        "external_send_message", {"value": 2}, idempotency_key="same-key",
    )

    assert isinstance(rejected, ToolRunResult)
    assert rejected.status == ToolRunStatus.FAILED
    assert "서로 다른 입력" in rejected.error
    assert calls["count"] == 1


def test_read_only_exception_retry_remains_backward_compatible():
    calls = {"count": 0}

    def handler(_plugin, _name, payload):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary read failure")
        return payload

    registry = PluginRegistry()
    registry.register_plugin(SafetyPlugin(make_schema(max_retries=1), handler))

    assert registry.execute_tool("safe_read", {"value": 9}) == {"value": 9}
    assert calls["count"] == 2
    assert registry.get_capability("safe_read").automatic_retry_allowed is True


def test_reusing_execution_id_for_different_tool_is_rejected():
    registry = PluginRegistry()
    registry.register_plugin(SafetyPlugin(
        make_schema(name="first_read"),
        lambda _plugin, _name, payload: payload,
    ))
    registry.register_plugin(SafetyPlugin(
        make_schema(name="second_read"),
        lambda _plugin, _name, payload: payload,
    ))

    assert registry.execute_tool(
        "first_read", {"value": 1}, execution_id="shared-run",
    ) == {"value": 1}
    rejected = registry.execute_tool(
        "second_read", {"value": 2}, execution_id="shared-run",
    )

    assert isinstance(rejected, ToolRunResult)
    assert rejected.status == ToolRunStatus.FAILED
    assert "이미 first_read 도구에 사용" in rejected.error


class _MetricRecorder:
    def __init__(self):
        self.rows = []

    def record(self, key, value=1.0, *, success=True, context=None):
        self.rows.append((key, value, success, context or {}))


def test_completed_step_counter_is_not_treated_as_verified_execution():
    executor = Executor.__new__(Executor)
    executor.quality_metrics = _MetricRecorder()
    outcome = ExecutionOutcome(
        "완료했습니다.", status="completed", completed_steps=2,
    )

    executor._record_turn_quality("파일을 생성해줘", outcome, time.perf_counter())

    false_completion = next(
        row for row in executor.quality_metrics.rows if row[0] == "false_completion"
    )
    assert false_completion[1:3] == (1.0, False)
    assert false_completion[3]["tool_count"] == 0


def test_multi_step_execution_is_verified_only_with_evidence_for_every_result():
    executor = Executor.__new__(Executor)
    executor.quality_metrics = _MetricRecorder()
    first = ToolRunResult.successful(
        tool_name="filesystem_create_file", raw_output="a",
        evidence=[Evidence("file_hash", "첫 파일 해시 확인")],
    )
    second = ToolRunResult.successful(
        tool_name="filesystem_create_file", raw_output="b",
        evidence=[Evidence("file_hash", "둘째 파일 해시 확인")],
    )
    outcome = ExecutionOutcome(
        "두 파일을 생성했습니다.", status="completed", completed_steps=2,
        tool_results=(first, second),
    )

    executor._record_turn_quality("파일 두 개를 생성해줘", outcome, time.perf_counter())

    false_completion = next(
        row for row in executor.quality_metrics.rows if row[0] == "false_completion"
    )
    assert false_completion[1:3] == (0.0, True)
    assert false_completion[3]["tool_count"] == 2
