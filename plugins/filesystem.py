"""Workspace-scoped filesystem tools and declarative creation intents."""
import json
import re
import ast
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
            ToolSchema("filesystem_write_file", "기존 파일에 요청한 내용을 작성하거나 수정합니다", {
                "type": "object", "properties": {
                    "filename": {"type": "string"},
                    "instruction": {"type": "string"},
                }, "required": ["filename", "instruction"]}, ["filesystem_write"]),
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
            IntentSchema(
                "filesystem.write_file",
                "기존 파일의 내용 또는 소스코드 작성·수정",
                "filesystem_write_file",
                ["파일", "코드", "소스코드", "코딩"],
                [
                    SlotSchema("filename", "수정할 파일 이름",
                               "어떤 파일을 수정할지 파일명을 알려주세요, 보스."),
                    SlotSchema("instruction", "파일에 반영할 내용",
                               "파일에 어떤 내용을 작성할지 알려주세요, 보스."),
                ],
                execution_hints=["작성", "수정", "고쳐", "코딩", "입력", "써줘"],
                follow_up_hints=["해당 파일", "그 파일", "내용을", "코드를"],
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
            if not slots.get("name") and re.fullmatch(r"[A-Za-z0-9가-힣_.-]+", normalized):
                slots["name"] = normalized
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
        elif intent_name == "filesystem.write_file":
            match = re.search(
                r"(?P<filename>[A-Za-z0-9가-힣_.-]+\.[A-Za-z0-9]{1,10})",
                normalized,
            )
            if match:
                slots["filename"] = match.group("filename")
            else:
                stem_match = re.search(
                    r"(?P<stem>[A-Za-z0-9가-힣_.-]+)\s*파일", normalized
                )
                if stem_match:
                    matches = [
                        path for path in self._workspace_root().rglob("*")
                        if path.is_file() and path.stem.casefold()
                        == stem_match.group("stem").casefold()
                    ]
                    if len(matches) == 1:
                        slots["filename"] = str(matches[0].relative_to(self._workspace_root()))
            if (not slots.get("filename")
                    and any(reference in normalized for reference in ("해당 파일", "그 파일"))):
                recent = self._most_recent_file()
                if recent:
                    slots["filename"] = str(recent.relative_to(self._workspace_root()))
            instruction = normalized
            if not slots.get("instruction") and instruction and not re.fullmatch(
                r"[A-Za-z0-9가-힣_.-]+\.[A-Za-z0-9]{1,10}", instruction
            ):
                slots["instruction"] = instruction
        return slots

    def _workspace_root(self) -> Path:
        path = self.workspace.get_workspace_path()
        if not path:
            raise ValueError("먼저 상단의 폴더 선택 버튼에서 작업 폴더를 선택해 주세요.")
        return Path(path).resolve()

    def _most_recent_file(self) -> Path | None:
        try:
            files = [path for path in self._workspace_root().rglob("*") if path.is_file()]
            return max(files, key=lambda path: path.stat().st_mtime) if files else None
        except (OSError, ValueError):
            return None

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
                with target.open("x", encoding="utf-8") as stream:
                    stream.write(str(tool_input.get("content", "")))
                return json.dumps({
                    "status": "created", "type": "file", "path": str(target),
                }, ensure_ascii=False)
            if tool_name == "filesystem_write_file":
                root = self._workspace_root()
                target = self._safe_child(root, str(tool_input["filename"]))
                if not target.is_file():
                    raise ValueError(f"수정할 파일이 존재하지 않습니다: {target.name}")
                content = self._generate_file_content(
                    target, str(tool_input.get("instruction", ""))
                )
                target.write_text(content, encoding="utf-8")
                return json.dumps({
                    "status": "written", "type": "file", "path": str(target),
                    "size": len(content.encode("utf-8")),
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
        if tool_name not in {
            "filesystem_create_project", "filesystem_create_file", "filesystem_write_file"
        }:
            return result
        try:
            payload = json.loads(result)
            path = Path(payload["path"])
            if tool_name == "filesystem_create_project":
                return f"{path.name} 프로젝트 폴더를 실제로 생성했습니다."
            if tool_name == "filesystem_write_file":
                return f"{path.name} 파일 내용을 작성하고 실제 저장을 확인했습니다."
            return f"{path.name} 파일을 실제로 생성했습니다."
        except (json.JSONDecodeError, KeyError, TypeError):
            return result

    @staticmethod
    def _generate_file_content(target: Path, instruction: str) -> str:
        """코딩 전용 모델로 완성 파일 내용을 만들며 실패 응답은 저장하지 않는다."""
        from core.llm import get_llm_client

        existing = target.read_text(encoding="utf-8", errors="replace")
        prompt = (
            f"파일명: {target.name}\n"
            f"현재 내용:\n{existing}\n\n"
            f"사용자 요청:\n{instruction}\n\n"
            "요청을 반영한 파일의 전체 내용을 출력하세요. 설명이나 Markdown 코드 펜스 없이 "
            "저장할 원문만 반환하세요."
        )
        result = get_llm_client("coding").chat([
            {
                "role": "system",
                "content": (
                    "당신은 파일 내용을 생성하는 코딩 엔진입니다. 파일을 직접 수정할 수 없다는 "
                    "설명이나 사과를 하지 마세요. 반드시 사용자 요청이 반영된 완성 파일 원문만 "
                    "출력하세요."
                ),
            },
            {"role": "user", "content": prompt},
        ]).strip()
        result = re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", result)
        result = re.sub(r"\s*```$", "", result).strip()
        if not result or result.casefold().startswith(("오류:", "error:")):
            raise RuntimeError(result or "코드 모델이 빈 내용을 반환했습니다.")
        refusal_terms = (
            "직접 파일을 수정", "코드를 작성하는 기능은 없", "도와드릴 수 없습니다",
            "죄송합니다", "i can't", "i cannot", "unable to",
        )
        if any(term in result.casefold() for term in refusal_terms):
            raise RuntimeError("코드 모델이 파일 내용 대신 거절 문장을 반환했습니다.")
        if target.suffix.casefold() == ".py":
            try:
                ast.parse(result)
            except SyntaxError as exc:
                raise RuntimeError(f"코드 모델이 유효하지 않은 Python 코드를 반환했습니다: {exc}")
        return result + "\n"
