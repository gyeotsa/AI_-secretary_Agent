"""Repository-scoped, rollback-safe coding transaction primitives."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, List, Optional
import difflib
import json
import os
import subprocess
import sys
import tempfile
import re
import shutil

from core.project_indexer import ProjectIndexer
from core.turn_context import check_turn_cancelled


@dataclass(frozen=True)
class RepositorySnapshot:
    root: str
    files: List[str]
    readme_files: List[str]
    dependency_files: List[str]
    git_branch: str = ""
    git_status: str = ""


@dataclass(frozen=True)
class FileEdit:
    path: str
    old_text: str
    new_text: str
    expected_sha256: str = ""
    create: bool = False


@dataclass(frozen=True)
class CodingPlan:
    request: str
    related_files: List[str]
    related_symbols: List[dict[str, Any]]
    impact_scope: List[str]
    validation_commands: List[List[str]]
    repository: RepositorySnapshot


@dataclass
class CodingTransactionResult:
    status: str
    changed_files: List[str] = field(default_factory=list)
    diff: str = ""
    validation_output: str = ""
    error: str = ""
    rolled_back: bool = False
    attempt_count: int = 1
    review_issues: List[str] = field(default_factory=list)
    summary: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


class CodingAgent:
    IGNORED_PARTS = {
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".pytest-tmp",
        "node_modules", "dist", "build", ".idea", ".vscode",
    }
    DEPENDENCY_FILES = {
        "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "cargo.toml", "go.mod",
    }
    MAX_PATCH_LINES = 2000

    def __init__(self, root: str | Path, denied_parts: Optional[set[str]] = None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"유효한 저장소 폴더가 아닙니다: {self.root}")
        self.denied_parts = {
            str(part).casefold() for part in (denied_parts or set()) if str(part).strip()
        }

    def _resolve(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError(f"Workspace 외부 파일은 변경할 수 없습니다: {relative_path}")
        relative = target.relative_to(self.root)
        if any(part.casefold() in self.denied_parts for part in relative.parts):
            raise ValueError(f"보호된 프로젝트 영역은 자기 수정으로 변경할 수 없습니다: {relative_path}")
        return target

    def analyze_repository(self, max_files: int = 5000) -> RepositorySnapshot:
        files = []
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if any(
                part.casefold() in self.IGNORED_PARTS or part.casefold() in self.denied_parts
                for part in relative.parts
            ):
                continue
            if path.is_file():
                files.append(str(relative))
                if len(files) >= max_files:
                    break
        readmes = [name for name in files if Path(name).name.casefold().startswith("readme")]
        dependencies = [
            name for name in files
            if Path(name).name.casefold() in self.DEPENDENCY_FILES
        ]
        branch = self._git(["branch", "--show-current"])
        status = self._git(["status", "--short"])
        return RepositorySnapshot(
            str(self.root), sorted(files), sorted(readmes), sorted(dependencies),
            branch.strip(), status.rstrip(),
        )

    def build_plan(self, request: str, max_results: int = 30) -> CodingPlan:
        """Build a deterministic impact plan from repository and symbol evidence."""
        snapshot = self.analyze_repository()
        english_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", request)
        korean_stopwords = {
            "수정", "변경", "구현", "추가", "삭제", "해줘", "해주세요", "코드", "프로젝트",
            "기능", "문제", "부분", "아니스", "자비스", "본인", "자기",
        }
        korean_tokens = [
            token for token in re.findall(r"[가-힣]{2,}", request)
            if token not in korean_stopwords
        ]
        tokens = list(dict.fromkeys([*english_tokens, *korean_tokens]))
        file_scores: dict[str, float] = {}
        related_symbols: list[dict[str, Any]] = []

        def score_file(path: str, points: float) -> None:
            normalized = str(path).replace("\\", "/")
            file_scores[normalized] = file_scores.get(normalized, 0.0) + points

        # Full relative paths carry important architectural evidence that a
        # basename-only index cannot see (for example ``ui/main_window.py``).
        # Rank them before content matches so a common word in documentation
        # cannot displace the implementation file from a bounded plan.
        for relative_path in snapshot.files:
            normalized_path = str(relative_path).replace("\\", "/")
            folded_path = normalized_path.casefold()
            path_parts = set(Path(normalized_path).parts)
            folded_parts = {part.casefold() for part in path_parts}
            for token in tokens[:12]:
                folded_token = token.casefold()
                if folded_token in folded_parts:
                    score_file(normalized_path, 12.0)
                elif folded_token in folded_path:
                    score_file(normalized_path, 7.0)
        with tempfile.TemporaryDirectory(prefix="jarvis-index-") as temp_dir:
            indexer = ProjectIndexer(str(Path(temp_dir) / "project.db"))
            indexer.set_project_root(str(self.root))
            indexer.index_project(list(self.IGNORED_PARTS | self.denied_parts))
            for token in tokens[:12]:
                for item in indexer.search_files(token, "name")[:max_results]:
                    score_file(str(Path(item["path"]).relative_to(self.root)), 10.0)
                for item in indexer.search_files(token, "content")[:max_results]:
                    score_file(str(Path(item["path"]).relative_to(self.root)), 2.0)
                for item in indexer.search_symbols(token)[:max_results]:
                    normalized = dict(item)
                    normalized["file_path"] = str(
                        Path(normalized["file_path"]).relative_to(self.root)
                    )
                    related_symbols.append(normalized)
                    score_file(normalized["file_path"], 14.0)

        source_extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".cpp", ".c", ".cs"}
        explicit_test_request = bool(re.search(r"(?:test|pytest|테스트)", request, re.IGNORECASE))
        for path in list(file_scores):
            candidate = Path(path)
            if candidate.suffix.casefold() in source_extensions:
                file_scores[path] += 1.0
            if not explicit_test_request and (
                candidate.name.casefold().startswith("test_") or "tests" in candidate.parts
            ):
                file_scores[path] -= 1.0
        ordered_files = [
            path for path, _score in sorted(
                file_scores.items(), key=lambda item: (-item[1], item[0].count("/"), item[0].casefold())
            )[:max_results]
        ]
        unique_symbols: list[dict[str, Any]] = []
        seen_symbols: set[tuple[str, str, int]] = set()
        for symbol in related_symbols:
            key = (
                str(symbol.get("file_path", "")), str(symbol.get("name", "")),
                int(symbol.get("line_number", 0) or 0),
            )
            if key not in seen_symbols:
                seen_symbols.add(key)
                unique_symbols.append(symbol)
        impact = sorted(set(ordered_files + self._related_tests(ordered_files)))
        validation = self._validation_commands_for_relative_paths(impact)
        return CodingPlan(
            request=request,
            related_files=ordered_files,
            related_symbols=unique_symbols[:max_results],
            impact_scope=impact,
            validation_commands=validation,
            repository=snapshot,
        )

    def apply_transaction(
        self,
        edits: List[FileEdit],
        validation_commands: Optional[List[List[str]]] = None,
    ) -> CodingTransactionResult:
        if not edits:
            return CodingTransactionResult("failed", error="적용할 최소 변경이 없습니다.")
        originals: dict[Path, Optional[bytes]] = {}
        updates: dict[Path, bytes] = {}
        diffs = []
        applied = False
        try:
            for edit in edits:
                target = self._resolve(edit.path)
                if edit.create:
                    if target.exists():
                        raise ValueError(f"생성 대상이 이미 존재합니다: {edit.path}")
                    if edit.old_text:
                        raise ValueError(f"새 파일 생성의 old_text는 비어 있어야 합니다: {edit.path}")
                    if not edit.new_text:
                        raise ValueError(f"빈 새 파일은 생성할 수 없습니다: {edit.path}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    originals[target] = None
                    updates[target] = edit.new_text.encode("utf-8")
                    diffs.extend(difflib.unified_diff(
                        [], edit.new_text.splitlines(keepends=True),
                        fromfile="/dev/null", tofile=f"b/{edit.path}",
                    ))
                    continue
                if not target.is_file():
                    raise ValueError(f"변경 대상 파일이 없습니다: {edit.path}")
                raw = target.read_bytes()
                digest = sha256(raw).hexdigest()
                if edit.expected_sha256 and digest != edit.expected_sha256:
                    raise RuntimeError(f"사용자 변경 충돌을 감지했습니다: {edit.path}")
                text, encoding = self._decode(raw)
                if not edit.old_text:
                    raise ValueError(f"전체 파일 재생성을 막기 위해 old_text가 필요합니다: {edit.path}")
                occurrences = text.count(edit.old_text)
                if occurrences != 1:
                    raise ValueError(
                        f"최소 patch 기준 문자열은 정확히 한 번 존재해야 합니다: "
                        f"{edit.path} (일치 {occurrences}개)"
                    )
                updated_text = text.replace(edit.old_text, edit.new_text, 1)
                originals[target] = raw
                updates[target] = updated_text.encode(encoding)
                diffs.extend(difflib.unified_diff(
                    text.splitlines(keepends=True), updated_text.splitlines(keepends=True),
                    fromfile=f"a/{edit.path}", tofile=f"b/{edit.path}",
                ))

            diff_text = "".join(diffs)
            review_issues = self.review_diff(diff_text)
            if review_issues:
                raise ValueError("최종 diff 자체 검토 실패: " + "; ".join(review_issues))

            for target, updated in updates.items():
                self._atomic_write(target, updated)
            applied = True

            commands = validation_commands or self._default_validation_commands(list(updates))
            validation_output = self._run_validations(commands)
            return CodingTransactionResult(
                "completed", [str(path.relative_to(self.root)) for path in updates],
                diff_text, validation_output, review_issues=review_issues,
            )
        except Exception as exc:
            rollback_error = ""
            for target, raw in originals.items():
                try:
                    if raw is None:
                        if target.exists():
                            target.unlink()
                    else:
                        self._atomic_write(target, raw)
                except Exception as rollback_exc:
                    rollback_error = f"; 롤백 오류: {rollback_exc}"
            return CodingTransactionResult(
                "failed", error=f"{exc}{rollback_error}", rolled_back=applied,
            )

    def apply_with_recovery(
        self,
        initial_edits: List[FileEdit],
        repair_callback: Callable[[CodingTransactionResult, int], Optional[List[FileEdit]]],
        validation_commands: Optional[List[List[str]]] = None,
        max_attempts: int = 3,
    ) -> CodingTransactionResult:
        """Retry only with a caller-proposed new minimal patch after full rollback."""
        edits = initial_edits
        last_result = CodingTransactionResult("failed", error="실행되지 않았습니다.")
        for attempt in range(1, max(1, min(max_attempts, 5)) + 1):
            check_turn_cancelled()
            last_result = self.apply_transaction(edits, validation_commands)
            last_result.attempt_count = attempt
            if last_result.succeeded:
                return last_result
            if attempt == max(1, min(max_attempts, 5)):
                break
            replacement = repair_callback(last_result, attempt)
            if not replacement:
                return last_result
            edits = replacement
        return last_result

    def execute_request(self, request: str, llm, max_attempts: int = 3):
        """Turn natural language into reviewed JSON edits; only this class writes files."""
        plan = self.build_plan(request)
        proposal = self._propose_edits(request, plan, llm)
        checkpoint = getattr(llm, "check_cancelled", check_turn_cancelled)
        checkpoint()
        self._validate_test_policy(proposal)

        def repair(failure: CodingTransactionResult, _attempt: int):
            replacement = self._propose_edits(request, plan, llm, failure.error)
            checkpoint()
            self._validate_test_policy(replacement)
            return [FileEdit(**item) for item in replacement["edits"]]

        result = self.apply_with_recovery(
            [FileEdit(**item) for item in proposal["edits"]],
            repair,
            max_attempts=max_attempts,
        )
        result.summary = str(proposal.get("summary", "")).strip()
        return plan, result

    def _propose_edits(self, request: str, plan: CodingPlan, llm,
                       failure_log: str = "") -> dict[str, Any]:
        contexts = []
        for relative in plan.related_files[:12]:
            path = self._resolve(relative)
            if path.is_file() and path.stat().st_size <= 200_000:
                raw = path.read_bytes()
                text, _encoding = self._decode(raw)
                contexts.append({
                    "path": relative, "sha256": sha256(raw).hexdigest(),
                    "content": text,
                })
        prompt = {
            "request": request,
            "related_symbols": plan.related_symbols,
            "impact_scope": plan.impact_scope,
            "repository_files": plan.repository.files[:300],
            "file_context": contexts,
            "previous_failure": failure_log[-8000:],
            "output_contract": {
                "summary": "string",
                "change_kind": "feature|bugfix|refactor|docs|config",
                "edits": [{
                    "path": "workspace-relative path", "old_text": "exact existing text",
                    "new_text": "replacement", "expected_sha256": "context sha256",
                    "create": False,
                }],
            },
        }
        response = llm.chat([
            {"role": "system", "content": (
                "당신은 코드 변경안을 제안하는 시니어 개발자입니다. 파일을 직접 수정하거나 "
                "완성 파일 전체를 반환하지 마세요. 제공된 JSON 계약의 단일 JSON 객체만 반환하세요. "
                "기존 파일은 가장 작은 exact old_text/new_text 치환을 사용하고 새 파일만 create=true로 "
                "표시하세요. feature에는 실행되는 테스트 edit를 반드시 포함하세요."
            )},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ]).strip()
        response = re.sub(r"^```(?:json)?\s*", "", response, flags=re.IGNORECASE)
        response = re.sub(r"\s*```$", "", response).strip()
        try:
            proposal = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Coding 모델이 유효한 JSON 변경안을 반환하지 않았습니다: {exc}") from exc
        if not isinstance(proposal, dict) or not isinstance(proposal.get("edits"), list) or not proposal["edits"]:
            raise ValueError("Coding 모델 변경안에 edits가 없습니다.")
        allowed = {"path", "old_text", "new_text", "expected_sha256", "create"}
        for item in proposal["edits"]:
            if not isinstance(item, dict) or not {"path", "old_text", "new_text"}.issubset(item):
                raise ValueError("Coding edit 계약이 올바르지 않습니다.")
            unknown = set(item) - allowed
            if unknown:
                raise ValueError(f"Coding edit에 허용되지 않은 필드가 있습니다: {sorted(unknown)}")
        return proposal

    @staticmethod
    def _validate_test_policy(proposal: dict[str, Any]):
        if proposal.get("change_kind") != "feature":
            return
        paths = [Path(str(item.get("path", ""))).name.casefold() for item in proposal["edits"]]
        if not any(name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts")) for name in paths):
            raise ValueError("새 기능 변경안에는 실행 가능한 테스트 edit가 필요합니다.")

    def review_diff(self, diff_text: str) -> List[str]:
        issues = []
        if not diff_text.strip():
            issues.append("변경 diff가 비어 있습니다.")
        added_lines = [line for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")]
        removed_lines = [line for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---")]
        if len(added_lines) + len(removed_lines) > self.MAX_PATCH_LINES:
            issues.append(f"최소 patch 한도({self.MAX_PATCH_LINES}줄)를 초과했습니다.")
        if any(marker in line for line in added_lines for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            issues.append("병합 충돌 표식이 추가되었습니다.")
        return issues

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, str]:
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig"), "utf-8-sig"
        for encoding in ("utf-8", "cp949"):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        raise UnicodeError("UTF-8 또는 CP949 텍스트 파일만 안전하게 변경할 수 있습니다.")

    @staticmethod
    def _atomic_write(target: Path, content: bytes):
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _default_validation_commands(self, paths: List[Path]) -> List[List[str]]:
        relatives = [str(path.relative_to(self.root)) for path in paths]
        scope = list(dict.fromkeys([*relatives, *self._related_tests(relatives)]))
        return self._validation_commands_for_relative_paths(scope)

    def _related_tests(self, relative_paths: List[str]) -> List[str]:
        candidates = []
        all_files = self.analyze_repository().files
        for relative in relative_paths:
            path = Path(relative)
            if path.suffix.casefold() != ".py" or path.name.startswith("test_"):
                continue
            names = {f"test_{path.stem}.py", f"{path.stem}_test.py"}
            candidates.extend(name for name in all_files if Path(name).name in names)
        return sorted(set(candidates))

    def _validation_commands_for_relative_paths(self, relative_paths: List[str]) -> List[List[str]]:
        commands: List[List[str]] = []
        python_files = [str(self._resolve(path)) for path in relative_paths if Path(path).suffix.casefold() == ".py"]
        if python_files:
            commands.append([sys.executable, "-m", "py_compile", *python_files])
            tests = [path for path in relative_paths if Path(path).name.startswith("test_")]
            if tests:
                commands.append([sys.executable, "-m", "pytest", "-q", *tests])
        javascript_files = [str(self._resolve(path)) for path in relative_paths
                            if Path(path).suffix.casefold() in {".js", ".mjs", ".cjs"}]
        if javascript_files and shutil.which("node"):
            commands.extend([["node", "--check", path] for path in javascript_files])
        commands.extend(self._manifest_validation_commands(relative_paths))
        return commands

    def _manifest_validation_commands(self, relative_paths: List[str]) -> List[List[str]]:
        commands: List[List[str]] = []
        pyproject = self.root / "pyproject.toml"
        pyproject_text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.exists() else ""
        python_targets = [str(self._resolve(path)) for path in relative_paths if Path(path).suffix == ".py"]
        if python_targets and "[tool.ruff" in pyproject_text:
            try:
                import ruff  # noqa: F401
                commands.append([sys.executable, "-m", "ruff", "check", *python_targets])
                if "[tool.ruff.format" in pyproject_text:
                    commands.append([sys.executable, "-m", "ruff", "format", "--check", *python_targets])
            except ImportError:
                pass
        if python_targets and ((self.root / "mypy.ini").exists() or "[tool.mypy]" in pyproject_text):
            try:
                import mypy  # noqa: F401
                commands.append([sys.executable, "-m", "mypy", *python_targets])
            except ImportError:
                pass

        package_json = self.root / "package.json"
        npm = shutil.which("npm")
        if package_json.exists() and npm and any(Path(path).suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx"} for path in relative_paths):
            try:
                scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
            except (json.JSONDecodeError, OSError):
                scripts = {}
            for script in ("format:check", "lint", "typecheck", "test", "build"):
                if script in scripts:
                    commands.append([npm, "run", "--if-present", script])
        if (self.root / "Cargo.toml").exists() and shutil.which("cargo"):
            commands.extend([["cargo", "check"], ["cargo", "test", "--no-fail-fast"]])
        if (self.root / "go.mod").exists() and shutil.which("go"):
            commands.append(["go", "test", "./..."])
        return commands

    def _run_validations(self, commands: List[List[str]]) -> str:
        outputs = []
        for command in commands:
            if not command or any(not isinstance(item, str) for item in command):
                raise ValueError("검증 명령은 문자열 인자 배열이어야 합니다.")
            completed = subprocess.run(
                command, cwd=self.root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120, shell=False,
            )
            output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
            outputs.append(json.dumps({
                "command": command, "returncode": completed.returncode, "output": output,
            }, ensure_ascii=False))
            if completed.returncode != 0:
                raise RuntimeError(
                    f"검증 실패(returncode={completed.returncode}): {' '.join(command)}\n{output}"
                )
        return "\n".join(outputs)

    def _git(self, arguments: List[str]) -> str:
        if not (self.root / ".git").exists():
            return ""
        completed = subprocess.run(
            ["git", *arguments], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, shell=False,
        )
        return completed.stdout if completed.returncode == 0 else ""
