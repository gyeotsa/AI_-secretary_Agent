"""Shared memory and resource-aware role orchestration for specialist workspaces."""
from __future__ import annotations

import hashlib
import base64
import json
import threading
import time
import uuid
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
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    artifacts: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "queued"
    current_role: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def record(self, role: SpecialistRole, status: str, started_at: float, detail: str = "") -> None:
        self.status = status if status == "failed" else self.status
        self.current_role = role.key
        self.updated_at = time.time()
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
        "photoshop": (
            SpecialistRole("memory_curator", "기억 큐레이터", "reasoning", (), "memory_context"),
            SpecialistRole("visual_analyst", "시각 요구 분석가", "image_editing", (), "visual_requirements"),
            SpecialistRole("image_editor", "Photoshop 실행 전문가", "image_editing", ("visual_requirements",), "changes"),
            SpecialistRole("reviewer", "이미지 작업 검수자", "reasoning", ("changes",), "quality_verdict"),
        ),
        "research": (
            SpecialistRole("memory_curator", "기억 큐레이터", "reasoning", (), "memory_context"),
            SpecialistRole("research_planner", "조사 설계자", "reasoning", (), "research_plan"),
            SpecialistRole("researcher", "근거 수집 전문가", "reasoning", ("research_plan",), "report"),
            SpecialistRole("reviewer", "출처 검수자", "reasoning", ("report",), "quality_verdict"),
        ),
        "knowledge_graph": (
            SpecialistRole("memory_curator", "기억 큐레이터", "reasoning", (), "memory_context"),
            SpecialistRole("graph_analyst", "지식 그래프 분석가", "reasoning", (), "graph_plan"),
            SpecialistRole("curator", "Obsidian·RAG 큐레이터", "reasoning", ("graph_plan",), "changes"),
            SpecialistRole("reviewer", "기억 무결성 검수자", "reasoning", ("changes",), "quality_verdict"),
        ),
    }

    EXECUTION_ROLES = {
        "document": "author", "coding": "implementer", "photoshop": "image_editor",
        "research": "researcher", "knowledge_graph": "curator",
    }

    def __init__(self, rag=None, *, namespace_provider=None, event_pipeline=None, supervisor=None):
        self.rag = rag
        self.namespace_provider = namespace_provider or (lambda: "global")
        self.event_pipeline = event_pipeline
        self.supervisor = supervisor
        self._rag_lock = threading.RLock()
        self._active_run = threading.local()
        self._runs: dict[str, TeamRun] = {}
        self._runs_lock = threading.RLock()

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
        state = TeamRun(workspace_key=workspace_key, instruction=instruction,
                        artifacts=dict(artifacts or {}))
        previous = getattr(self._active_run, "value", None)
        self._active_run.value = state
        state.status = "running"
        with self._runs_lock:
            self._runs[state.run_id] = state
        self._publish("specialist_team.started", state)
        try:
            yield state
            if state.status != "failed":
                state.status = "completed"
        finally:
            state.updated_at = time.time()
            self._publish(f"specialist_team.{state.status}", state)
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
        contract = None
        if self.supervisor is not None:
            from core.task_contracts import AcceptanceCriterion, ResourceBudget, RetryPolicy
            from core.model_registry import get_model_registry
            profile = get_model_registry().resolve(role.model_role)
            contract = self.supervisor.create_contract(
                goal=f"{role.label}: {run.instruction}", specialist=role.key,
                input_contract={name: type(run.artifacts.get(name)).__name__ for name in role.input_artifacts},
                output_contract={"artifact": role.output_artifact},
                acceptance_criteria=(AcceptanceCriterion(role.output_artifact, role.output_artifact, "artifact"),)
                if role.output_artifact else (), resource_budget=ResourceBudget(
                    timeout_seconds=180, vram_mb=int(profile.vram_mb),
                    ram_mb=int(profile.ram_mb), max_parallel=1,
                ),
                retry_policy=RetryPolicy(max_attempts=2, escalate_after=2), parent_id=run.run_id,
            )
            self.supervisor.begin_attempt(contract)
        try:
            if contract is not None:
                if self.supervisor.cancellation_requested(contract):
                    self.supervisor.transition(contract, "cancelled",
                                               failure_reason="사용자가 작업을 취소했습니다.")
                    raise RuntimeError("사용자가 작업을 취소했습니다.")
                with self.supervisor.resource_guard(contract):
                    value = handler(run)
                if self.supervisor.cancellation_requested(contract):
                    self.supervisor.transition(contract, "cancelled",
                                               failure_reason="실행 중 취소 요청을 반영했습니다.")
                    raise RuntimeError("실행 중 취소되었습니다.")
            else:
                value = handler(run)
            if role.output_artifact:
                run.artifacts[role.output_artifact] = value
            run.record(role, "completed", started)
            if contract is not None:
                self.supervisor.verify(contract, artifacts=[{
                    "kind": role.output_artifact, "value_type": type(value).__name__,
                }] if role.output_artifact else [], evidence=[{
                    "kind": "role_execution", "elapsed_ms": run.events[-1]["elapsed_ms"],
                }])
            return value
        except Exception as exc:
            run.record(role, "failed", started, str(exc))
            if contract is not None and contract.status.value != "cancelled":
                self.supervisor.verify(contract, failure_reason=str(exc))
            raise
        finally:
            if release is not None:
                try:
                    release()
                except Exception:
                    pass

    def snapshot(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._runs_lock:
            values = sorted(self._runs.values(), key=lambda item: item.updated_at, reverse=True)[:limit]
            return [{
                "run_id": item.run_id, "workspace_key": item.workspace_key,
                "instruction": item.instruction, "status": item.status,
                "current_role": item.current_role, "started_at": item.started_at,
                "updated_at": item.updated_at, "events": list(item.events),
                "artifacts": list(item.artifacts),
            } for item in values]

    def execute_workspace_request(
        self, workspace_key: str, instruction: str, *, attachments: list[str] | tuple[str, ...] = (),
        invoke_executor: Callable[[str], Any], release_models: bool = True,
    ) -> tuple[Any, TeamRun]:
        """Run one real workspace request through memory, planning, execution and review.

        Planning output is advisory context.  The only stage allowed to claim work
        completion is the shared Executor, because it owns ToolRunResult evidence and
        task-state verification.  This prevents a role-playing LLM from being shown as
        a worker when it did not touch the requested application or file.
        """
        key = str(workspace_key or "document").casefold()
        roles = self.roles(key)
        execution_key = self.EXECUTION_ROLES.get(key)
        if execution_key is None:
            raise KeyError(f"실행 가능한 전문가 팀이 등록되지 않았습니다: {key}")
        paths = [str(item) for item in attachments if str(item).strip()]
        with self.run(key, instruction, artifacts={"attachments": paths}) as run:
            memory_role = roles[0]
            memory = self.execute_role(
                run, memory_role.key,
                lambda _run: self.recall(key, instruction).as_prompt(),
            )
            planning_role = next(
                role for role in roles
                if role.key not in {memory_role.key, execution_key, "reviewer"}
            )
            plan = self.execute_role(
                run, planning_role.key,
                lambda _run: self._build_specialist_plan(
                    planning_role, instruction, memory, paths
                ),
                release=(lambda: self._release_role(planning_role.model_role))
                if release_models else None,
            )
            execution_role = next(role for role in roles if role.key == execution_key)

            def execute(_run: TeamRun):
                attachment_text = "\n".join(f"- {path}" for path in paths) or "- 없음"
                enriched = (
                    f"사용자 요청: {instruction}\n\n"
                    f"전문 작업공간: {key}\n첨부 파일:\n{attachment_text}\n\n"
                    f"이 작업공간에서 승인된 관련 기억:\n{memory}\n\n"
                    f"전문가 요구사항 분석:\n{json.dumps(plan, ensure_ascii=False, default=str)}\n\n"
                    "사용자 요청을 최우선으로 실제 도구를 실행하고, 실행 증거가 없으면 완료라고 말하지 마세요."
                )
                outcome = invoke_executor(enriched)
                return self._outcome_payload(outcome)

            executed = self.execute_role(run, execution_role.key, execute)
            reviewer = next(role for role in roles if role.key == "reviewer")
            verdict = self.execute_role(
                run, reviewer.key,
                lambda _run: self._review_execution(executed),
            )
            outcome = executed.pop("_outcome")
            if verdict["passed"]:
                self.remember_success(
                    key, instruction=instruction,
                    result={**executed, "team_events": list(run.events),
                            "quality_verdict": verdict},
                    approved=False,
                )
            return outcome, run

    @staticmethod
    def _outcome_payload(outcome: Any) -> dict[str, Any]:
        tool_result = getattr(outcome, "tool_result", None)
        evidence = list(getattr(tool_result, "evidence", ()) or ())
        artifacts = list(getattr(tool_result, "artifacts", ()) or ())
        return {
            "_outcome": outcome,
            "status": str(getattr(outcome, "status", "failed")),
            "response": str(getattr(outcome, "response", "")),
            "task_id": str(getattr(outcome, "task_id", "")),
            "completed_steps": int(getattr(outcome, "completed_steps", 0) or 0),
            "failed_steps": int(getattr(outcome, "failed_steps", 0) or 0),
            "evidence_count": len(evidence), "artifact_count": len(artifacts),
        }

    @staticmethod
    def _review_execution(payload: dict[str, Any]) -> dict[str, Any]:
        status = payload.get("status")
        completed_steps = int(payload.get("completed_steps", 0) or 0)
        evidence_count = int(payload.get("evidence_count", 0) or 0)
        passed = status in {"completed", "partial"} and (
            completed_steps > 0 or evidence_count > 0
        )
        # Clarification and approval are valid non-terminal outcomes, but they
        # are not recorded as successful work or approved style memory.
        if status in {"awaiting_user", "awaiting_input", "awaiting_approval"}:
            return {"passed": False, "status": status,
                    "reason": "사용자 입력 또는 승인을 기다리고 있습니다."}
        return {"passed": passed, "status": status,
                "reason": "검증된 실행 단계/근거 확인" if passed
                else "실행 증거가 없거나 작업이 실패했습니다."}

    @staticmethod
    def _release_role(model_role: str) -> None:
        try:
            from core.llm import get_llm_client
            client = get_llm_client(model_role)
            release = getattr(client, "release", None)
            if callable(release):
                release()
        except Exception:
            pass

    @staticmethod
    def _build_specialist_plan(role: SpecialistRole, instruction: str,
                               memory: str, attachments: list[str]) -> dict[str, Any]:
        """Use the selected role model; fall back to an explicit minimal plan."""
        try:
            from core.llm import get_llm_client
            client = get_llm_client(role.model_role)
            messages: list[dict[str, Any]] = [{
                "role": "system",
                "content": (
                    "너는 실행 전 요구사항 분석 담당자다. 실제 실행 완료를 주장하지 말고, "
                    "사용자 목표·입력·제약·검수 기준·필요 도구를 JSON 객체로만 정리한다."
                ),
            }, {"role": "user", "content": (
                f"요청: {instruction}\n관련 기억: {memory}\n첨부: {attachments}"
            )}]
            if "image" in getattr(getattr(client, "profile", None), "modalities", ()):
                images = []
                for path in attachments[:4]:
                    try:
                        with open(path, "rb") as handle:
                            images.append(base64.b64encode(handle.read()).decode("ascii"))
                    except OSError:
                        continue
                if images:
                    messages[-1]["images"] = images
            schema = {"type": "object", "properties": {
                "goal": {"type": "string"}, "inputs": {"type": "array"},
                "constraints": {"type": "array"}, "acceptance": {"type": "array"},
                "tools": {"type": "array"},
            }, "required": ["goal", "acceptance"]}
            raw = client.chat_structured(messages, json_schema=schema)
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                parsed = json.loads(raw[start:end + 1])
                if isinstance(parsed, dict) and parsed.get("goal"):
                    return parsed
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = "구조화된 요구사항을 반환하지 않았습니다."
        return {
            "goal": instruction, "inputs": list(attachments),
            "constraints": ["사용자 원문 우선", "실행 증거 없는 완료 주장 금지"],
            "acceptance": ["요청 결과를 실제 도구 결과와 증거로 검증"],
            "tools": [], "planner_fallback": error,
        }

    @staticmethod
    def _publish(event_type: str, run: TeamRun) -> None:
        try:
            from core.runtime.event_bus import Event, get_event_bus
            get_event_bus().publish(Event(type=event_type, source="specialist_team", data={
                "run_id": run.run_id, "workspace": run.workspace_key,
                "status": run.status, "current_role": run.current_role,
                "instruction": run.instruction[:500],
            }))
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
