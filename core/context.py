from typing import Optional, Dict, Any
import inspect
from core.memory import get_memory
from core.rag import get_rag
from core.scratchpad import Scratchpad, get_scratchpad
import os
import platform
import re
import threading
from core.assistant_settings import get_assistant_settings
from core.context_lifecycle import ContextLifecycleManager


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

    def __init__(
        self,
        *,
        memory=None,
        rag=None,
        scratchpad: Optional[Scratchpad] = None,
    ):
        """Create a context assembler.

        Dependencies are injectable so a turn-owned executor can keep its
        working memory isolated from other UI, voice, and workspace turns.
        The singleton defaults remain available for legacy callers.
        """
        self.memory = memory or get_memory()
        self.rag = rag or get_rag()
        self.scratchpad = scratchpad or get_scratchpad()
        self.lifecycle = ContextLifecycleManager()
        self._retrieval_local = threading.local()

    @staticmethod
    def _accepts_keyword(callable_obj, keyword: str) -> bool:
        """Check adapter capability without catching errors raised inside it."""
        try:
            parameters = inspect.signature(callable_obj).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == keyword
            for parameter in parameters
        )

    def get_rag_context(self, user_query: str, top_k: int = 3, *,
                        namespace: Optional[str] = None) -> str:
        """Retrieve grounded context and remember exactly what entered this prompt."""
        self._retrieval_local.trace_id = ""
        self._retrieval_local.chunk_ids = []
        if not user_query:
            return ""
        search = self.rag.search_with_confidence
        if namespace is not None and self._accepts_keyword(search, "namespace"):
            retrieval = search(
                user_query, top_k=top_k, namespace=str(namespace or "global"),
            )
        else:
            # Compatibility for third-party RAG adapters that predate explicit
            # request namespaces. The built-in manager always takes the safe path.
            retrieval = search(user_query, top_k=top_k)
        rag_docs = retrieval["results"]
        if not retrieval["answerable"]:
            return (
                "[RAG 검색 상태]\n관련 후보는 있으나 신뢰도가 낮습니다. "
                "이 자료만으로 사실을 단정하지 말고 사용자에게 확인하거나 실시간 검색을 사용하세요."
                if rag_docs else ""
            )
        selected = rag_docs[:top_k]
        chunk_ids = [
            str(doc.get("chunk_id") or doc.get("citation", {}).get("chunk_id") or index + 1)
            for index, doc in enumerate(selected)
        ]
        self._retrieval_local.trace_id = self.rag.get_last_retrieval_trace()
        self._retrieval_local.chunk_ids = chunk_ids
        rag_str = "\n".join(
            f"근거 [{chunk_ids[index]}] 출처={doc.get('source', '')} "
            f"섹션={doc.get('section', '')} 줄={doc.get('start_line', 0)}-{doc.get('end_line', 0)}\n"
            f"{doc['content']}"
            for index, doc in enumerate(selected)
        )
        return (
            "[관련 문서와 인용 근거]\n"
            "문서 기반 주장을 답변에 사용할 때 해당 [근거 ID]를 함께 표시하세요.\n"
            + rag_str
        )

    def mark_response_usage(self, response: str) -> None:
        """Separate retrieved/included evidence from evidence actually cited."""
        trace_id = str(getattr(self._retrieval_local, "trace_id", "") or "")
        included = [str(item) for item in getattr(self._retrieval_local, "chunk_ids", [])]
        if not trace_id or not included:
            return
        cited = set(re.findall(r"\[근거\s+([^\]]+)\]", str(response or "")))
        used = [item for item in included if item in cited]
        tracker = getattr(self.rag, "usage_tracker", None)
        if tracker is not None:
            tracker.mark_used(trace_id, used)
        try:
            from core.quality_metrics import get_quality_metric_store
            get_quality_metric_store().record(
                "rag_used", 1.0 if used else 0.0, success=bool(used),
                context={"trace_id": trace_id, "included": included, "used": used},
            )
        except Exception:
            pass

    def get_full_context(
        self,
        user_query: str = "",
        session_id: Optional[str] = None,
        *,
        include_conversation: bool = True,
        include_scratchpad: bool = True,
        rag_namespace: Optional[str] = None,
    ) -> str:
        """
        모든 소스의 컨텍스트를 통합합니다.

        Args:
            user_query: 사용자 쿼리 (RAG 검색용)
            session_id: 대화 세션 ID
            include_conversation: 외부 파이프라인이 이미 대화 기억을 넣은 경우 False
            include_scratchpad: 외부 파이프라인이 이미 작업 상태를 넣은 경우 False
            rag_namespace: 이번 요청이 검색할 작업공간 네임스페이스

        Returns:
            통합된 컨텍스트 문자열
        """
        context_parts = []

        # 1. OS 상태
        os_status = self._get_os_status()
        if os_status:
            context_parts.append(f"[OS 상태]\n{os_status}")

        # 2. 대화 Memory (최근 10개)
        if include_conversation and session_id:
            conversation = self.memory.load_session(session_id)
            if conversation:
                recent_messages = self.lifecycle.compact_messages(conversation)
                assistant_name = get_assistant_settings().assistant_name
                conv_str = "\n".join([
                    f"{'사용자' if msg['role'] == 'user' else assistant_name}: {msg['content']}"
                    for msg in recent_messages
                ])
                context_parts.append(f"\n[최근 대화]\n{conv_str}")

        # 3. RAG 문서 (사용자 쿼리와 관련된 것)
        if user_query:
            rag_context = self.get_rag_context(
                user_query, namespace=rag_namespace,
            )
            if rag_context:
                context_parts.append("\n" + rag_context)

        # 4. Scratchpad (현재 작업 상태)
        if include_scratchpad:
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
