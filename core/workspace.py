
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
import os


@dataclass
class WorkspaceInfo:
    path: str = ""
    name: str = ""
    file_count: int = 0
    last_updated: float = 0.0


class WorkspaceManager:
    def __init__(self):
        self._workspace: Optional[Path] = None
        self._info: WorkspaceInfo = WorkspaceInfo()
        
    def set_workspace(self, path: str) -> bool:
        """
        작업 공간을 설정합니다.
        
        Args:
            path: 작업 공간으로 사용할 폴더 경로
            
        Returns:
            성공 여부
        """
        try:
            p = Path(path).resolve()
            if not p.exists() or not p.is_dir():
                return False
            
            self._workspace = p
            self._info.path = str(p)
            self._info.name = p.name
            self._update_info()
            return True
        except Exception as e:
            print(f"[Workspace] 설정 오류: {e}")
            return False
        
    def resolve(self, relative_path: str) -> Path:
        """
        상대 경로를 절대 경로로 변환합니다. (Workspace 내부만 허용!)
        
        Args:
            relative_path: Workspace 기준 상대 경로
            
        Returns:
            절대 경로
            
        Raises:
            Exception: Workspace 외부 경로 접근 시
        """
        if not self._workspace:
            raise Exception("Workspace가 설정되지 않았습니다!")
        
        # 상대 경로 정규화
        real_path = (self._workspace / relative_path).resolve()
        
        # Workspace 내부인지 확인
        if not real_path.is_relative_to(self._workspace):
            raise Exception(f"접근 거부: {relative_path} (Workspace 외부)")
        
        return real_path
        
    def is_set(self) -> bool:
        """Workspace가 설정되어 있는지 확인"""
        return self._workspace is not None

    @property
    def current_workspace(self) -> Optional[str]:
        """ProjectIndexer 등 기존 소비자가 사용하는 현재 Workspace 절대 경로."""
        return self.get_workspace_path()
        
    def get_workspace_path(self) -> Optional[str]:
        """설정된 Workspace 경로 반환"""
        return str(self._workspace) if self._workspace else None
        
    def get_info(self) -> WorkspaceInfo:
        """Workspace 정보 반환"""
        self._update_info()
        return self._info
        
    def _update_info(self):
        """Workspace 정보 업데이트 (내부용)"""
        if not self._workspace:
            return
            
        try:
            # 파일 개수 세기
            file_count = 0
            for item in self._workspace.rglob("*"):
                if item.is_file():
                    file_count += 1
                    
            self._info.file_count = file_count
            self._info.last_updated = self._workspace.stat().st_mtime
        except Exception as e:
            print(f"[Workspace] 정보 업데이트 오류: {e}")
            
    def get_file_tree(self, max_depth: int = 3) -> Dict[str, Any]:
        """
        Workspace의 파일 트리 구조를 반환합니다.
        
        Args:
            max_depth: 최대 탐색 깊이
            
        Returns:
            파일 트리 딕셔너리
        """
        if not self._workspace:
            return {}
            
        tree = {
            "name": self._info.name,
            "type": "folder",
            "children": []
        }
        
        def _build_tree(current_path: Path, current_depth: int, parent: Dict[str, Any]):
            if current_depth > max_depth:
                return
                
            try:
                items = sorted(current_path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
                
                for item in items:
                    if item.name.startswith('.'):  # 숨김 파일/폴더 건너뛰기
                        continue
                        
                    node = {
                        "name": item.name,
                        "type": "folder" if item.is_dir() else "file"
                    }
                    
                    if item.is_file():
                        node["size"] = item.stat().st_size
                        
                    parent["children"].append(node)
                    
                    if item.is_dir():
                        node["children"] = []
                        _build_tree(item, current_depth + 1, node)
            except Exception as e:
                print(f"[Workspace] 트리 빌드 오류 ({current_path}): {e}")
                
        _build_tree(self._workspace, 1, tree)
        return tree


# Singleton 인스턴스
_workspace_manager = None


def get_workspace_manager() -> WorkspaceManager:
    """WorkspaceManager 싱글톤 인스턴스 반환"""
    global _workspace_manager
    if _workspace_manager is None:
        _workspace_manager = WorkspaceManager()
    return _workspace_manager
