import asyncio
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from core import mcp_bridge as bridge
from core.plugin import CancellationToken, PluginRegistry, ToolCancelledError
from core.tool_result import ToolRunStatus


def remote_tool(name="echo", schema=None):
    return SimpleNamespace(name=name, description="echo", input_schema=schema or {
        "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
    })


def test_sdk_schema_and_remote_error_are_not_success(monkeypatch):
    plugin = bridge.MCPPlugin({"name": "test", "url": "http://127.0.0.1/mcp"}, [remote_tool()])
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    assert registry.validate_tool_call("mcp_test_echo", {})
    assert registry.validate_tool_call("mcp_test_echo", {"value": 1})
    async def error(*_):
        return {"is_error": True, "content": [{"type": "text", "text": "거부됨"}]}
    monkeypatch.setattr(bridge, "_call", error)
    result = registry.execute_tool("mcp_test_echo", {"value": "합성 시험"})
    assert result.status == ToolRunStatus.FAILED and result.raw_output == "거부됨"
    assert plugin.probe_connection().state == "confirmed"
    registry.shutdown()


@pytest.mark.parametrize("value", [None, [], {"is_error": "false"}, {"content": ["invalid"]}])
def test_invalid_response_is_rejected(value):
    with pytest.raises(ValueError):
        bridge._response_result("local", "remote", value)


def test_success_response_is_not_independent_workflow_verification():
    result = bridge._response_result("local", "remote", {
        "content": [{"type": "text", "text": "완료했다고 서버가 주장함"}],
        "structured_content": {"id": "synthetic"},
    })
    assert result.status == ToolRunStatus.UNVERIFIED
    assert "synthetic" in result.raw_output
    assert result.evidence[0].data["remote_tool"] == "remote"


def test_long_text_is_bounded_and_binary_is_not_forwarded():
    result = bridge._response_result("local", "remote", {"content": [
        {"type": "text", "text": "x" * 70000}, {"type": "image", "data": "sensitive-binary"},
    ]})
    assert len(result.raw_output) == 64000
    assert result.evidence[0].data["truncated"] is True
    assert "sensitive-binary" not in str(result.to_dict())


def test_colliding_remote_names_fail_before_registration():
    with pytest.raises(ValueError, match="구분"):
        bridge.MCPPlugin({"name": "test", "url": "http://127.0.0.1/mcp"}, [
            remote_tool("same-name"), remote_tool("same_name"),
        ])


@pytest.mark.parametrize("url", [
    "https://:secret@example.com/mcp", "https://example.com/mcp#secret",
    "https://example.com/mcp?mcp_token=secret", "https://example.com/mcp?api_key=secret",
])
def test_credentials_cannot_be_persisted_in_plaintext_url(url):
    with pytest.raises(ValueError):
        bridge.validate_manifest({"name": "test", "url": url})


def test_remote_tools_require_explicit_external_action_approval():
    tool = bridge.MCPPlugin({"name": "test", "url": "https://example.com/mcp"}, [remote_tool()]).get_tools()[0]
    assert tool.side_effect == "external_send"
    assert tool.required_permissions == ["network_access", "external_send"]
    assert tool.max_retries == 0


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_bounded_call_closes_owned_async_operation(mode):
    closed = []
    async def waiting():
        try:
            await asyncio.sleep(30)
        finally:
            closed.append(True)
    ticks = []
    def checkpoint():
        ticks.append(1)
        if mode == "cancel" and len(ticks) >= 2:
            raise ToolCancelledError("cancelled")
    before = time.monotonic()
    with pytest.raises(TimeoutError if mode == "timeout" else ToolCancelledError):
        asyncio.run(bridge._bounded(waiting(), 0.03 if mode == "timeout" else 5, checkpoint))
    assert closed == [True] and time.monotonic() - before < 2


