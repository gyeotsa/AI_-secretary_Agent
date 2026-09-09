"""Shared memory and resource-aware role orchestration for specialist workspaces."""
from __future__ import annotations

import hashlib
import base64
import inspect
import json
from pathlib import Path
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, is_dataclass, replace
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
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    workspace_contract: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "queued"
    current_role: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def record(self, role: SpecialistRole, status: str, started_at: float, detail: str = "") -> None:
        if status in {"failed", "degraded"}:
            self.status = status
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

    def __init__(self, rag=None, *, namespace_provider=None, event_pipeline=None, supervisor=None,
                 tool_name_provider=None, review_client_provider=None):
        self.rag = rag
        self.namespace_provider = namespace_provider or (lambda: "global")
        self.event_pipeline = event_pipeline
        self.supervisor = supervisor
        self.tool_name_provider = tool_name_provider or self._registered_tool_names
        self.review_client_provider = review_client_provider
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
    def run(self, workspace_key: str, instruction: str, *, artifacts: dict | None = None,
            workspace_contract: dict | None = None) -> Iterator[TeamRun]:
        state = TeamRun(workspace_key=workspace_key, instruction=instruction,
                        artifacts=dict(artifacts or {}),
                        workspace_contract=dict(workspace_contract or {}))
        previous = getattr(self._active_run, "value", None)
        self._active_run.value = state
        state.status = "running"
        with self._runs_lock:
            self._runs[state.run_id] = state
        self._publish("specialist_team.started", state)
        try:
            yield state
            if state.status == "running":
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
            if role.output_artifact and not self._has_meaningful_output(value):
                raise ValueError(
                    f"{role.label}가 필수 산출물 '{role.output_artifact}'을 "
                    "비어 있는 상태로 반환했습니다."
                )
            if role.output_artifact:
                run.artifacts[role.output_artifact] = value
            run.record(role, "completed", started)
            if contract is not None:
                contract_artifacts = [{
                    "kind": role.output_artifact, "value_type": type(value).__name__,
                }] if role.output_artifact else []
                contract_evidence = [{
                    "kind": "role_execution", "elapsed_ms": run.events[-1]["elapsed_ms"],
                }]
                acceptance = value if role.output_artifact == "quality_verdict" and isinstance(value, dict) else {}
                failure_reason = ""
                if acceptance:
                    contract_evidence.append({
                        "kind": "quality_acceptance", "passed": acceptance.get("passed"),
                        "status": acceptance.get("status", ""),
                        "reason": str(acceptance.get("reason", ""))[:2000],
                    })
                    # Waiting is a handoff, not a rejected artifact. The owning
                    # workspace preserves these non-terminal task states.
                    if (acceptance.get("passed") is False and acceptance.get("status") not in {
                            "awaiting_user", "awaiting_input", "awaiting_approval"}):
                        failure_reason = str(acceptance.get("reason") or "전문가 산출물 수락 검수를 통과하지 못했습니다.")
                if acceptance.get("status") == "cancelled":
                    # A cancelled execution can reach the reviewer as a valid
                    # return value. Preserve cancellation rather than converting
                    # it to a successful review or a generic quality failure.
                    contract.artifacts = contract_artifacts
                    contract.evidence = contract_evidence
                    self.supervisor.transition(contract, "cancelled", failure_reason=failure_reason)
                    run.status = "cancelled"
                    run.events[-1]["status"] = "cancelled"
                else:
                    # Producing a verdict is successful role execution, but a
                    # negative verdict is not successful output acceptance.
                    self.supervisor.verify(contract, artifacts=contract_artifacts,
                                           evidence=contract_evidence, failure_reason=failure_reason)
            return value
        except Exception as exc:
            cancelled = contract is not None and contract.status.value == "cancelled"
            run.record(role, "cancelled" if cancelled else "failed", started, str(exc))
            if cancelled:
                run.status = "cancelled"
            elif contract is not None:
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
                "workspace_contract": dict(item.workspace_contract),
            } for item in values]

    def execute_workspace_request(
        self, workspace_key: str, instruction: str, *, attachments: list[str] | tuple[str, ...] = (),
        invoke_executor: Callable[[str], Any], release_models: bool = True,
    ) -> tuple[Any, TeamRun]:
        """Run one real workspace request through memory, planning, execution and review.

        Executor owns ToolRunResult evidence, while workspace acceptance owns final
        completion. Successful tool calls cannot bypass an unsuccessful review.
        """
        key = str(workspace_key or "document").casefold()
        roles = self.roles(key)
        execution_key = self.EXECUTION_ROLES.get(key)
        if execution_key is None:
            raise KeyError(f"실행 가능한 전문가 팀이 등록되지 않았습니다: {key}")
        from core.specialist_workspaces import get_specialist_workspace_registry
        spec = get_specialist_workspace_registry().get(key)
        if spec is None:
            raise KeyError(f"작업공간 실행 계약이 등록되지 않았습니다: {key}")
        workspace_contract = self._resolve_workspace_contract(
            spec.execution_contract(), executor_available=callable(invoke_executor),
        )
        paths = [str(item) for item in attachments if str(item).strip()]
        with self.run(
            key, instruction,
            artifacts={"attachments": paths, "workspace_contract": workspace_contract},
            workspace_contract=workspace_contract,
        ) as run:
            if not workspace_contract["readiness"]["ready"]:
                run.status = "failed"
                missing = workspace_contract["readiness"]["missing_tool_families"]
                failed_checks = workspace_contract["readiness"]["failed_checks"]
                raise RuntimeError(
                    "전문가 작업공간 실행 준비가 되지 않았습니다: "
                    f"누락 도구={missing}, 실패 점검={failed_checks}"
                )
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
                    planning_role, instruction, memory, paths, workspace_contract
                ),
                release=(lambda: self._release_role(planning_role.model_role))
                if release_models else None,
            )
            if plan.get("planning_status") != "ready":
                run.status = "degraded"
                run.events[-1]["status"] = "degraded"
                run.events[-1]["detail"] = str(plan.get("planning_error", "계획 품질 저하"))[:500]
            execution_role = next(role for role in roles if role.key == execution_key)

            def execute(_run: TeamRun):
                execution_context = self._execution_context(
                    key, memory, paths, plan,
                )
                allowed_tool_names = list(
                    workspace_contract.get("readiness", {}).get("resolved_tools") or ()
                )
                try:
                    signature = inspect.signature(invoke_executor)
                    accepts_scope = (
                        "allowed_tool_names" in signature.parameters
                        or any(parameter.kind == inspect.Parameter.VAR_KEYWORD
                               for parameter in signature.parameters.values())
                    )
                    accepts_context = (
                        "execution_context" in signature.parameters
                        or any(parameter.kind == inspect.Parameter.VAR_KEYWORD
                               for parameter in signature.parameters.values())
                    )
                except (TypeError, ValueError):
                    accepts_scope = False
                    accepts_context = False
                invoke_kwargs = {}
                if accepts_scope:
                    invoke_kwargs["allowed_tool_names"] = allowed_tool_names
                if accepts_context:
                    invoke_kwargs["execution_context"] = execution_context
                # Preserve the original user utterance exactly at the intent
                # boundary. Structured control data travels out-of-band.
                outcome = invoke_executor(instruction, **invoke_kwargs)
                return self._outcome_payload(outcome)

            executed = self.execute_role(run, execution_role.key, execute)
            reviewer = next(role for role in roles if role.key == "reviewer")
            def review(_run: TeamRun):
                verdict = self._review_execution(executed, workspace_contract,
                    planning_degraded=plan.get("planning_status") != "ready")
                if verdict["passed"] and key in {"document", "coding", "research"}:
                    from core.specialist_acceptance import review_requirement_fulfillment
                    semantic = review_requirement_fulfillment(executed, workspace_contract, plan,
                        instruction, client_provider=self.review_client_provider)
                    verdict["semantic_review"] = semantic
                    if not semantic["passed"]:
                        verdict.update(passed=False, status="needs_review", reason=semantic["reason"])
                    verdict["readiness_checks"]["semantic_requirements_verified"] = semantic["passed"]
                return verdict

            verdict = self.execute_role(run, reviewer.key, review,
                release=(lambda: self._release_role("reasoning")) if release_models else None)
            outcome = executed.pop("_outcome")
            run.artifacts["execution_contract_result"] = {
                "contract": workspace_contract,
                "execution": {key: value for key, value in executed.items()},
                "review": verdict,
            }
            if not verdict["passed"] and run.status != "failed":
                run.status = verdict["status"] if verdict["status"] in {
                    "awaiting_user", "awaiting_input", "awaiting_approval", "cancelled",
                } else "degraded"
                run.events[-1]["status"] = run.status
                run.events[-1]["detail"] = verdict["reason"][:500]
            if getattr(outcome, "status", "") == "completed" and not verdict["passed"]:
                # Preserve verified artifacts and original execution in the audit
                # record, but do not leak the executor's unqualified completion.
                response = (
                    "작업 결과는 생성됐지만 최종 검수를 통과하지 못해 완료로 처리하지 않았습니다.\n"
                    f"확인할 내용: {verdict['reason']}"
                )
                accepted = verdict.get("accepted_artifacts") or []
                if accepted:
                    response += "\n검수 대상 결과: " + ", ".join(item["uri"] for item in accepted)
                updates = {"status": "partial", "response": response, "next_goal": ""}
                if is_dataclass(outcome):
                    outcome = replace(outcome, **updates)
                else:
                    import copy
                    outcome = copy.copy(outcome)
                    for name, value in updates.items():
                        setattr(outcome, name, value)
            return outcome, run

    @staticmethod
    def _execution_context(workspace_key: str, memory: str, paths: list[str],
                           plan: dict[str, Any]) -> str:
        """Build planner-only context without leaking tool catalogs into intent text."""
        compact_plan = {
            key: plan.get(key)
            for key in ("goal", "inputs", "constraints", "acceptance")
            if plan.get(key) not in (None, "", [])
        }
        payload = {
            "workspace": workspace_key,
            "attachments": list(paths),
            "approved_memory": memory,
            "requirements": compact_plan,
            "policy": "사용자 원문 우선, 실제 도구 증거가 없으면 완료 주장 금지",
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _outcome_payload(outcome: Any) -> dict[str, Any]:
        tool_result = getattr(outcome, "tool_result", None)
        tool_results = list(getattr(outcome, "tool_results", ()) or ())
        if tool_result is not None and all(item is not tool_result for item in tool_results):
            tool_results.append(tool_result)
        evidence = [
            item for result in tool_results
            for item in (getattr(result, "evidence", ()) or ())
        ]
        artifacts = [
            item for result in tool_results
            for item in (getattr(result, "artifacts", ()) or ())
        ]
        tool_names = [
            str(getattr(result, "tool_name", "")).strip()
            for result in tool_results
            if str(getattr(result, "tool_name", "")).strip()
        ]
        tool_statuses = []
        for result in tool_results:
            status = getattr(result, "status", "")
            tool_statuses.append(str(status.value if hasattr(status, "value") else status))
        if tool_statuses and all(status == "succeeded" for status in tool_statuses):
            aggregate_status = "succeeded"
        elif tool_statuses:
            aggregate_status = next(
                (status for status in tool_statuses if status != "succeeded"),
                tool_statuses[-1],
            )
        else:
            aggregate_status = ""
        return {
            "_outcome": outcome,
            "status": str(getattr(outcome, "status", "failed")),
            "response": str(getattr(outcome, "response", "")),
            "task_id": str(getattr(outcome, "task_id", "")),
            "completed_steps": int(getattr(outcome, "completed_steps", 0) or 0),
            "failed_steps": int(getattr(outcome, "failed_steps", 0) or 0),
            "tool_name": tool_names[0] if len(tool_names) == 1 else "",
            "tool_names": list(dict.fromkeys(tool_names)),
            "tool_status": aggregate_status,
            "tool_statuses": tool_statuses,
            "tool_outputs": [{"tool_name": str(getattr(result, "tool_name", "")),
                              "status": tool_statuses[index],
                              "raw_output": str(getattr(result, "raw_output", ""))}
                             for index, result in enumerate(tool_results)],
            "evidence": [SpecialistTeamRuntime._evidence_payload(item) for item in evidence],
            "artifacts": [SpecialistTeamRuntime._artifact_payload(item) for item in artifacts],
            "evidence_count": len(evidence), "artifact_count": len(artifacts),
        }

    @staticmethod
    def _review_execution(payload: dict[str, Any], contract: dict[str, Any], *,
                          planning_degraded: bool = False) -> dict[str, Any]:
        status = payload.get("status")
        # Clarification and approval are valid non-terminal outcomes, but they
        # are not recorded as successful work or approved style memory.
        if status in {"awaiting_user", "awaiting_input", "awaiting_approval", "cancelled"}:
            return {"passed": False, "status": status,
                    "reason": "작업이 취소되었습니다." if status == "cancelled" else "사용자 입력 또는 승인을 기다리고 있습니다."}
        failures: list[str] = []
        if status != "completed":
            failures.append(f"실행 상태가 완료가 아닙니다: {status}")
        if payload.get("tool_status") != "succeeded":
            failures.append(f"도구 결과가 검증된 성공이 아닙니다: {payload.get('tool_status') or '없음'}")

        evidence = [item for item in payload.get("evidence", [])
                    if isinstance(item, dict) and item.get("kind") and item.get("summary")]
        if not evidence:
            failures.append("종류와 설명이 있는 실제 검증 근거가 없습니다.")
        for item in evidence:
            data = dict(item.get("data") or {})
            if item.get("kind") == "docx_structure" and int(data.get("paragraphs") or 0) <= 0:
                failures.append("Word 산출물이 존재하지만 실제 문단 내용이 없는 빈 문서입니다.")

        expected_types = set(contract.get("artifact_types") or ())
        artifacts = [item for item in payload.get("artifacts", [])
                     if isinstance(item, dict)
                     and item.get("kind") in expected_types and item.get("uri")
                     and (item.get("metadata") or {}).get("role") != "input"]
        valid_artifacts = [item for item in artifacts if SpecialistTeamRuntime._artifact_exists(item)]
        if len(valid_artifacts) != len(artifacts):
            failures.append("일부 최종 산출물이 없거나 손상되어 모든 결과를 검증하지 못했습니다.")
        if expected_types and not valid_artifacts:
            failures.append("작업공간 산출물 계약을 충족하는 실제 산출물이 없습니다.")

        tool_names = {
            str(item).strip() for item in (
                payload.get("tool_names") or [payload.get("tool_name", "")]
            ) if str(item).strip()
        }
        resolved_tools = set(contract.get("readiness", {}).get("resolved_tools") or ())
        unexpected_tools = sorted(tool_names - resolved_tools) if resolved_tools else []
        if unexpected_tools:
            failures.append(
                "작업공간 계약 밖의 도구가 실행되었습니다: " + ", ".join(unexpected_tools)
            )
        if planning_degraded:
            failures.append("전문가 계획이 구조화 검증을 통과하지 못해 저하 모드로 실행되었습니다.")

        criteria = list(contract.get("acceptance_criteria") or ())
        verifier_names = list(contract.get("acceptance_verifiers") or ())
        criteria_results = []
        if verifier_names:
            # A generic successful tool/file is not proof that an edit happened.
            # Contracts bind each criterion to an explicit typed evidence gate.
            from core.specialist_acceptance import verify_specialist_criteria
            verified_checks = verify_specialist_criteria(payload, contract, evidence, valid_artifacts)
            if len(verifier_names) != len(criteria):
                failures.append("수락 기준과 검증기 개수가 달라 완료를 확인할 수 없습니다.")
            for index, criterion in enumerate(criteria):
                verifier = verifier_names[index] if index < len(verifier_names) else ""
                check = verified_checks.get(verifier, {"verified": False, "reason": "알 수 없는 수락 기준 검증기입니다."})
                criteria_results.append({
                    **check,
                    "criterion": criterion, "verified": bool(check.get("verified")),
                    "verification": verifier or "not_verified", "reason": check.get("reason", ""),
                    "evidence": [item["summary"] for item in evidence],
                    "artifacts": [item["uri"] for item in valid_artifacts],
                })
                if not check.get("verified"):
                    failures.append(f"수락 기준 미충족({criterion}): {check.get('reason', '')}")
        else:
            # Legacy ad-hoc contracts retain their structural check. Every
            # registered workspace must declare a real criterion verifier.
            if contract.get("workspace_key") and criteria:
                failures.append("작업공간 수락 기준에 실행 가능한 검증기가 연결되지 않았습니다.")
            criteria_results = [{
                "criterion": criterion,
                "verified": not failures,
                "verification": "tool_evidence_and_artifact_contract" if not failures else "not_verified",
                "evidence": [item["summary"] for item in evidence],
                "artifacts": [item["uri"] for item in valid_artifacts],
            } for criterion in criteria]
        passed = not failures
        return {
            "passed": passed,
            "status": "completed" if passed else "degraded",
            "reason": "산출물 계약, 도구 성공 상태, 실제 근거를 모두 확인했습니다."
            if passed else " ".join(failures),
            "accepted_artifacts": valid_artifacts,
            "evidence": evidence,
            "acceptance_criteria": list(contract.get("acceptance_criteria") or ()),
            "criteria_results": criteria_results,
            "readiness_checks": {
                **dict(contract.get("readiness", {}).get("checks") or {}),
                "verified_evidence": bool(evidence) and (not verifier_names or (
                    len(verifier_names) == len(criteria) and bool(criteria_results)
                    and all(item["verified"] for item in criteria_results)
                )),
                "artifact_contract_satisfied": bool(valid_artifacts) or not expected_types,
            },
        }

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
                               memory: str, attachments: list[str],
                               workspace_contract: dict[str, Any]) -> dict[str, Any]:
        """Use the selected role model and expose, rather than hide, degradation."""
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
                f"요청: {instruction}\n관련 기억: {memory}\n첨부: {attachments}\n"
                f"실행 계약: {json.dumps(workspace_contract, ensure_ascii=False)}"
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
            string_array = {"type": "array", "items": {"type": "string"}}
            schema = {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "inputs": string_array,
                    "constraints": string_array,
                    "acceptance": string_array,
                    "tools": string_array,
                },
                "required": ["goal", "inputs", "constraints", "acceptance", "tools"],
                "additionalProperties": False,
            }
            raw = client.chat_structured(messages, json_schema=schema)
            if isinstance(raw, dict):
                parsed = raw
            else:
                raw = str(raw or "")
                start, end = raw.find("{"), raw.rfind("}")
                parsed = json.loads(raw[start:end + 1]) if start >= 0 and end > start else None
            parsed = SpecialistTeamRuntime._normalize_specialist_plan(parsed)
            if SpecialistTeamRuntime._valid_specialist_plan(parsed):
                parsed["planning_status"] = "ready"
                parsed["workspace_contract"] = workspace_contract
                return parsed
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = "구조화된 요구사항을 반환하지 않았습니다."
        return {
            "goal": instruction, "inputs": list(attachments),
            "constraints": ["사용자 원문 우선", "실행 증거 없는 완료 주장 금지"],
            "acceptance": list(workspace_contract.get("acceptance_criteria") or ()),
            "tools": list(workspace_contract.get("readiness", {}).get("resolved_tools") or ()),
            "planning_status": "degraded", "planning_error": error,
            "workspace_contract": workspace_contract,
        }

    @staticmethod
    def _registered_tool_names() -> tuple[str, ...]:
        from core.tools import get_tool_names
        return tuple(get_tool_names())

    _TOOL_FAMILY_ALIASES = {
        "vision": ("analyze_image", "visual_analyze", "video_sample_analyze"),
        "image_renderer": ("mockup_render",),
        "rag_knowledge": ("rag_", "search_docs", "add_document"),
        "knowledge_memory": ("memory_", "add_semantic_memory", "search_semantic_memory"),
        "filesystem": ("filesystem_", "read_file", "write_file", "create_directory"),
        "system_tools": ("run_command", "runtime_", "gpu_runtime_status"),
    }

    @classmethod
    def _matches_tool_family(cls, tool_name: str, family: str) -> bool:
        name, group = str(tool_name), str(family)
        candidates = cls._TOOL_FAMILY_ALIASES.get(group, (f"{group}_", group))
        return any(name == candidate or name.startswith(candidate) for candidate in candidates)

    def _resolve_workspace_contract(self, contract: dict[str, Any], *,
                                    executor_available: bool = True) -> dict[str, Any]:
        registry_error = ""
        try:
            registered = tuple(dict.fromkeys(str(item) for item in self.tool_name_provider()))
        except Exception as exc:
            registered = ()
            registry_error = f"{type(exc).__name__}: {exc}"
        families = tuple(contract.get("required_tools") or ())
        family_matches = {
            family: [name for name in registered if self._matches_tool_family(name, family)]
            for family in families
        }
        missing = [family for family, matches in family_matches.items() if not matches]
        resolved = sorted({name for matches in family_matches.values() for name in matches})
        checks = {
            "required_tools_registered": not missing and not registry_error,
            "executor_available": bool(executor_available),
            # This is intentionally pending until the reviewer sees ToolRunResult.
            "verified_evidence": False,
        }
        requested_checks = tuple(contract.get("readiness_checks") or ())
        blocking_checks = [name for name in requested_checks
                           if name != "verified_evidence" and not checks.get(name, False)]
        return {
            **contract,
            "readiness": {
                "ready": not missing and not blocking_checks,
                "missing_tool_families": missing,
                "failed_checks": blocking_checks,
                "registry_error": registry_error,
                "resolved_tools": resolved,
                "family_matches": family_matches,
                "checks": checks,
            },
        }

    @staticmethod
    def _valid_specialist_plan(value: Any) -> bool:
        if (not isinstance(value, dict) or not isinstance(value.get("goal"), str)
                or not value["goal"].strip()):
            return False
        acceptance = value.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance or not all(
            isinstance(item, str) and item.strip() for item in acceptance
        ):
            return False
        for key in ("inputs", "constraints", "tools"):
            if key not in value or not isinstance(value[key], list) or not all(
                isinstance(item, str) and item.strip() for item in value[key]
            ):
                return False
        return True

    @staticmethod
    def _normalize_specialist_plan(value: Any) -> dict[str, Any] | None:
        """Normalize schema-near local-model output without hiding bad plans.

        Some local providers return ``[{"name": ...}]`` even when the schema
        asks for ``array[string]``.  We preserve the meaningful scalar value,
        but still reject missing/empty contract fields in ``_valid_specialist_plan``.
        """
        if not isinstance(value, dict):
            return None
        normalized = dict(value)
        key_preferences = {
            "inputs": ("value", "path", "content", "description", "name", "type"),
            "constraints": ("description", "value", "name", "type"),
            "acceptance": ("criterion", "description", "value", "name"),
            "tools": ("name", "tool", "value", "description"),
        }
        for field, preferences in key_preferences.items():
            # Normalization must not invent required fields or silently remove
            # invalid entries: either case could promote an incomplete plan.
            items = normalized.get(field)
            if not isinstance(items, list):
                continue
            converted: list[Any] = []
            for item in items:
                if isinstance(item, str) and item.strip():
                    converted.append(item.strip())
                    continue
                if not isinstance(item, dict):
                    converted.append(item)
                    continue
                scalar = next(
                    (item.get(key) for key in preferences
                     if isinstance(item.get(key), (str, int, float, bool))
                     and str(item.get(key)).strip()),
                    None,
                )
                if scalar is not None:
                    converted.append(str(scalar).strip())
                else:
                    converted.append(item)
            normalized[field] = (list(dict.fromkeys(converted))
                                 if all(isinstance(item, str) for item in converted)
                                 else converted)
        return normalized

    @staticmethod
    def _has_meaningful_output(value: Any) -> bool:
        """Reject role-play stages that produced no usable contract artifact."""
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (bytes, bytearray, list, tuple, set, frozenset, dict)):
            return bool(value)
        return True

    @staticmethod
    def _evidence_payload(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return {
                "kind": str(item.get("kind", "")),
                "summary": str(item.get("summary", "")),
                "data": dict(item.get("data") or {}),
            }
        return {
            "kind": str(getattr(item, "kind", "")),
            "summary": str(getattr(item, "summary", "")),
            "data": dict(getattr(item, "data", {}) or {}),
        }

    @staticmethod
    def _artifact_payload(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return {
                "kind": str(item.get("kind", "")),
                "uri": str(item.get("uri", "")),
                "metadata": dict(item.get("metadata") or {}),
            }
        return {
            "kind": str(getattr(item, "kind", "")),
            "uri": str(getattr(item, "uri", "")),
            "metadata": dict(getattr(item, "metadata", {}) or {}),
        }

    @staticmethod
    def _artifact_exists(item: dict[str, Any]) -> bool:
        kind, uri = str(item.get("kind", "")), str(item.get("uri", "")).strip()
        if not uri:
            return False
        if kind == "url":
            return uri.casefold().startswith(("http://", "https://"))
        if kind in {"knowledge_entity", "knowledge_relation", "semantic_memory"}:
            return True
        # A path-only shell is not a deliverable. This content-aware boundary
        # protects every specialist workspace, including future plugins.
        from core.artifact_validation import validate_local_artifact
        valid, _reason = validate_local_artifact(uri)
        return valid

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
        search = self.rag.search_docs
        target_namespace = self.namespace(workspace_key)
        kwargs = {"top_k": top_k}
        if self._accepts_keyword(search, "metadata_filter"):
            kwargs["metadata_filter"] = {"approved": True}

        if self._accepts_keyword(search, "namespace"):
            # Built-in RAG path: the namespace belongs to this recall operation,
            # so concurrent specialist teams cannot overwrite one another.
            results = search(query, namespace=target_namespace, **kwargs)
        else:
            # Compatibility boundary for legacy/lightweight adapters. Only this
            # path mutates their default namespace and it remains serialized.
            with self._rag_lock:
                original = getattr(self.rag, "namespace", "global")
                setter = getattr(self.rag, "set_namespace", None)
                try:
                    if callable(setter):
                        setter(target_namespace)
                    results = search(query, **kwargs)
                finally:
                    if callable(setter):
                        setter(original)

        approved = tuple(
            item for item in (results or ())
            if isinstance(item, dict) and item.get("approved") is True
        )
        return SpecialistContext(workspace_key, query, approved)

    @staticmethod
    def _accepts_keyword(callable_obj, keyword: str) -> bool:
        try:
            parameters = inspect.signature(callable_obj).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == keyword
            for parameter in parameters
        )

    def remember_success(self, workspace_key: str, *, instruction: str, result: dict,
                         approved: bool = False) -> str:
        # Drafts and merely executed outputs must never become retrieval memory.
        # The workspace UI calls this with approved=True only after the user saves
        # the reviewed preview/result.
        if not approved or self.rag is None or not str(instruction).strip():
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
