"""Shared memory and resource-aware role orchestration for specialist workspaces."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


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


@dataclass(frozen=True)
class SpecialistRole:
    key: str
    label: str
    model_role: str
    input_artifacts: tuple[str, ...] = ()
    output_artifact: str = ""
    optional: bool = False


@dataclass
class TeamRun:
    workspace_key: str
    instruction: str
    artifacts: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, role: SpecialistRole, status: str, started_at: float, detail: str = "") -> None:
        self.events.append({
            "role": role.key, "label": role.label, "model_role": role.model_role,
            "status": status, "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "detail": str(detail)[:500],
        })


class SpecialistTeamRuntime:
    """Runs workspace specialists sequentially and releases heavy models between roles.

    A role is more than a display label: it declares required artifacts, a model role,
    and one output artifact. The owning workspace supplies role handlers, allowing the
    same orchestration contract to be shared by mockup, document, coding and research
    workspaces without loading all models at once.
    """

    TEAM_BLUEPRINTS = {
        "mockup": (
            SpecialistRole("memory_curator", "기억 큐레이터", "reasoning", (), "memory_context"),
            SpecialistRole("style_analyst", "스타일·Vision 분석가", "style_vision", ("references",), "style_evidence"),
            SpecialistRole("subject_specialist", "인물 분할·크롭 전문가", "subject_analysis", ("production_assets",), "subject_evidence"),
            SpecialistRole("design_director", "디자인 디렉터", "design_planning", ("style_evidence", "subject_evidence"), "layer_graph"),
            SpecialistRole("renderer", "레이어 렌더링 전문가", "rendering", ("layer_graph",), "rendered_image"),
            SpecialistRole("visual_critic", "시각 품질 검수자", "visual_critic", ("rendered_image",), "quality_verdict"),
            SpecialistRole("corrector", "제약 기반 교정자", "design_planning", ("quality_verdict", "layer_graph"), "corrected_graph", True),
        ),
        "document": (
            SpecialistRole("memory_curator", "기억 큐레이터", "reasoning", (), "memory_context"),
            SpecialistRole("requirements", "요구사항 분석가", "reasoning", (), "requirements"),
            SpecialistRole("author", "문서 작성 전문가", "document", ("requirements",), "document"),
            SpecialistRole("reviewer", "문서 품질 검수자", "reasoning", ("document",), "quality_verdict"),
        ),
        "coding": (
            SpecialistRole("memory_curator", "기억 큐레이터", "reasoning", (), "memory_context"),
            SpecialistRole("architect", "소프트웨어 아키텍트", "reasoning", (), "plan"),
            SpecialistRole("implementer", "시니어 개발자", "code", ("plan",), "changes"),
            SpecialistRole("reviewer", "코드 리뷰어", "reasoning", ("changes",), "quality_verdict"),
        ),
    }

    def __init__(self, rag=None, *, namespace_provider=None, event_pipeline=None):
        self.rag = rag
        self.namespace_provider = namespace_provider or (lambda: "global")
        self.event_pipeline = event_pipeline
        self._rag_lock = threading.RLock()
        self._active_run = threading.local()

    def roles(self, workspace_key: str) -> tuple[SpecialistRole, ...]:
        return self.TEAM_BLUEPRINTS.get(workspace_key, self.TEAM_BLUEPRINTS["document"])

    def role_pipeline(self, workspace_key: str) -> tuple[str, ...]:
        # Compatibility surface retained for older workspace integrations.
        if workspace_key == "mockup":
            return ("기억 검색", "Vision 관찰", "레이아웃 설계", "제약 검증", "비파괴 렌더링")
        return tuple(role.label for role in self.roles(workspace_key))

    def describe_team(self, workspace_key: str) -> str:
        labels = tuple(role.label for role in self.roles(workspace_key))
        return "전문가 팀: " + " → ".join(labels) + " · 필요한 모델만 순차 실행/해제"

    def namespace(self, workspace_key: str) -> str:
        return f"{str(self.namespace_provider() or 'global')}:specialist:{workspace_key}"

    @contextmanager
    def run(self, workspace_key: str, instruction: str, *, artifacts: dict | None = None) -> Iterator[TeamRun]:
        state = TeamRun(workspace_key, instruction, dict(artifacts or {}))
        previous = getattr(self._active_run, "value", None)
        self._active_run.value = state
        try:
            yield state
        finally:
            self._active_run.value = previous

    def execute_role(self, run: TeamRun, role_key: str, handler: Callable[[TeamRun], Any],
                     *, release: Callable[[], Any] | None = None) -> Any:
        role = next((item for item in self.roles(run.workspace_key) if item.key == role_key), None)
        if role is None:
            raise KeyError(f"등록되지 않은 전문가 역할입니다: {role_key}")
        missing = [name for name in role.input_artifacts if name not in run.artifacts]
        if missing and not role.optional:
            raise ValueError(f"{role.label} 입력 산출물이 없습니다: {missing}")
        started = time.perf_counter()
        try:
            value = handler(run)
            if role.output_artifact:
                run.artifacts[role.output_artifact] = value
            run.record(role, "completed", started)
            return value
        except Exception as exc:
            run.record(role, "failed", started, str(exc))
            raise
        finally:
            if release is not None:
                try:
                    release()
                except Exception:
                    pass

    def recall(self, workspace_key: str, query: str, *, top_k: int = 6) -> SpecialistContext:
        if self.rag is None or not str(query).strip():
            return SpecialistContext(workspace_key, query, ())
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
        if self.rag is None or not str(instruction).strip():
            return ""
        summary = {
            "workspace": workspace_key,
            "instruction": " ".join(str(instruction).split())[:1200],
            "approved": bool(approved), "renderer": str(result.get("renderer", "")),
            "applied_fields": list(result.get("applied_edit_fields") or ()),
            "profile_id": str(result.get("profile_id", "")),
            "team_events": list(result.get("team_events") or ()),
            "quality_verdict": result.get("quality_verdict") or {},
            "recorded_at": time.time(),
        }
        digest = hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        namespace = self.namespace(workspace_key)
        with self._rag_lock:
            doc_id = self.rag.add_text_document(
                json.dumps(summary, ensure_ascii=False), doc_id=f"workspace-event-{digest}",
                namespace=namespace,
                metadata={"source_type": "specialist_workspace", "approved": bool(approved)},
                source_uri=f"specialist://{workspace_key}/{digest}",
            )
        if approved and self.event_pipeline is not None:
            self.event_pipeline.record_approved_result(
                workspace=namespace, instruction=instruction, result=result,
                needs_consolidation=False,
            )
        return doc_id
