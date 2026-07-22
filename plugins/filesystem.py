"""Workspace-scoped filesystem tools."""
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema


class FilesystemPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "filesystem"
        self.description = "안전 경로 내부의 파일 탐색 및 디렉터리 트리"

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("filesystem_tree", "디렉터리 트리를 조회합니다", {
                "type": "object", "properties": {
                    "path": {"type": "string"}, "max_depth": {"type": "integer", "default": 3}
                }, "required": ["path"]}, ["filesystem_read"]),
            ToolSchema("filesystem_search", "파일 이름 패턴으로 파일을 검색합니다", {
                "type": "object", "properties": {
                    "path": {"type": "string"}, "pattern": {"type": "string"},
                    "max_results": {"type": "integer", "default": 100}
                }, "required": ["path", "pattern"]}, ["filesystem_read"]),
        ]

    @staticmethod
    def _safe_root(path: str) -> Path:
        ok, message = SafetyLayer.validate_path(path)
        if not ok:
            raise ValueError(message)
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"디렉터리가 아닙니다: {path}")
        return root

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        try:
            root = self._safe_root(str(tool_input["path"]))
            if tool_name == "filesystem_search":
                limit = max(1, min(int(tool_input.get("max_results", 100)), 1000))
                matches = []
                for item in root.rglob(str(tool_input["pattern"])):
                    if SafetyLayer.validate_path(str(item))[0]:
                        matches.append(str(item.relative_to(root)))
                    if len(matches) >= limit:
                        break
                return "\n".join(matches) if matches else "검색 결과가 없습니다."
            if tool_name == "filesystem_tree":
                max_depth = max(0, min(int(tool_input.get("max_depth", 3)), 10))
                lines = [root.name + "/"]
                for item in sorted(root.rglob("*")):
                    relative = item.relative_to(root)
                    if len(relative.parts) > max_depth or any(p.startswith(".") for p in relative.parts):
                        continue
                    lines.append("  " * len(relative.parts) + item.name + ("/" if item.is_dir() else ""))
                return "\n".join(lines)
            return f"오류: 알 수 없는 툴 '{tool_name}'"
        except Exception as exc:
            return f"오류: {exc}"
