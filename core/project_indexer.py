"""Incremental, ignore-aware project index used by workspace intelligence."""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional

from core.workspace import DEFAULT_EXCLUDED_DIRS, get_workspace_manager


@dataclass
class FileInfo:
    path: str
    name: str
    extension: str
    size: int
    modified_at: float
    is_text: bool = True
    content_preview: str = ""
    symbols: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SymbolInfo:
    name: str
    type: str
    line: int
    end_line: int
    file_path: str
    docstring: Optional[str] = None


class IgnoreRules:
    """Small gitignore-compatible matcher with negation and nested rule files."""

    def __init__(self, root: Path):
        self.root = root
        self.rules: List[tuple[str, bool, bool]] = []
        for ignore_file in root.rglob(".gitignore"):
            if any(part in DEFAULT_EXCLUDED_DIRS for part in ignore_file.relative_to(root).parts[:-1]):
                continue
            base = ignore_file.parent.relative_to(root).as_posix()
            try:
                lines = ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for raw in lines:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                negate = line.startswith("!")
                if negate:
                    line = line[1:]
                directory_only = line.endswith("/")
                line = line.rstrip("/").lstrip("/")
                pattern = f"{base}/{line}" if base not in ("", ".") else line
                self.rules.append((pattern, negate, directory_only))

    def ignored(self, path: Path, is_dir: bool = False) -> bool:
        rel = path.relative_to(self.root).as_posix()
        if any(part in DEFAULT_EXCLUDED_DIRS for part in PurePosixPath(rel).parts):
            return True
        ignored = False
        for pattern, negate, directory_only in self.rules:
            if directory_only and not (is_dir or rel.startswith(pattern.rstrip("/") + "/")):
                continue
            matched = PurePosixPath(rel).match(pattern)
            if not matched and "/" not in pattern:
                matched = any(fnmatch.fnmatch(part, pattern) for part in PurePosixPath(rel).parts)
            if matched or rel.startswith(pattern.rstrip("/") + "/"):
                ignored = not negate
        return ignored


