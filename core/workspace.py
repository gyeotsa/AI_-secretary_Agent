"""Workspace persistence, project metadata, and safe path resolution."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_EXCLUDED_DIRS = (
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv",
    "__pycache__", "node_modules", "dist", "build", ".next", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage",
)


@dataclass
class WorkspaceInfo:
    path: str = ""
    name: str = ""
    alias: str = ""
    file_count: int = 0
    last_updated: float = 0.0
    namespace: str = "global"
    settings: Dict[str, Any] = field(default_factory=dict)


class WorkspaceManager:
    """Owns the selected workspace and persists a reusable project catalog."""

    def __init__(self, state_path: Optional[str] = None):
        self.state_path = Path(state_path or "data/workspaces.json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._workspace: Optional[Path] = None
        self._info = WorkspaceInfo()
        self._state: Dict[str, Any] = {"version": 1, "last_workspace": "", "workspaces": {}}
        self._load_state()
        self.restore_last_workspace()

    @staticmethod
    def namespace_for(path: str | Path) -> str:
        resolved = os.path.normcase(str(Path(path).resolve()))
        return f"workspace-{hashlib.sha256(resolved.encode('utf-8')).hexdigest()[:16]}"

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("workspaces", {}), dict):
                self._state.update(loaded)
        except (OSError, ValueError, TypeError) as exc:
            print(f"[Workspace] 설정 파일을 읽지 못했습니다: {exc}")

    def _save_state(self) -> None:
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temp_path, self.state_path)

    def set_workspace(self, path: str, alias: Optional[str] = None) -> bool:
        try:
            workspace = Path(path).expanduser().resolve()
            if not workspace.is_dir():
                return False
            key = os.path.normcase(str(workspace))
            record = dict(self._state["workspaces"].get(key, {}))
            if alias is not None:
                record["alias"] = alias.strip()
            record.setdefault("alias", workspace.name)
            record.setdefault("settings", {})
            record["path"] = str(workspace)
            record["last_opened_at"] = time.time()
            self._state["workspaces"][key] = record
            self._state["last_workspace"] = key
            self._workspace = workspace
            self._info = WorkspaceInfo(
                path=str(workspace), name=workspace.name,
                alias=record["alias"], namespace=self.namespace_for(workspace),
                settings=dict(record["settings"]),
            )
            self._update_info()
            self._save_state()
            return True
        except (OSError, ValueError, TypeError) as exc:
            print(f"[Workspace] 설정 오류: {exc}")
            return False

    def restore_last_workspace(self) -> bool:
        key = self._state.get("last_workspace", "")
        record = self._state.get("workspaces", {}).get(key, {})
        path = record.get("path", key)
        if not path or not Path(path).is_dir():
            return False
        return self.set_workspace(path)

    def list_workspaces(self) -> List[WorkspaceInfo]:
        result: List[WorkspaceInfo] = []
        for record in sorted(
            self._state["workspaces"].values(),
            key=lambda item: item.get("last_opened_at", 0), reverse=True,
        ):
            path = record.get("path", "")
            if not path or not Path(path).is_dir():
                continue
            result.append(WorkspaceInfo(
                path=path, name=Path(path).name, alias=record.get("alias") or Path(path).name,
                last_updated=float(record.get("last_opened_at", 0)),
                namespace=self.namespace_for(path), settings=dict(record.get("settings", {})),
            ))
        return result

    def set_alias(self, alias: str, path: Optional[str] = None) -> bool:
        target = Path(path).resolve() if path else self._workspace
        if target is None:
            return False
        key = os.path.normcase(str(target))
        record = self._state["workspaces"].get(key)
        if record is None:
            return False
        record["alias"] = alias.strip() or target.name
        if self._workspace == target:
            self._info.alias = record["alias"]
        self._save_state()
        return True

    def update_settings(self, updates: Dict[str, Any], path: Optional[str] = None) -> bool:
        target = Path(path).resolve() if path else self._workspace
        if target is None:
            return False
        key = os.path.normcase(str(target))
        record = self._state["workspaces"].get(key)
        if record is None:
            return False
        settings = record.setdefault("settings", {})
        settings.update(updates)
        if self._workspace == target:
            self._info.settings = dict(settings)
        self._save_state()
        return True

    def resolve(self, relative_path: str) -> Path:
        if self._workspace is None:
            raise RuntimeError("Workspace가 설정되지 않았습니다.")
        candidate = (self._workspace / relative_path).resolve()
        if not candidate.is_relative_to(self._workspace):
            raise PermissionError(f"Workspace 외부 접근을 차단했습니다: {relative_path}")
        return candidate

    def is_set(self) -> bool:
        return self._workspace is not None

    @property
    def current_workspace(self) -> Optional[str]:
        return self.get_workspace_path()

    def get_workspace_path(self) -> Optional[str]:
        return str(self._workspace) if self._workspace else None

    def get_namespace(self) -> str:
        return self._info.namespace if self._workspace else "global"

    def get_info(self) -> WorkspaceInfo:
        self._update_info()
        return self._info

    def _update_info(self) -> None:
        if self._workspace is None:
            return
        count = 0
        try:
            for root, dirs, files in os.walk(self._workspace):
                dirs[:] = [d for d in dirs if d not in DEFAULT_EXCLUDED_DIRS]
                count += len(files)
            self._info.file_count = count
            self._info.last_updated = self._workspace.stat().st_mtime
        except OSError as exc:
            print(f"[Workspace] 정보 갱신 오류: {exc}")

    def get_git_status(self) -> Dict[str, Any]:
        if self._workspace is None:
            return {"is_repository": False, "branch": "", "dirty": False}
        try:
            branch = subprocess.run(
                ["git", "branch", "--show-current"], cwd=self._workspace,
                capture_output=True, text=True, timeout=3, check=False,
            )
            if branch.returncode != 0:
                return {"is_repository": False, "branch": "", "dirty": False}
            status = subprocess.run(
                ["git", "status", "--porcelain"], cwd=self._workspace,
                capture_output=True, text=True, timeout=3, check=False,
            )
            return {
                "is_repository": True,
                "branch": branch.stdout.strip() or "detached",
                "dirty": bool(status.stdout.strip()),
            }
        except (OSError, subprocess.SubprocessError):
            return {"is_repository": False, "branch": "", "dirty": False}

    def get_file_tree(self, max_depth: int = 3) -> Dict[str, Any]:
        if self._workspace is None:
            return {}
        root = {"name": self._info.name, "type": "folder", "children": []}

        def visit(path: Path, depth: int, parent: Dict[str, Any]) -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            except OSError:
                return
            for entry in entries:
                if entry.name in DEFAULT_EXCLUDED_DIRS:
                    continue
                node: Dict[str, Any] = {"name": entry.name, "type": "folder" if entry.is_dir() else "file"}
                parent["children"].append(node)
                if entry.is_dir():
                    node["children"] = []
                    visit(entry, depth + 1, node)
                else:
                    try:
                        node["size"] = entry.stat().st_size
                    except OSError:
                        node["size"] = 0

        visit(self._workspace, 1, root)
        return root


_workspace_manager: Optional[WorkspaceManager] = None


def get_workspace_manager() -> WorkspaceManager:
    global _workspace_manager
    if _workspace_manager is None:
        _workspace_manager = WorkspaceManager()
    return _workspace_manager
