from typing import Optional, Dict, Any
from core.memory import get_memory
from core.rag import get_rag
from core.scratchpad import get_scratchpad
import os
import platform


class ContextManager:
    """
    여러 소스의 컨텍스트를 하나로 통합하는 Context Manager 클래스
    - 대화 Memory
    - RAG 문서
    - Scratchpad (현재 작업 상태)
    - 최근 Tool 결과
    - OS 상태
    - 현재 디렉토리
    """

    def __init__(self):
        self.memory = get_memory()
        self.rag = get_rag()
        self.scratchpad = get_scratchpad()

    def get_full_context(self, user_query: str = "", session_id: Optional[str] = None) -> str:
        """
        모든 소스의 컨텍스트를 통합합니다.

        Args:
            user_query: 사용자 쿼리 (RAG 검색용)
            session_id: 대화 세션 ID

        Returns:
            통합된 컨텍스트 문자열
        """
        context_parts = []

        # 1. OS 상태
        os_status = self._get_os_status()
        if os_status:
            context_parts.append(f"[OS 상태]\n{os_status}")

        # 2. 대화 Memory (최근 10개)
        if session_id:
            conversation = self.memory.load_session(session_id)
            if conversation:
                recent_messages = conversation[-10:]  # 최근 10개만
                conv_str = "\n".join([
                    f"{'사용자' if msg['role'] == 'user' else '자비스'}: {msg['content']}"
                    for msg in recent_messages
                ])
                context_parts.append(f"\n[최근 대화]\n{conv_str}")

        # 3. RAG 문서 (사용자 쿼리와 관련된 것)
        if user_query:
            rag_docs = self.rag.search_docs(user_query)
            if rag_docs:
                rag_str = "\n".join([
                    (
                        f"근거 [{doc.get('chunk_id') or doc.get('citation', {}).get('chunk_id', i + 1)}] "
                        f"출처={doc.get('source', '')} 섹션={doc.get('section', '')} "
                        f"줄={doc.get('start_line', 0)}-{doc.get('end_line', 0)}\n{doc['content']}"
                    )
                    for i, doc in enumerate(rag_docs[:3])  # 최대 3개
                ])
                context_parts.append(
                    "\n[관련 문서와 인용 근거]\n"
                    "문서 기반 주장을 답변에 사용할 때 해당 [근거 ID]를 함께 표시하세요.\n"
                    + rag_str
                )

        # 4. Scratchpad (현재 작업 상태)
        scratchpad_context = self.scratchpad.get_context()
        if scratchpad_context:
            context_parts.append(f"\n[현재 작업 상태]\n{scratchpad_context}")

        # 통합
        full_context = "\n".join(context_parts)
        return full_context

    def _get_os_status(self) -> str:
        """OS 상태를 가져옵니다."""
        try:
            os_name = platform.system()
            cwd = os.getcwd()
            return f"OS: {os_name}\n현재 디렉토리: {cwd}"
        except Exception as e:
            return f"OS 상태 가져오기 오류: {e}"


# Singleton instance
_context_manager = None


def get_context_manager() -> ContextManager:
    """Context Manager 싱글톤 인스턴스 반환"""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager
