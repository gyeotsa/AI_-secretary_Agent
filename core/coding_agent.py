"""Repository-scoped, rollback-safe coding transaction primitives."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import List, Optional
import difflib
import json
import os
import subprocess
import sys
import tempfile


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


@dataclass
class CodingTransactionResult:
    status: str
    changed_files: List[str] = field(default_factory=list)
    diff: str = ""
    validation_output: str = ""
    error: str = ""
    rolled_back: bool = False

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

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"유효한 저장소 폴더가 아닙니다: {self.root}")

    def _resolve(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError(f"Workspace 외부 파일은 변경할 수 없습니다: {relative_path}")
        return target

    def analyze_repository(self, max_files: int = 5000) -> RepositorySnapshot:
        files = []
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if any(part.casefold() in self.IGNORED_PARTS for part in relative.parts):
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

    def apply_transaction(
        self,
        edits: List[FileEdit],
        validation_commands: Optional[List[List[str]]] = None,
    ) -> CodingTransactionResult:
        if not edits:
            return CodingTransactionResult("failed", error="적용할 최소 변경이 없습니다.")
        originals: dict[Path, bytes] = {}
        updates: dict[Path, bytes] = {}
        diffs = []
        try:
            for edit in edits:
                target = self._resolve(edit.path)
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

            for target, updated in updates.items():
                self._atomic_write(target, updated)

            commands = validation_commands or self._default_validation_commands(list(updates))
            validation_output = self._run_validations(commands)
            return CodingTransactionResult(
                "completed", [str(path.relative_to(self.root)) for path in updates],
                "".join(diffs), validation_output,
            )
        except Exception as exc:
            rollback_error = ""
            for target, raw in originals.items():
                try:
                    self._atomic_write(target, raw)
                except Exception as rollback_exc:
                    rollback_error = f"; 롤백 오류: {rollback_exc}"
            return CodingTransactionResult(
                "failed", error=f"{exc}{rollback_error}", rolled_back=bool(originals),
            )

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
        python_files = [str(path) for path in paths if path.suffix.casefold() == ".py"]
        return [[sys.executable, "-m", "py_compile", *python_files]] if python_files else []

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
