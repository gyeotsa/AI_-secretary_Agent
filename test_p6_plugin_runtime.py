import asyncio
import threading
import time

import pytest

from core.plugin import BasePlugin, PluginContractError, PluginRegistry, ToolSchema
from core.tool_result import ToolRunResult


class RuntimePlugin(BasePlugin):
    def __init__(self, name="runtime", tools=None, handler=None):
        super().__init__()
        self.name = name
        self.version = "2.1.0"
        self.supported_os = ["Windows"]
        self._tools = tools or []
        self._handler = handler or (lambda _name, payload: payload)

    def get_tools(self):
        return self._tools

    def execute_tool(self, tool_name, tool_input):
        return self._handler(tool_name, tool_input)


def schema(name="sample", **kwargs):
    return ToolSchema(
        name=name,
        description="test",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "required": ["value"]},
        **kwargs,
    )


def test_duplicate_plugin_and_tool_names_fail_fast():
    registry = PluginRegistry()
    registry.register_plugin(RuntimePlugin("one", [schema()]))
    with pytest.raises(PluginContractError, match="중복 Plugin"):
        registry.register_plugin(RuntimePlugin("one", [schema("other")]))
    with pytest.raises(PluginContractError, match="중복 Tool"):
        registry.register_plugin(RuntimePlugin("two", [schema()]))


def test_input_and_output_json_schema_are_both_enforced():
    registry = PluginRegistry()
    registry.register_plugin(RuntimePlugin(tools=[schema()], handler=lambda *_: {"wrong": True}))
    invalid_input = registry.execute_tool("sample", {"value": "not-int"})
    assert isinstance(invalid_input, ToolRunResult) and not invalid_input.succeeded
    assert "입력 JSON Schema 오류" in invalid_input.error
    invalid_output = registry.execute_tool("sample", {"value": 1})
    assert isinstance(invalid_output, ToolRunResult) and not invalid_output.succeeded
    assert "출력 JSON Schema 오류" in invalid_output.error


def test_async_tool_and_retry_policy():
    attempts = {"count": 0}
    async def handler(_name, payload):
        attempts["count"] += 1
        await asyncio.sleep(0)
        if attempts["count"] == 1:
            raise RuntimeError("retry")
        return payload
    registry = PluginRegistry()
    registry.register_plugin(RuntimePlugin(tools=[schema(execution_mode="async", max_retries=1)], handler=handler))
    assert registry.execute_tool("sample", {"value": 7}) == {"value": 7}
    assert attempts["count"] == 2


def test_timeout_and_cancellation_policy():
    registry = PluginRegistry()
    registry.register_plugin(RuntimePlugin(tools=[schema(timeout_seconds=0.03)], handler=lambda *_: time.sleep(0.2) or {"value": 1}))
    timed_out = registry.execute_tool("sample", {"value": 1})
    assert "시간 초과" in timed_out.error

    registry = PluginRegistry()
    registry.register_plugin(RuntimePlugin(tools=[schema(cancellable=True)], handler=lambda *_: time.sleep(0.3) or {"value": 1}))
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("value", registry.execute_tool("sample", {"value": 1})))
    worker.start()
    time.sleep(0.03)
    assert registry.cancel_tool("sample") is True
    worker.join(timeout=1)
    assert "취소" in result["value"].error


def test_plugin_status_axes_are_independent():
    plugin = RuntimePlugin(tools=[schema()])
    plugin.auth_required = True
    plugin.auth_type = "OAuth"
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    status = registry.get_plugin_statuses()[0]
    assert status.installed is True and status.enabled
    assert status.connected is None
    assert status.connection_state == "not_applicable"
    assert status.authenticated is None
    assert status.authentication_state == "unchecked"
    assert status.verified is None
    assert status.verification_state == "unchecked"
    assert any("OAuth" in item for item in status.diagnostics)


def test_tool_executor_exposes_legacy_tools_only_through_registry():
    from core.tools import get_tool_executor, get_tools_schema
    executor = get_tool_executor()
    assert executor.plugin_registry.get_plugin("legacy_runtime") is not None
    names = [item["name"] for item in get_tools_schema()]
    assert len(names) == len(set(names))
    assert "read_file" in names
