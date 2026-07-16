"""
Project Indexer for Jarvis - Claude Code 수준의 프로젝트 인덱싱
- 프로젝트 파일 트리 구조 관리
- 파일 내용 인덱싱 (텍스트 추출, Python AST 분석)
- 파일/심볼 검색 기능
- Workspace와 연동
"""

import os
import ast
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import threading
from core.workspace import get_workspace_manager


@dataclass
class FileInfo:
    """파일 정보 데이터 클래스"""
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
    """코드 심볼 정보 (함수, 클래스, 변수)"""
    name: str
    type: str  # "function", "class", "variable", "method"
    line: int
    end_line: int
    file_path: str
    docstring: Optional[str] = None


class ProjectIndexer:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or "data/project_index.db"
        Path(self.db_path).parent.mkdir(exist_ok=True)
        self.project_root: Optional[str] = None
        self._lock = threading.Lock()
        self._init_db()
        # Workspace Manager와 연동
        self.workspace_manager = get_workspace_manager()
        if self.workspace_manager.current_workspace:
            self.set_project_root(self.workspace_manager.current_workspace)

    def _init_db(self):
        """데이터베이스 초기화"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                extension TEXT,
                size INTEGER,
                modified_at REAL,
                is_text INTEGER,
                content_preview TEXT,
                last_indexed_at REAL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                line INTEGER,
                end_line INTEGER,
                docstring TEXT,
                FOREIGN KEY(file_id) REFERENCES files(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_files_path ON files(path)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")
        conn.commit()
        conn.close()

    def set_project_root(self, path: str) -> bool:
        """프로젝트 루트 경로 설정"""
        if os.path.isdir(path):
            self.project_root = os.path.abspath(path)
            return True
        return False

    def _is_text_file(self, path: str) -> bool:
        """텍스트 파일인지 확인"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                f.read(1024)
            return True
        except UnicodeDecodeError:
            return False
        except Exception:
            return False

    def _extract_text_preview(self, path: str, max_length: int = 500) -> str:
        """파일 내용 미리보기 추출"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read(max_length + 100)
                if len(content) > max_length:
                    content = content[:max_length] + "..."
                return content
        except Exception:
            return ""

    def _extract_python_symbols(self, path: str) -> List[SymbolInfo]:
        """Python 파일에서 심볼 추출 (AST 분석)"""
        symbols = []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    docstring = ast.get_docstring(node)
                    symbols.append(SymbolInfo(
                        name=node.name,
                        type="function",
                        line=node.lineno,
                        end_line=getattr(node, 'end_lineno', node.lineno),
                        file_path=path,
                        docstring=docstring
                    ))
                elif isinstance(node, ast.ClassDef):
                    docstring = ast.get_docstring(node)
                    symbols.append(SymbolInfo(
                        name=node.name,
                        type="class",
                        line=node.lineno,
                        end_line=getattr(node, 'end_lineno', node.lineno),
                        file_path=path,
                        docstring=docstring
                    ))
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            method_docstring = ast.get_docstring(item)
                            symbols.append(SymbolInfo(
                                name=item.name,
                                type="method",
                                line=item.lineno,
                                end_line=getattr(item, 'end_lineno', item.lineno),
                                file_path=path,
                                docstring=method_docstring
                            ))
        except Exception:
            pass
        return symbols

    def index_file(self, path: str) -> Optional[int]:
        """단일 파일 인덱싱"""
        if not self.project_root or not os.path.isfile(path):
            return None
        if not path.startswith(self.project_root):
            return None

        try:
            stat = os.stat(path)
            name = os.path.basename(path)
            ext = os.path.splitext(name)[1].lower()
            is_text = self._is_text_file(path)
            content_preview = self._extract_text_preview(path) if is_text else ""

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO files 
                (path, name, extension, size, modified_at, is_text, content_preview, last_indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                path, name, ext, stat.st_size, stat.st_mtime,
                1 if is_text else 0, content_preview, datetime.now().timestamp()
            ))
            file_id = cursor.lastrowid or cursor.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()[0]
            cursor.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))

            if ext == ".py":
                symbols = self._extract_python_symbols(path)
                for sym in symbols:
                    cursor.execute("""
                        INSERT INTO symbols (file_id, name, type, line, end_line, docstring)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        file_id, sym.name, sym.type, sym.line, sym.end_line, sym.docstring
                    ))
            conn.commit()
            conn.close()
            return file_id
        except Exception:
            return None

    def index_project(self, exclude_dirs: Optional[List[str]] = None) -> int:
        """프로젝트 전체 인덱싱"""
        if not self.project_root:
            return 0
        exclude = exclude_dirs or ["__pycache__", ".git", "venv", "node_modules", ".venv"]
        count = 0
        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [d for d in dirs if d not in exclude]
            for file in files:
                file_path = os.path.join(root, file)
                if self.index_file(file_path):
                    count += 1
        return count

    def get_file_tree(self, root_path: Optional[str] = None) -> Dict[str, Any]:
        """파일 트리 구조 반환"""
        base_path = root_path or self.project_root or os.getcwd()
        tree = {"name": os.path.basename(base_path), "path": base_path, "type": "directory", "children": []}
        try:
            for item in os.listdir(base_path):
                item_path = os.path.join(base_path, item)
                if os.path.isdir(item_path):
                    if item not in ["__pycache__", ".git", "venv", "node_modules", ".venv"]:
                        tree["children"].append(self.get_file_tree(item_path))
                else:
                    tree["children"].append({"name": item, "path": item_path, "type": "file"})
        except Exception:
            pass
        tree["children"].sort(key=lambda x: (x["type"] != "directory", x["name"]))
        return tree

    def search_files(self, query: str, search_type: str = "name") -> List[Dict[str, Any]]:
        """
        파일 검색
        search_type: "name", "content", "extension"
        """
        if not self.project_root:
            return []
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        results = []
        try:
            if search_type == "name":
                cursor.execute("""
                    SELECT path, name, extension, size, modified_at FROM files
                    WHERE path LIKE ? AND name LIKE ?
                """, (f"{self.project_root}%", f"%{query}%"))
            elif search_type == "extension":
                cursor.execute("""
                    SELECT path, name, extension, size, modified_at FROM files
                    WHERE path LIKE ? AND extension LIKE ?
                """, (f"{self.project_root}%", f"%{query}%"))
            elif search_type == "content":
                cursor.execute("""
                    SELECT path, name, extension, size, modified_at, content_preview FROM files
                    WHERE path LIKE ? AND content_preview LIKE ?
                """, (f"{self.project_root}%", f"%{query}%"))
            else:
                return []
            rows = cursor.fetchall()
            for row in rows:
                results.append({
                    "path": row[0],
                    "name": row[1],
                    "extension": row[2],
                    "size": row[3],
                    "modified_at": datetime.fromtimestamp(row[4]).isoformat() if row[4] else "",
                    "content_preview": row[5] if len(row) > 5 else ""
                })
        finally:
            conn.close()
        return results

    def search_symbols(self, query: str, symbol_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """심볼 검색 (함수, 클래스, 메서드)"""
        if not self.project_root:
            return []
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        results = []
        try:
            if symbol_type:
                cursor.execute("""
                    SELECT s.name, s.type, s.line, s.end_line, s.docstring, f.path 
                    FROM symbols s JOIN files f ON s.file_id = f.id
                    WHERE f.path LIKE ? AND s.name LIKE ? AND s.type = ?
                """, (f"{self.project_root}%", f"%{query}%", symbol_type))
            else:
                cursor.execute("""
                    SELECT s.name, s.type, s.line, s.end_line, s.docstring, f.path 
                    FROM symbols s JOIN files f ON s.file_id = f.id
                    WHERE f.path LIKE ? AND s.name LIKE ?
                """, (f"{self.project_root}%", f"%{query}%"))
            rows = cursor.fetchall()
            for row in rows:
                results.append({
                    "name": row[0],
                    "type": row[1],
                    "line": row[2],
                    "end_line": row[3],
                    "docstring": row[4],
                    "file_path": row[5]
                })
        finally:
            conn.close()
        return results

    def get_file_info(self, path: str) -> Optional[FileInfo]:
        """파일 정보 가져오기"""
        if not self.project_root:
            return None
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT path, name, extension, size, modified_at, is_text, content_preview
                FROM files WHERE path=?
            """, (path,))
            row = cursor.fetchone()
            if row:
                file_info = FileInfo(
                    path=row[0],
                    name=row[1],
                    extension=row[2],
                    size=row[3],
                    modified_at=row[4],
                    is_text=bool(row[5]),
                    content_preview=row[6]
                )
                cursor.execute("""
                    SELECT name, type, line, end_line, docstring FROM symbols WHERE file_id IN 
                    (SELECT id FROM files WHERE path=?)
                """, (path,))
                symbols = cursor.fetchall()
                for sym in symbols:
                    file_info.symbols.append({
                        "name": sym[0],
                        "type": sym[1],
                        "line": sym[2],
                        "end_line": sym[3],
                        "docstring": sym[4]
                    })
                return file_info
        finally:
            conn.close()
        return None


# Singleton
_project_indexer: Optional[ProjectIndexer] = None

def get_project_indexer() -> ProjectIndexer:
    global _project_indexer
    if _project_indexer is None:
        _project_indexer = ProjectIndexer()
    return _project_indexer
