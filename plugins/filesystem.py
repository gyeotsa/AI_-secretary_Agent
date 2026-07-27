"""Workspace-scoped filesystem tools and declarative creation intents."""
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.workspace import get_workspace_manager


class FilesystemPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "filesystem"
        self.description = "선택한 작업 폴더 안의 파일·프로젝트 생성과 탐색"
        self.workspace = get_workspace_manager()

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
            ToolSchema("filesystem_create_project", "현재 작업 폴더 안에 프로젝트 폴더를 생성합니다", {
                "type": "object", "properties": {
                    "name": {"type": "string"},
                }, "required": ["name"]}, ["filesystem_write"]),
            ToolSchema("filesystem_create_file", "현재 작업 폴더 안에 파일을 생성합니다", {
                "type": "object", "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string", "default": ""},
                }, "required": ["filename"]}, ["filesystem_write"]),
        ]

    def get_intents(self) -> List[IntentSchema]:
        return [
            IntentSchema(
                "filesystem.create_project",
                "선택한 Workspace에 새 프로젝트 폴더 생성",
                "filesystem_create_project",
                ["프로젝트"],
                [SlotSchema("name", "프로젝트 폴더 이름",
                            "새 프로젝트의 이름을 알려주세요, 보스.")],
                execution_hints=["생성", "만들", "추가"],
                follow_up_hints=["이름으로", "로 해줘", "라고 해줘"],
            ),
            IntentSchema(
                "filesystem.create_file",
                "선택한 Workspace에 새 파일 생성",
                "filesystem_create_file",
                ["파일"],
                [SlotSchema("filename", "확장자를 포함한 파일 이름",
                            "생성할 파일 이름을 확장자와 함께 알려주세요, 보스.")],
                execution_hints=["생성", "만들", "추가", "작성"],
                follow_up_hints=["이름으로", "로 해줘", "라고 해줘"],
            ),
        ]

    @staticmethod
    def _clean_name(value: str) -> str:
        value = value.strip().strip("\"'“”‘’")
        value = re.sub(r"(?:이라는|라는|이란|란)?\s*이름(?:으로)?$", "", value).strip()
        return value

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        normalized = text.strip()
        if intent_name == "filesystem.create_project":
            patterns = (
                r"(?P<name>[^,\s]+)(?:이라는|라는)\s*이름(?:으로)?",
                r"(?:이름(?:은|을|으로)?\s*)(?P<name>[^,\s]+)",
                r"(?P<name>[A-Za-z0-9가-힣_.-]+)\s*(?:로|으로)\s*해줘",
            )
            for pattern in patterns:
                match = re.search(pattern, normalized, re.I)
                if match:
                    slots["name"] = self._clean_name(match.group("name"))
                    break
        elif intent_name == "filesystem.create_file":
            match = re.search(
                r"(?P<filename>[A-Za-z0-9가-힣_.-]+\.[A-Za-z0-9]{1,10})",
                normalized,
            )
            if match:
                slots["filename"] = match.group("filename")
            elif "파이썬" in normalized:
                # 파일명은 추측하지 않고 확장자 정보만 질문에 활용한다.
                slots.pop("filename", None)
        return slots

    def _workspace_root(self) -> Path:
        path = self.workspace.get_workspace_path()
        if not path:
            raise ValueError("먼저 상단의 폴더 선택 버튼에서 작업 폴더를 선택해 주세요.")
        return Path(path).resolve()

    @staticmethod
    def _safe_child(root: Path, name: str) -> Path:
        if not name or name in {".", ".."}:
            raise ValueError("유효한 이름을 입력해 주세요.")
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            raise ValueError("작업 폴더 외부 경로는 생성할 수 없습니다.")
        return target

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
            if tool_name == "filesystem_create_project":
                root = self._workspace_root()
                target = self._safe_child(root, str(tool_input["name"]))
                target.mkdir(parents=False, exist_ok=False)
                # 이어지는 “파일을 만들어줘”가 새 프로젝트 내부에서 실행되도록 한다.
                self.workspace.set_workspace(str(target))
                return json.dumps({
                    "status": "created", "type": "directory", "path": str(target),
                }, ensure_ascii=False)
            if tool_name == "filesystem_create_file":
                root = self._workspace_root()
                target = self._safe_child(root, str(tool_input["filename"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(tool_input.get("content", "")), encoding="utf-8")
                return json.dumps({
                    "status": "created", "type": "file", "path": str(target),
                }, ensure_ascii=False)
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

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name not in {"filesystem_create_project", "filesystem_create_file"}:
            return result
        try:
            payload = json.loads(result)
            path = Path(payload["path"])
            if tool_name == "filesystem_create_project":
                return f"프로젝트 폴더를 실제로 생성했습니다: {path}"
            return f"파일을 실제로 생성했습니다: {path}"
        except (json.JSONDecodeError, KeyError, TypeError):
            return result
