"""Workspace-scoped filesystem tools and declarative creation intents."""
import json
import re
import ast
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult
from core.workspace import get_workspace_manager


class FilesystemPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "filesystem"
        self.description = "선택한 작업 폴더 안의 파일·프로젝트 생성과 탐색"
        self.workspace = get_workspace_manager()

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("filesystem_read_file", "현재 작업 폴더의 실제 파일 내용을 줄 범위로 읽습니다. 파일을 변경하지 않습니다.", {
                "type": "object", "properties": {
                    "filename": {"type": "string", "minLength": 1,
                                 "description": "현재 작업 폴더 안의 실제 파일 이름 또는 상대 경로"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1,
                                   "description": "포함할 시작 줄 번호(1부터). 생략하면 1줄부터 읽습니다."},
                    "end_line": {"type": "integer", "minimum": 1,
                                 "description": "포함할 마지막 줄 번호. 생략하면 파일 끝까지 읽습니다. 한 줄 조회는 start_line과 같은 번호를 지정합니다."},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 32000, "default": 16000,
                                  "description": "반환할 최대 문자 수. 넘으면 truncated=true로 일부만 반환합니다. 줄 범위를 대신하지 않습니다."},
                }, "required": ["filename"], "additionalProperties": False}, ["filesystem_read"],
                side_effect="read", verification_required=True),
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
                "filesystem.read_file", "기존 파일의 내용이나 특정 줄을 읽고 확인", "filesystem_read_file",
                ["파일 내용", "파일의 내용", "첫 줄", "첫 번째 줄", "첫번째 줄"],
                [SlotSchema("filename", "읽을 실제 파일의 이름 또는 상대 경로", "어떤 파일을 읽을지 이름을 알려주세요.")],
                execution_hints=["읽", "알려", "보여", "확인", "뭐", "무슨"],
                utterance_patterns=[
                    r"(?:파일|문서|\.[a-z0-9]{1,10}).*(?:읽어|읽고|내용.*(?:알려|보여|확인|뭐|무슨)|(?:첫|\d+).*줄)",
                    r"(?:파일|문서).*(?:작성되어|적혀|쓰여|기록되어).*(?:있|내용)",
                ], request_type="query",
            ),
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
                utterance_patterns=[
                    r"[A-Za-z0-9가-힣_.-]+\.[A-Za-z0-9]{1,10}.*(?:수정|작성|고쳐|바꿔|변경)",
                    r"[A-Za-z0-9가-힣_.-]+\s*파일.*(?:수정|작성|고쳐|바꿔|변경)",
                    r"(?:해당|그)\s*파일.*(?:수정|작성|고쳐|바꿔|변경|코드|코딩)",
                ],
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
        if intent_name == "filesystem.read_file":
            matches = self.resolve_file_candidates(normalized)
            if len(matches) == 1:
                slots["filename"] = matches[0]
            else:
                # No 'most recently modified' guess: a reference can be filled
                # only from established dialogue slots or a unique real file.
                quoted = re.search(r'["\'“‘]([^"\'”’]+\.[A-Za-z0-9]{1,10})["\'”’]', normalized)
                if quoted and not matches:
                    slots["filename"] = quoted.group(1)
            if re.search(r"첫\s*(?:번째\s*)?줄", normalized):
                slots.update(start_line=1, end_line=1)
            else:
                span = re.search(r"(\d+)\s*(?:번째\s*)?줄\s*(?:부터|~|-)\s*(\d+)\s*(?:번째\s*)?줄", normalized)
                single = re.search(r"(\d+)\s*(?:번째\s*)?줄", normalized)
                if span:
                    slots.update(start_line=int(span.group(1)), end_line=int(span.group(2)))
                elif single:
                    slots.update(start_line=int(single.group(1)), end_line=int(single.group(1)))
        elif intent_name == "filesystem.create_project":
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
            candidates = self.resolve_file_candidates(normalized)
            if len(candidates) == 1:
                slots["filename"] = candidates[0]
            match = re.search(
                r"(?P<filename>[A-Za-z0-9가-힣_.-]+\.[A-Za-z0-9]{1,10})",
                normalized,
            )
            if match and not slots.get("filename"):
                slots["filename"] = match.group("filename")
            else:
                stem_match = re.search(
                    r"(?P<stem>[A-Za-z0-9가-힣_.-]+)\s*파일", normalized
                )
                if stem_match:
                    matches = self._files_with_stem(stem_match.group("stem"))
                    if len(matches) == 1:
                        slots["filename"] = str(matches[0].relative_to(self._workspace_root()))
            # Referents such as '그 파일' must come from the active request's
            # slots. Disk modification time is not conversational evidence.
            if not slots.get("instruction"):
                instruction = self._extract_write_instruction(normalized)
                if instruction:
                    slots["instruction"] = normalized
        return slots

    def intent_applicable(self, intent_name: str, text: str) -> bool:
        """Reject lexical write matches where the user is describing stored text.

        This is a non-execution guard for the fallback router; the semantic
        interpreter remains responsible for arbitrary phrasing.
        """
        if intent_name not in {"filesystem.write_file", "filesystem.create_file"}:
            return True
        observable = re.sub(r"(?:작성|기록|저장)(?:되어|돼|된|한)|(?:적혀|쓰여)", "", text)
        return bool(re.search(r"(?:만들|생성|추가|작성|수정|고쳐|바꿔|변경|코딩|입력|써\s*줘)", observable))

    def resolve_file_candidates(self, request: str, *, max_files: int = 5000) -> List[str]:
        """Ground a mention in actual workspace paths, preserving spaces.

        Exact longest path/name wins; a basename occurring in several folders
        remains ambiguous. Traversal is bounded and excludes runtime/private
        metadata, virtualenvs and symlink escapes.
        """
        try:
            root = self._workspace_root()
            query = str(request).casefold().replace("\\", "/")
            found = []
            seen = 0
            for folder, directories, filenames in os.walk(root, followlinks=False):
                relative = Path(folder).relative_to(root)
                directories[:] = sorted(d for d in directories if not d.startswith(".")
                                         and d not in {"node_modules", "__pycache__", "build", "dist"}
                                         and len(relative.parts) < 6
                                         and not (Path(folder) / d).is_symlink())
                for name in sorted(filenames):
                    seen += 1
                    if seen > max_files:
                        break
                    path = Path(folder) / name
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        continue
                    rel = path.relative_to(root).as_posix()
                    terms = [rel, name]
                    # Explicit extension-free requests ('인수인계 파일') still
                    # resolve using full actual stems, never the last word.
                    if path.stem and (path.stem.casefold() + " 파일") in query:
                        terms.append(path.stem)
                    scores = [len(term) for term in terms if term.casefold() in query]
                    if scores:
                        found.append((max(scores), rel))
                if seen > max_files:
                    break
            if not found:
                return []
            best = max(score for score, _ in found)
            return [path for score, path in found if score == best]
        except (OSError, ValueError):
            return []

    @staticmethod
    def _extract_write_instruction(text: str) -> str:
        """대상 선택·일반 수정 동작을 제외한 실제 변경 요구가 있는지 판별한다."""
        value = text.strip()
        value = re.sub(
            r"(?:[A-Za-z0-9가-힣_.-]+\.[A-Za-z0-9]{1,10}|"
            r"[A-Za-z0-9가-힣_.-]+\s*파일|(?:해당|그)\s*파일)(?:의|을|를|에|에서)?",
            " ",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(
            r"(?:코드|소스코드|내용)?(?:을|를)?\s*"
            r"(?:수정|작성|고쳐|바꿔|변경)(?:해\s*줘|해주세요|할\s*거야|해줘)?",
            " ",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"[\s.,!?\"'“”‘’]+", "", value)
        return value

    def _workspace_root(self) -> Path:
        from core.turn_context import current_turn_context
        context = current_turn_context()
        path = context.workspace_path if context is not None else self.workspace.get_workspace_path()
        if not path:
            raise ValueError("먼저 상단의 폴더 선택 버튼에서 작업 폴더를 선택해 주세요.")
        return Path(path).resolve()

    def _files_with_stem(self, stem: str) -> List[Path]:
        try:
            root = self._workspace_root()
            return [
                path for path in root.rglob("*")
                if path.is_file() and path.stem.casefold() == stem.casefold()
            ]
        except (OSError, ValueError):
            return []

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

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        from core.turn_context import check_turn_cancelled
        try:
            check_turn_cancelled()
            if tool_name == "filesystem_read_file":
                root = self._workspace_root()
                target = self._safe_child(root, str(tool_input["filename"]))
                if not target.is_file():
                    raise ValueError(f"읽을 파일이 존재하지 않습니다: {target.name}")
                if target.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("4 MiB를 초과하는 파일은 전용 문서 도구로 읽어주세요.")
                raw = target.read_bytes()
                if b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                    raise ValueError("바이너리 파일은 전용 문서 도구로 읽어주세요.")
                encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                try:
                    decoded = raw.decode(encoding)
                except UnicodeDecodeError:
                    encoding = "cp949"
                    decoded = raw.decode(encoding)
                lines = decoded.splitlines(keepends=True)
                start = int(tool_input.get("start_line", 1))
                end = int(tool_input.get("end_line", len(lines) or 1))
                if start < 1 or end < start:
                    raise ValueError("줄 범위는 1부터 시작하며 끝 줄이 시작 줄보다 작을 수 없습니다.")
                limit = max(1, min(int(tool_input.get("max_chars", 16000)), 32000))
                content = "".join(lines[start - 1:end])
                payload = {"path": str(target), "start_line": start,
                           "end_line": min(end, len(lines)), "total_lines": len(lines),
                           "content": content[:limit], "truncated": len(content) > limit,
                           "encoding": encoding, "sha256": hashlib.sha256(raw).hexdigest()}
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("file_content", "실제 파일에서 지정된 줄을 읽었습니다.", payload)],
                )
            if tool_name == "filesystem_create_project":
                root = self._workspace_root()
                target = self._safe_child(root, str(tool_input["name"]))
                check_turn_cancelled()
                target.mkdir(parents=False, exist_ok=False)
                # 이어지는 “파일을 만들어줘”가 새 프로젝트 내부에서 실행되도록 한다.
                self.workspace.set_workspace(str(target))
                output = json.dumps({
                    "status": "created", "type": "directory", "path": str(target),
                }, ensure_ascii=False)
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=output,
                    evidence=[Evidence(
                        "filesystem_state",
                        "프로젝트 디렉터리 생성을 확인했습니다.",
                        {"path": str(target), "is_directory": target.is_dir()},
                    )],
                    artifacts=[Artifact("directory", str(target))],
                )
            if tool_name == "filesystem_create_file":
                root = self._workspace_root()
                target = self._safe_child(root, str(tool_input["filename"]))
                check_turn_cancelled()
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x", encoding="utf-8") as stream:
                    stream.write(str(tool_input.get("content", "")))
                output = json.dumps({
                    "status": "created", "type": "file", "path": str(target),
                }, ensure_ascii=False)
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=output,
                    evidence=[Evidence(
                        "filesystem_state",
                        "파일 생성을 확인했습니다.",
                        {
                            "path": str(target),
                            "is_file": target.is_file(),
                            "size": target.stat().st_size,
                        },
                    )],
                    artifacts=[Artifact("file", str(target))],
                )
            if tool_name == "filesystem_write_file":
                root = self._workspace_root()
                target = self._safe_child(root, str(tool_input["filename"]))
                if not target.is_file():
                    raise ValueError(f"수정할 파일이 존재하지 않습니다: {target.name}")
                previous = target.read_bytes()
                content = self._generate_file_content(
                    target, str(tool_input.get("instruction", ""))
                )
                updated = content.encode("utf-8")
                if updated == previous:
                    raise ValueError("생성된 내용이 기존 파일과 같아 실제 변경이 없습니다.")
                check_turn_cancelled()
                if target.read_bytes() != previous:
                    raise ValueError("코드를 생성하는 동안 파일이 변경되어 덮어쓰지 않았습니다. 최신 파일을 다시 읽어주세요.")
                target.write_bytes(updated)
                output = json.dumps({
                    "status": "written", "type": "file", "path": str(target),
                    "size": len(updated), "changed": True,
                    "before_sha256": hashlib.sha256(previous).hexdigest(),
                    "after_sha256": hashlib.sha256(updated).hexdigest(),
                }, ensure_ascii=False)
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=output,
                    evidence=[Evidence(
                        "content_hash",
                        "파일 내용 변경과 저장을 확인했습니다.",
                        {
                            "path": str(target),
                            "before_sha256": hashlib.sha256(previous).hexdigest(),
                            "after_sha256": hashlib.sha256(updated).hexdigest(),
                            "size": len(updated),
                        },
                    )],
                    artifacts=[Artifact("file", str(target), {"changed": True})],
                )
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
            return ToolRunResult.failed(
                tool_name=tool_name,
                error=f"알 수 없는 툴 '{tool_name}'",
            )
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name == "filesystem_read_file":
            try:
                payload = json.loads(result)
                content = payload["content"]
                longest = max((len(m.group()) for m in re.finditer(r"`+", content)), default=0)
                fence = "`" * max(3, longest + 1)
                if payload["start_line"] > payload["total_lines"]:
                    return f"{Path(payload['path']).name}에는 요청한 줄이 없습니다. 전체 {payload['total_lines']}줄입니다."
                label = f"{Path(payload['path']).name} · {payload['start_line']}–{payload['end_line']}줄"
                note = "\n표시 한도를 넘어 일부만 표시했습니다." if payload.get("truncated") else ""
                return f"{label}\n\n{fence}text\n{content}\n{fence}{note}"
            except (json.JSONDecodeError, KeyError, TypeError):
                return result
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
            "시니어 개발자 관점에서 요청을 정확히 충족하고 실제 실행 가능한 완성 파일을 "
            "작성하세요. 기존의 유효한 구조와 동작은 요청상 필요하지 않으면 보존하고, 오류 처리·"
            "가독성·유지보수성을 작업 규모에 맞게 적용하세요. 단순한 요구에는 불필요한 추상화나 "
            "상용구를 추가하지 마세요. 설명이나 Markdown 코드 펜스 없이 저장할 전체 원문만 "
            "반환하세요."
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
        requested_literals = [
            left or right
            for left, right in re.findall(r'"([^"]+)"|\'([^\']+)\'', instruction)
        ]
        missing_literals = [literal for literal in requested_literals if literal not in result]
        if missing_literals:
            raise RuntimeError(
                "코드 모델 결과에 사용자가 지정한 값이 반영되지 않았습니다: "
                + ", ".join(missing_literals)
            )
        return result + "\n"