class ProjectIndexer:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "data/project_index.db"
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.project_root: Optional[str] = None
        self._lock = threading.RLock()
        self._ignore: Optional[IgnoreRules] = None
        self._init_db()
        current = get_workspace_manager().current_workspace
        if current:
            self.set_project_root(current)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,
                project_root TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, extension TEXT,
                size INTEGER, modified_at REAL, is_text INTEGER, content_preview TEXT,
                last_indexed_at REAL)""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            if "project_root" not in columns:
                conn.execute("ALTER TABLE files ADD COLUMN project_root TEXT NOT NULL DEFAULT ''")
            conn.execute("""CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER, name TEXT NOT NULL,
                type TEXT NOT NULL, line INTEGER, end_line INTEGER, docstring TEXT,
                FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_root ON files(project_root)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")

    def set_project_root(self, path: str) -> bool:
        root = Path(path).resolve()
        if not root.is_dir():
            return False
        self.project_root = str(root)
        self._ignore = IgnoreRules(root)
        return True

    def _root(self) -> Optional[Path]:
        return Path(self.project_root) if self.project_root else None

    def _iter_files(self) -> Iterable[Path]:
        root = self._root()
        if root is None:
            return
        rules = self._ignore or IgnoreRules(root)
        for current, dirs, files in os.walk(root):
            base = Path(current)
            dirs[:] = [d for d in dirs if not rules.ignored(base / d, True)]
            for name in files:
                candidate = base / name
                if not rules.ignored(candidate, False):
                    yield candidate

    @staticmethod
    def _read_text(path: Path, limit: Optional[int] = None) -> Optional[str]:
        try:
            data = path.read_bytes()
            if b"\0" in data[:4096]:
                return None
            text = data.decode("utf-8")
            return text if limit is None else text[:limit]
        except (OSError, UnicodeDecodeError):
            return None

    @staticmethod
    def _extract_python_symbols(path: Path, content: str) -> List[SymbolInfo]:
        result: List[SymbolInfo] = []
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError):
            return result
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                result.append(SymbolInfo(
                    node.name, kind, node.lineno, getattr(node, "end_lineno", node.lineno),
                    str(path), ast.get_docstring(node),
                ))
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            result.append(SymbolInfo(
                                child.name, "method", child.lineno,
                                getattr(child, "end_lineno", child.lineno), str(path),
                                ast.get_docstring(child),
                            ))
        return result

    def index_file(self, path: str) -> Optional[int]:
        root = self._root()
        candidate = Path(path).resolve()
        if root is None or not candidate.is_file() or not candidate.is_relative_to(root):
            return None
        if (self._ignore or IgnoreRules(root)).ignored(candidate):
            return None
        try:
            stat = candidate.stat()
            content = self._read_text(candidate)
            preview = (content[:500] + "...") if content is not None and len(content) > 500 else (content or "")
            with self._lock, self._connect() as conn:
                existing = conn.execute("SELECT id FROM files WHERE path=?", (str(candidate),)).fetchone()
                if existing:
                    file_id = existing[0]
                    conn.execute("""UPDATE files SET project_root=?, name=?, extension=?, size=?,
                        modified_at=?, is_text=?, content_preview=?, last_indexed_at=? WHERE id=?""",
                        (str(root), candidate.name, candidate.suffix.lower(), stat.st_size, stat.st_mtime,
                         int(content is not None), preview, time.time(), file_id))
                else:
                    cursor = conn.execute("""INSERT INTO files
                        (path, project_root, name, extension, size, modified_at, is_text,
                         content_preview, last_indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(candidate), str(root), candidate.name, candidate.suffix.lower(), stat.st_size,
                         stat.st_mtime, int(content is not None), preview, time.time()))
                    file_id = int(cursor.lastrowid)
                conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
                if candidate.suffix.lower() == ".py" and content is not None:
                    conn.executemany(
                        "INSERT INTO symbols (file_id,name,type,line,end_line,docstring) VALUES (?,?,?,?,?,?)",
                        [(file_id, s.name, s.type, s.line, s.end_line, s.docstring)
                         for s in self._extract_python_symbols(candidate, content)],
                    )
                return file_id
        except OSError:
            return None

    def sync_changes(self) -> Dict[str, int]:
        """Index new/changed files and remove stale or newly ignored rows."""
        root = self._root()
        if root is None:
            return {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}
        self._ignore = IgnoreRules(root)
        current = {str(path): path for path in self._iter_files()}
        with self._lock, self._connect() as conn:
            indexed = {
                row[0]: (row[1], row[2]) for row in conn.execute(
                    "SELECT path,size,modified_at FROM files WHERE project_root=?", (str(root),)
                )
            }
        stats = {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}
        for path_string, path in current.items():
            try:
                stat = path.stat()
            except OSError:
                continue
            old = indexed.get(path_string)
            if old and old[0] == stat.st_size and old[1] == stat.st_mtime:
                stats["unchanged"] += 1
                continue
            if self.index_file(path_string):
                stats["updated" if old else "added"] += 1
        stale = set(indexed) - set(current)
        if stale:
            with self._lock, self._connect() as conn:
                placeholders = ",".join("?" for _ in stale)
                conn.execute(f"DELETE FROM files WHERE path IN ({placeholders})", tuple(stale))
            stats["deleted"] = len(stale)
        return stats

    def index_project(self, exclude_dirs: Optional[List[str]] = None) -> int:
        # exclude_dirs remains accepted for callers; the shared ignore contract is authoritative.
        result = self.sync_changes()
        return result["added"] + result["updated"] + result["unchanged"]

    def detect_project_profile(self) -> Dict[str, Any]:
        root = self._root()
        if root is None:
            return {"languages": [], "frameworks": [], "test_commands": []}
        extensions = Counter(path.suffix.lower() for path in self._iter_files())
        language_map = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
            ".java": "Java", ".kt": "Kotlin", ".rs": "Rust", ".go": "Go", ".cs": "C#",
            ".cpp": "C++", ".c": "C", ".swift": "Swift",
        }
        languages = [language_map[ext] for ext, _ in extensions.most_common() if ext in language_map]
        frameworks: List[str] = []
        commands: List[str] = []
        if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
            text = " ".join(
                self._read_text(path) or "" for path in (root / "pyproject.toml", root / "requirements.txt")
                if path.exists()
            ).lower()
            for token, label in (("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask"), ("pytest", "pytest")):
                if token in text:
                    frameworks.append(label)
            commands.append("python -m pytest")
        package = root / "package.json"
        if package.exists():
            try:
                manifest = json.loads(package.read_text(encoding="utf-8"))
                deps = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
                for token, label in (("react", "React"), ("next", "Next.js"), ("vue", "Vue"), ("svelte", "Svelte")):
                    if token in deps:
                        frameworks.append(label)
                scripts = manifest.get("scripts", {})
                for name in ("test", "lint", "typecheck", "build"):
                    if name in scripts:
                        commands.append(f"npm run {name}")
            except (OSError, ValueError, TypeError):
                pass
        if (root / "Cargo.toml").exists():
            commands.extend(["cargo test", "cargo check"])
        if (root / "go.mod").exists():
            commands.append("go test ./...")
        return {"languages": languages, "frameworks": sorted(set(frameworks)), "test_commands": commands}

    def search_files(self, query: str, search_type: str = "name") -> List[Dict[str, Any]]:
        if not self.project_root or search_type not in {"name", "content", "extension"}:
            return []
        column = {"name": "name", "content": "content_preview", "extension": "extension"}[search_type]
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT path,name,extension,size,modified_at,content_preview FROM files "
                f"WHERE project_root=? AND {column} LIKE ?", (self.project_root, f"%{query}%")
            ).fetchall()
        return [{"path": r[0], "name": r[1], "extension": r[2], "size": r[3],
                 "modified_at": r[4], "content_preview": r[5]} for r in rows]

    def search_symbols(self, query: str, symbol_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.project_root:
            return []
        sql = """SELECT s.name,s.type,s.line,s.end_line,s.docstring,f.path FROM symbols s
                 JOIN files f ON s.file_id=f.id WHERE f.project_root=? AND s.name LIKE ?"""
        args: List[Any] = [self.project_root, f"%{query}%"]
        if symbol_type:
            sql += " AND s.type=?"
            args.append(symbol_type)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [{"name": r[0], "type": r[1], "line": r[2], "end_line": r[3],
                 "docstring": r[4], "file_path": r[5]} for r in rows]

    def get_file_info(self, path: str) -> Optional[FileInfo]:
        with self._connect() as conn:
            row = conn.execute("""SELECT path,name,extension,size,modified_at,is_text,content_preview
                                  FROM files WHERE path=?""", (str(Path(path).resolve()),)).fetchone()
            if not row:
                return None
            info = FileInfo(row[0], row[1], row[2], row[3], row[4], bool(row[5]), row[6])
            symbols = conn.execute("""SELECT name,type,line,end_line,docstring FROM symbols
                                      WHERE file_id=(SELECT id FROM files WHERE path=?)""", (row[0],)).fetchall()
        info.symbols = [{"name": s[0], "type": s[1], "line": s[2], "end_line": s[3], "docstring": s[4]} for s in symbols]
        return info

    def get_file_tree(self, root_path: Optional[str] = None) -> Dict[str, Any]:
        root = Path(root_path or self.project_root or os.getcwd()).resolve()
        result = {"name": root.name, "path": str(root), "type": "directory", "children": []}
        rules = self._ignore if self._root() == root else IgnoreRules(root)
        try:
            entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return result
        for item in entries:
            if rules and rules.ignored(item, item.is_dir()):
                continue
            result["children"].append(
                self.get_file_tree(str(item)) if item.is_dir()
                else {"name": item.name, "path": str(item), "type": "file"}
            )
        return result


_project_indexer: Optional[ProjectIndexer] = None


def get_project_indexer() -> ProjectIndexer:
    global _project_indexer
    if _project_indexer is None:
        _project_indexer = ProjectIndexer()
    return _project_indexer
