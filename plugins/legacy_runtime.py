"""Registry adapter for Jarvis built-in runtime tools."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

from core.plugin import BasePlugin, ToolSchema

if TYPE_CHECKING:
    from core.tools import ToolExecutor


class LegacyRuntimePlugin(BasePlugin):
    def __init__(self, executor: "ToolExecutor" | None = None):
        super().__init__()
        self.name = "legacy_runtime"
        self.description = "Jarvis 내장 런타임 도구 호환 Plugin"
        self.version = "2.0.0"
        self.author = "Jarvis"
        self.auto_discover = False
        self.supported_os = ["Windows"]
        self._executor = executor

    def get_tools(self) -> List[ToolSchema]:
        from core.permission import TOOL_PERMISSION_MAP
        from core.tools import _get_legacy_tools_schema
        return [
            ToolSchema(
                name=item["name"], description=item["description"],
                input_schema=item.get("input_schema", {"type": "object"}),
                required_permissions=([TOOL_PERMISSION_MAP[item["name"]]]
                                      if item["name"] in TOOL_PERMISSION_MAP else []),
                timeout_seconds=120.0,
                cancellable=item["name"] in {"run_command", "listen", "web_search"},
            )
            for item in _get_legacy_tools_schema()
        ]

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if self._executor is None:
            raise RuntimeError("LegacyRuntimePlugin에 ToolExecutor가 연결되지 않았습니다.")
        return self._executor._execute_legacy_tool(tool_name, tool_input)

    def is_connected(self) -> bool:
        return self._executor is not None
