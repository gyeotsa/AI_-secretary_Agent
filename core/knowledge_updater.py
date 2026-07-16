"""
Knowledge Updater for Jarvis
- Observer 이벤트 → RAG 자동 인덱싱
- Event Bus 연동
"""

from pathlib import Path
from typing import Optional
from core.runtime.event_bus import get_event_bus, Event
from core.rag import get_rag_manager
from core.user_profile import get_user_profile_manager
from core.runtime.action_journal import get_action_journal

class KnowledgeUpdater:
    def __init__(self):
        self.event_bus = get_event_bus()
        self.rag = get_rag_manager()
        self.profile = get_user_profile_manager()
        self.action_journal = get_action_journal()
        self._subscribe()
        
    def _subscribe(self):
        """이벤트 구독"""
        self.event_bus.subscribe("file_created", self._on_file_created)
        self.event_bus.subscribe("file_modified", self._on_file_modified)
        self.event_bus.subscribe("file_deleted", self._on_file_deleted)
        
    def _on_file_created(self, event: Event):
        """새 파일 생성 이벤트 처리 → RAG 인덱싱"""
        file_path = event.data.get("path")
        if not file_path:
            return
        path = Path(file_path)
        # 지원하는 파일 타입만 처리
        if path.suffix.lower() in [".txt", ".md", ".pdf", ".docx", ".py", ".js", ".html", ".css"]:
            try:
                self.action_journal.record(
                    action_type="knowledge_index",
                    description=f"새 파일 인덱싱: {path.name}",
                    source="knowledge_updater",
                    data={"path": str(path)}
                )
                # RAG에 추가
                if path.suffix.lower() == ".pdf":
                    # PDF는 나중에 지원
                    pass
                else:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                        self.rag.add_text(
                            text=content,
                            metadata={
                                "source": "file",
                                "path": str(path),
                                "name": path.name,
                                "type": path.suffix.lower()
                            }
                        )
                # Action Journal에 성공 기록
                self.action_journal.record(
                    action_type="knowledge_index",
                    description=f"파일 인덱싱 완료: {path.name}",
                    source="knowledge_updater",
                    data={"path": str(path)},
                    success=True
                )
            except Exception as e:
                self.action_journal.record(
                    action_type="knowledge_index",
                    description=f"파일 인덱싱 실패: {path.name}",
                    source="knowledge_updater",
                    data={"path": str(path)},
                    success=False,
                    error=str(e)
                )
                
    def _on_file_modified(self, event: Event):
        """파일 수정 이벤트 처리 → RAG 업데이트"""
        file_path = event.data.get("path")
        if not file_path:
            return
        path = Path(file_path)
        if path.suffix.lower() in [".txt", ".md", ".pdf", ".docx", ".py", ".js", ".html", ".css"]:
            try:
                # 기존 문서 삭제 후 재인덱싱 (나중에 최적화)
                self._on_file_created(event)
            except Exception as e:
                pass
                
    def _on_file_deleted(self, event: Event):
        """파일 삭제 이벤트 처리 → RAG에서 제거"""
        file_path = event.data.get("path")
        if not file_path:
            return
        # RAG에서 제거 (Chroma DB는 나중에 구현)
        pass

# Singleton
_knowledge_updater = None

def get_knowledge_updater() -> KnowledgeUpdater:
    global _knowledge_updater
    if _knowledge_updater is None:
        _knowledge_updater = KnowledgeUpdater()
    return _knowledge_updater
