"""Shared memory and sequential role orchestration for specialist workspaces."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpecialistContext:
    workspace_key: str
    query: str
    memories: tuple[dict, ...]

    def as_prompt(self) -> str:
        if not self.memories:
            return "관련 작업공간 기억 없음"
        lines = []
        for item in self.memories[:6]:
            content = str(item.get("content", item.get("document", ""))).strip()
            if content:
                lines.append(f"- {content[:700]}")
        return "\n".join(lines) or "관련 작업공간 기억 없음"


class SpecialistTeamRuntime:
    """Makes workspace memory available without keeping every model resident.

    Individual specialists are called sequentially by the owning runtime. This
    service supplies the same scoped evidence and records only approved or
    successful outcomes, avoiding failed-preview pollution in RAG.
    """

    def __init__(self, rag=None, *, namespace_provider=None):
        self.rag = rag
        self.namespace_provider = namespace_provider or (lambda: "global")
        self._rag_lock = threading.RLock()

    ROLE_PIPELINES = {
        "mockup": (
            "기억 검색",
            "Vision 관찰",
            "레이아웃 설계",
            "제약 검증",
            "비파괴 렌더링",
        ),
    }

    def role_pipeline(self, workspace_key: str) -> tuple[str, ...]:
        return self.ROLE_PIPELINES.get(
            workspace_key,
            ("기억 검색", "의도 분석", "전문 작업", "결과 검증"),
        )

    def describe_team(self, workspace_key: str) -> str:
        roles = " → ".join(self.role_pipeline(workspace_key))
        return f"전문가 팀: {roles} · 필요한 모델만 순차 실행/해제"

    def namespace(self, workspace_key: str) -> str:
        base = str(self.namespace_provider() or "global")
        return f"{base}:specialist:{workspace_key}"

    def recall(self, workspace_key: str, query: str, *, top_k: int = 6) -> SpecialistContext:
        if self.rag is None or not str(query).strip():
            return SpecialistContext(workspace_key, query, ())
        # Namespace is mutable process state, so isolate workspace and chat
        # retrieval when their worker threads happen to run concurrently.
        with self._rag_lock:
            original = getattr(self.rag, "namespace", "global")
            try:
                self.rag.set_namespace(self.namespace(workspace_key))
                results = self.rag.search_docs(query, top_k=top_k)
                return SpecialistContext(workspace_key, query, tuple(results or ()))
            finally:
                self.rag.set_namespace(original)

    def remember_success(self, workspace_key: str, *, instruction: str, result: dict,
                         approved: bool = False) -> str:
        """Store compact decisions, never binary images or failed attempts."""
        if self.rag is None or not str(instruction).strip():
            return ""
        summary = {
            "workspace": workspace_key,
            "instruction": " ".join(str(instruction).split())[:1200],
            "approved": bool(approved),
            "renderer": str(result.get("renderer", "")),
            "applied_fields": list(result.get("applied_edit_fields") or ()),
            "profile_id": str(result.get("profile_id", "")),
            "recorded_at": time.time(),
        }
        digest = hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        namespace = self.namespace(workspace_key)
        with self._rag_lock:
            return self.rag.add_text_document(
                json.dumps(summary, ensure_ascii=False), doc_id=f"workspace-event-{digest}",
                namespace=namespace,
                metadata={"source_type": "specialist_workspace", "approved": bool(approved)},
                source_uri=f"specialist://{workspace_key}/{digest}",
            )