def test_tool_discovery_follows_pages_and_rejects_repeated_cursor(monkeypatch):
    import mcp
    pages = [[remote_tool("one")], [remote_tool("two")]]
    cursors = []
    class Client:
        def __init__(self, *_args, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def list_tools(self, *, cursor, cache_mode):
            cursors.append(cursor)
            assert cache_mode == "reload"
            return SimpleNamespace(tools=pages.pop(0), next_cursor="next" if len(pages) else None)
    monkeypatch.setattr(mcp, "Client", Client)
    tools = asyncio.run(bridge._discover({"url": "http://localhost/mcp"}))
    assert [x.name for x in tools] == ["one", "two"] and cursors == [None, "next"]
    pages[:] = [[remote_tool()], [remote_tool()], [remote_tool()]]
    with pytest.raises(ValueError, match="반복"):
        asyncio.run(bridge._discover({"url": "http://localhost/mcp"}))


def test_invalid_replacement_keeps_existing_plugin(tmp_path, monkeypatch):
    registry = PluginRegistry()
    config = {"name": "test", "url": "http://localhost/mcp"}
    old = bridge.MCPPlugin(config, [remote_tool()])
    registry.register_plugin(old)
    async def bad(_config):
        return [remote_tool("a-b"), remote_tool("a_b")]
    monkeypatch.setattr(bridge, "_discover", bad)
    with pytest.raises(ValueError):
        bridge.connect_mcp(registry, config, path=tmp_path / "servers.json")
    assert registry.get_plugin(old.name) is old
    assert not (tmp_path / "servers.json").exists()
    registry.shutdown()


def test_store_failure_restores_existing_plugin(tmp_path, monkeypatch):
    registry = PluginRegistry()
    config = {"name": "test", "url": "http://localhost/mcp"}
    old = bridge.MCPPlugin(config, [remote_tool()])
    registry.register_plugin(old)
    async def discover(_config):
        return [remote_tool("new_tool")]
    monkeypatch.setattr(bridge, "_discover", discover)
    def refuse(*_args):
        raise OSError("synthetic disk error")
    monkeypatch.setattr(bridge, "_write_store", refuse)
    with pytest.raises(OSError):
        bridge.connect_mcp(registry, config, path=tmp_path / "servers.json")
    assert registry.get_plugin(old.name) is old
    assert "mcp_test_new_tool" not in registry._tools
    registry.shutdown()


@pytest.mark.parametrize("failures", [set(), {"two"}, {"one", "two"}])
def test_saved_reconnect_reports_actual_success_and_safe_failure_counts(monkeypatch, failures):
    registry = PluginRegistry()
    monkeypatch.setattr(bridge, "_read_store", lambda _path: [
        {"name": "one", "url": "https://one.example/mcp"},
        {"name": "two", "url": "https://two.example/mcp"},
    ])
    def connect(_registry, config, **kwargs):
        assert kwargs["persist"] is False
        if config["name"] in failures:
            raise ValueError("remote response included synthetic-secret")
    monkeypatch.setattr(bridge, "connect_mcp", connect)
    result = bridge.load_saved_mcp_plugins(registry)
    assert result == {"connected": 2 - len(failures), "failed": len(failures)}
    assert len(registry._load_failures) == len(failures)
    assert all(row["error"] == "mcp_reconnect_failed" for row in registry._load_failures.values())


def test_saved_reconnect_does_not_swallow_cancellation(monkeypatch):
    registry = PluginRegistry()
    monkeypatch.setattr(bridge, "_read_store", lambda _path: [{"name": "one"}])
    def cancel(*_args, **_kwargs):
        raise ToolCancelledError("cancelled")
    monkeypatch.setattr(bridge, "connect_mcp", cancel)
    with pytest.raises(ToolCancelledError):
        bridge.load_saved_mcp_plugins(registry)
    assert registry._load_failures == {}


def test_real_sdk_http_discovery_call_and_schema(tmp_path):
    """Real SDK + loopback HTTP server; no account or external service."""
    import uvicorn
    from mcp.server import MCPServer
    server = MCPServer("ANIS synthetic QA")
    dispatched = threading.Event()
    @server.tool()
    async def echo(value: str) -> dict:
        if value == "cancel-synthetic":
            dispatched.set()
            await asyncio.sleep(30)
        return {"echo": value}
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="error", lifespan="on"))
    worker = threading.Thread(target=lambda: service.run(sockets=[sock]), daemon=True)
    worker.start()
    registry = PluginRegistry()
    try:
        deadline = time.monotonic() + 8
        while not service.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert service.started, "loopback MCP server did not start"
        plugin = bridge.connect_mcp(registry, {"name": "qa", "url": f"http://127.0.0.1:{port}/mcp"},
                                    path=tmp_path / "servers.json")
        assert plugin.get_tools()[0].input_schema["required"] == ["value"]
        assert registry.validate_tool_call("mcp_qa_echo", {"value": 3})
        result = registry.execute_tool("mcp_qa_echo", {"value": "로컬 MCP 왕복"})
        assert result.status == ToolRunStatus.UNVERIFIED
        assert "로컬 MCP 왕복" in result.raw_output
        assert plugin.probe_connection().state == "confirmed"
        assert bridge.saved_mcp_count(path=tmp_path / "servers.json") == 1
        token, received = CancellationToken(), []
        caller = threading.Thread(target=lambda: received.append(registry.execute_tool(
            "mcp_qa_echo", {"value": "cancel-synthetic"}, cancellation_token=token)), daemon=True)
        caller.start()
        assert dispatched.wait(4), "MCP request did not reach the real HTTP server"
        before_cancel = time.monotonic()
        token.cancel()
        caller.join(4)
        assert not caller.is_alive() and time.monotonic() - before_cancel < 4
        assert received[0].status == ToolRunStatus.CANCELLED
    finally:
        registry.shutdown()
        service.should_exit = True
        worker.join(8)
        sock.close()
    assert not worker.is_alive()
