"""Small MCP-to-PluginRegistry adapter."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from jsonschema import Draft202012Validator

from core.plugin import BasePlugin, PluginStateProbe, ToolCancelledError, ToolSchema
from core.tool_result import Evidence, ToolRunResult
from core.turn_context import check_turn_cancelled


STORE_PATH = Path("data/mcp_servers.json")
REQUEST_TIMEOUT = 30.0
CALL_TIMEOUT = 110.0


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.casefold()).strip("_") or "server"


def validate_manifest(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("MCP 매니페스트는 JSON 객체여야 합니다.")
    data = dict(value.get("mcp") or value)
    name = str(data.get("name") or "").strip()
    transport = str(data.get("transport") or ("http" if data.get("url") else "stdio")).casefold()
    if not name:
        raise ValueError("MCP 이름이 필요합니다.")
    if transport != "http":
        raise ValueError("현재 앱에서는 HTTPS MCP 연결만 지원합니다.")
    clean: dict[str, Any] = {"name": name, "transport": transport}
    url = str(data.get("url") or "").strip()
    parsed = urlparse(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError("사용자 정보가 포함되지 않은 http/https MCP URL이 필요합니다.")
    if any(re.search(r"(?:token|secret|password|api[_-]?key|authorization)", key, re.I)
           for key, _ in parse_qsl(parsed.query)):
        raise ValueError("인증정보가 포함된 MCP URL은 평문으로 저장할 수 없습니다. 공개 엔드포인트만 연결하세요.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("원격 MCP는 HTTPS를 사용해야 합니다.")
    clean["url"] = url
    return clean


def manifest_for_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url.strip())
    name = (parsed.hostname or "MCP").split(".")[0]
    return validate_manifest({"name": name, "transport": "http", "url": url.strip()})


def _target(config: dict[str, Any]):
    try:
        import mcp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("MCP SDK가 설치되지 않았습니다. requirements.txt를 설치하세요.") from exc
    return config["url"]


async def _discover(config: dict[str, Any]):
    from mcp import Client
    async with Client(_target(config), read_timeout_seconds=REQUEST_TIMEOUT) as client:
        found, cursors, cursor = [], set(), None
        for _ in range(20):
            result = await client.list_tools(cursor=cursor, cache_mode="reload")
            found.extend(result.tools)
            if len(found) > 500:
                raise ValueError("MCP 도구 목록이 500개 한도를 초과합니다.")
            cursor = result.next_cursor
            if not cursor:
                return found
            if cursor in cursors:
                raise ValueError("MCP 도구 목록의 페이지가 반복됩니다.")
            cursors.add(cursor)
        raise ValueError("MCP 도구 목록의 페이지 한도를 초과합니다.")


async def _call(config: dict[str, Any], name: str, arguments: dict[str, Any]):
    from mcp import Client
    async with Client(_target(config), read_timeout_seconds=CALL_TIMEOUT) as client:
        result = await client.call_tool(name, arguments, read_timeout_seconds=CALL_TIMEOUT)
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json", exclude_none=True)
        raise ValueError("MCP 서버가 구조화된 실행 결과를 반환하지 않았습니다.")


async def _bounded(operation, timeout: float, checkpoint=check_turn_cancelled):
    """Keep SDK I/O cancellable at the existing turn/tool boundary."""
    task = asyncio.create_task(operation)
    deadline = time.monotonic() + timeout
    try:
        while not task.done():
            checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError("MCP 통신 제한시간을 초과했습니다. 실행 여부가 불확실하면 재시도하지 마세요.")
            await asyncio.wait({task}, timeout=min(0.1, max(0, deadline - time.monotonic())))
        checkpoint()
        return await task
    finally:
        if not task.done():
            task.cancel()
        # SDK context managers must close their owned HTTP streams before return.
        await asyncio.gather(task, return_exceptions=True)


def _response_result(tool_name: str, remote_name: str, value: dict[str, Any]):
    if not isinstance(value, dict) or not isinstance(value.get("is_error", False), bool):
        raise ValueError("MCP 실행 결과의 오류 상태가 올바르지 않습니다.")
    content = value.get("content", [])
    if not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
        raise ValueError("MCP 실행 결과의 본문이 올바르지 않습니다.")
    texts = [item["text"] for item in content if item.get("type") == "text" and isinstance(item.get("text"), str)]
    structured = value.get("structured_content")
    if structured is not None:
        texts.append(json.dumps(structured, ensure_ascii=False))
    output = "\n".join(texts)
    truncated = len(output) > 64000
    output = output[:64000]
    evidence = [Evidence("mcp_response", "실제 MCP 서버 응답을 수신했습니다. 업무 완료의 독립 검증은 아닙니다.", {
        "remote_tool": remote_name, "is_error": value.get("is_error", False),
        "truncated": truncated, "content_types": [str(item.get("type", "")) for item in content],
    })]
    if value.get("is_error"):
        return ToolRunResult.failed(tool_name=tool_name, error="MCP 도구가 오류를 반환했습니다.",
                                    raw_output=output or "MCP 도구 오류", evidence=evidence)
    return ToolRunResult.unverified(tool_name=tool_name,
        raw_output=output or "MCP 응답에 표시 가능한 텍스트가 없습니다.", evidence=evidence)


class MCPPlugin(BasePlugin):
    def __init__(self, config: dict[str, Any], tools: list[Any]):
        super().__init__()
        self.config = validate_manifest(config)
        self.name = f"mcp:{_slug(self.config['name'])}"
        self.description = f"MCP 서버 · {self.config['name']}"
        self.dependencies = ["mcp"]
        self._connected = True
        self._remote_names: dict[str, str] = {}
        self._tools = []
        prefix = f"mcp_{_slug(self.config['name'])}_"
        for tool in tools:
            remote_name = str(tool.name)
            local_name = prefix + _slug(remote_name)
            if local_name in self._remote_names:
                raise ValueError("MCP 도구 이름을 안전한 로컬 이름으로 구분할 수 없습니다.")
            schema = dict(getattr(tool, "input_schema", None) or {"type": "object"})
            Draft202012Validator.check_schema(schema)
            self._remote_names[local_name] = remote_name
            self._tools.append(ToolSchema(
                name=local_name,
                description=f"{self.config['name']} MCP · {getattr(tool, 'description', '') or remote_name}",
                input_schema=schema,
                # Remote annotations are not authority to bypass approval.
                required_permissions=["network_access", "external_send"],
                side_effect="external_send",
                cancellable=True,
                max_retries=0,
            ))

    def get_tools(self):
        return list(self._tools)

    def execute_tool(self, tool_name: str, tool_input: dict[str, Any]):
        remote_name = self._remote_names.get(tool_name)
        if remote_name is None:
            raise ValueError(f"알 수 없는 MCP 도구: {tool_name}")
        context = self.get_execution_context()
        def checkpoint():
            check_turn_cancelled()
            if context:
                context.raise_if_cancelled()
        try:
            value = asyncio.run(_bounded(_call(self.config, remote_name, tool_input), CALL_TIMEOUT, checkpoint))
            result = _response_result(tool_name, remote_name, value)
            self._connected = True
            return result
        except Exception:
            self._connected = False
            raise

    def probe_connection(self):
        return PluginStateProbe(
            "confirmed" if self._connected else "failed",
            "도구 목록을 실제 MCP 서버에서 확인했습니다." if self._connected else "마지막 MCP 호출에 실패했습니다.",
        )


def _read_store(path: Path = STORE_PATH) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _write_store(items: list[dict[str, Any]], path: Path = STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def connect_mcp(registry, manifest: dict[str, Any], *, path: Path = STORE_PATH, persist: bool = True):
    config = validate_manifest(manifest)
    tools = asyncio.run(_bounded(_discover(config), REQUEST_TIMEOUT))
    plugin = MCPPlugin(config, tools)
    existing = registry.get_plugin(plugin.name)
    if existing is not None:
        registry.unregister_plugin(plugin.name)
    try:
        registry.register_plugin(plugin)
        if persist:
            items = [item for item in _read_store(path) if _slug(item.get("name", "")) != _slug(config["name"])]
            items.append(config)
            _write_store(items, path)
    except Exception:
        if registry.get_plugin(plugin.name) is plugin:
            registry.unregister_plugin(plugin.name)
        if existing is not None:
            registry.register_plugin(existing)
        raise
    registry._load_failures.pop(plugin.name, None)
    return plugin


def load_saved_mcp_plugins(registry, *, path: Path = STORE_PATH) -> dict[str, int]:
    """Reconnect saved HTTPS servers only after an explicit user action."""
    counts = {"connected": 0, "failed": 0}
    for config in _read_store(path):
        try:
            connect_mcp(registry, config, path=path, persist=False)
            counts["connected"] += 1
        except ToolCancelledError:
            raise
        except Exception:
            counts["failed"] += 1
            name = config.get("name") if isinstance(config, dict) else "server"
            key = f"mcp:{_slug(str(name or 'server'))}"
            registry._load_failures[key] = {"error": "mcp_reconnect_failed"}
    return counts


def saved_mcp_count(*, path: Path = STORE_PATH) -> int:
    return len(_read_store(path))
