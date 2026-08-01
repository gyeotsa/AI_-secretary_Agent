"""Executable Plan DAG and bounded execution/recovery coordinator."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.tool_result import Artifact, ToolRunResult, ToolRunStatus


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    id: str
    description: str = ""
    tool_name: str = ""
    tool_input: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    preconditions: List[str] = field(default_factory=list)
    expected_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    verification: Dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    approval_reason: str = ""
    retry_budget: int = 2
    retry_strategies: List[str] = field(default_factory=lambda: ["retry", "replan"])
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    last_error_signature: str = ""
    observations: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.status, str):
            self.status = StepStatus(self.status)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.id):
            raise ValueError(f"유효하지 않은 단계 ID입니다: {self.id}")
        if self.retry_budget < 0 or self.retry_budget > 10:
            raise ValueError("retry_budget은 0~10 범위여야 합니다.")
        if self.requires_approval and not self.approval_reason.strip():
            raise ValueError(f"승인 단계에는 이유가 필요합니다: {self.id}")

    @property
    def required_tools(self) -> List[str]:
        return [self.tool_name] if self.tool_name else []


@dataclass
class PlanDAG:
    goal: str
    steps: List[PlanStep]
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    revision: int = 1

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Plan 단계 ID가 중복되었습니다.")
        known = set(ids)
        for step in self.steps:
            missing = set(step.dependencies) - known
            if missing:
                raise ValueError(f"{step.id}의 존재하지 않는 의존성: {sorted(missing)}")
            if step.id in step.dependencies:
                raise ValueError(f"단계가 자기 자신에 의존할 수 없습니다: {step.id}")
        visiting: set[str] = set()
        visited: set[str] = set()
        graph = {step.id: step.dependencies for step in self.steps}

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("Plan DAG에 순환 의존성이 있습니다.")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)
        for node in ids:
            visit(node)

    def by_id(self) -> Dict[str, PlanStep]:
        return {step.id: step for step in self.steps}

    def ready_steps(self) -> List[PlanStep]:
        lookup = self.by_id()
        return [
            step for step in self.steps
            if step.status == StepStatus.PENDING
            and all(lookup[item].status == StepStatus.COMPLETED for item in step.dependencies)
        ]

    def terminal(self) -> bool:
        return all(step.status in {
            StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.SKIPPED,
            StepStatus.AWAITING_APPROVAL,
        } for step in self.steps)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        for step in value["steps"]:
            step["status"] = step["status"].value if isinstance(step["status"], StepStatus) else step["status"]
        return value

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PlanDAG":
        return cls(
            goal=str(payload["goal"]),
            steps=[PlanStep(**item) for item in payload.get("steps", [])],
            plan_id=str(payload["plan_id"]), revision=int(payload.get("revision", 1)),
        )


class ErrorClassifier:
    TRANSIENT = ("timeout", "timed out", "temporarily", "connection reset", "429", "503")
    PERMISSION = ("permission", "access denied", "권한", "승인")
    VALIDATION = ("validation", "schema", "invalid", "검증", "형식")

    @classmethod
    def classify(cls, result: ToolRunResult) -> str:
        text = f"{result.error or ''} {result.raw_output}".casefold()
        if any(token in text for token in cls.PERMISSION):
            return "permission"
        if any(token in text for token in cls.TRANSIENT):
            return "transient"
        if any(token in text for token in cls.VALIDATION):
            return "validation"
        if result.status == ToolRunStatus.UNVERIFIED:
            return "unverified"
        return "execution"

    @classmethod
    def signature(cls, step: PlanStep, result: ToolRunResult) -> str:
        normalized = re.sub(r"\b\d+\b", "#", f"{result.error or ''}|{result.raw_output}".casefold())
        normalized = re.sub(r"\s+", " ", normalized).strip()[:500]
        payload = f"{step.tool_name}|{cls.classify(result)}|{normalized}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


class PlanExecutionStore:
    """Persists plan revisions, observations, strategies, budgets, and signatures."""

    def __init__(self, db_path: str = "data/plan_runtime.db"):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS plan_runs (
                plan_id TEXT PRIMARY KEY, goal TEXT NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS step_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, step_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, strategy TEXT NOT NULL, error_class TEXT NOT NULL,
                error_signature TEXT NOT NULL, result_status TEXT NOT NULL,
                observation TEXT NOT NULL, created_at REAL NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_attempt_signature ON step_attempts(plan_id,step_id,error_signature)")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_plan(self, plan: PlanDAG, status: str = "running") -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO plan_runs(plan_id,goal,revision,status,payload,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET revision=excluded.revision,
                status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at""",
                (plan.plan_id, plan.goal, plan.revision, status,
                 json.dumps(plan.to_dict(), ensure_ascii=False), time.time()))

    def load_plan(self, plan_id: str) -> Optional[PlanDAG]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT payload FROM plan_runs WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            return None
        return PlanDAG.from_dict(json.loads(row[0]))

    def record_attempt(self, plan_id: str, step: PlanStep, strategy: str,
                       result: ToolRunResult, signature: str) -> None:
        observation = {"raw_output": result.raw_output, "error": result.error,
                       "evidence": [asdict(item) for item in result.evidence],
                       "artifacts": [asdict(item) for item in result.artifacts]}
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT INTO step_attempts
                (plan_id,step_id,attempt,strategy,error_class,error_signature,result_status,observation,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (plan_id, step.id, step.attempts, strategy, ErrorClassifier.classify(result),
                 signature, result.status.value, json.dumps(observation, ensure_ascii=False), time.time()))

    def signature_count(self, plan_id: str, step_id: str, signature: str) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("""SELECT COUNT(*) FROM step_attempts
                WHERE plan_id=? AND step_id=? AND error_signature=?""",
                (plan_id, step_id, signature)).fetchone()[0])


@dataclass
class PlanRunResult:
    plan: PlanDAG
    status: str
    results: Dict[str, ToolRunResult] = field(default_factory=dict)
    awaiting_approval: List[str] = field(default_factory=list)


class PlanCoordinator:
    """Runs ready DAG nodes concurrently and owns recovery/replanning policy."""

    def __init__(self, store: Optional[PlanExecutionStore] = None, max_parallel: int = 4,
                 duplicate_failure_limit: int = 2, max_replans: int = 2):
        self.store = store or PlanExecutionStore()
        self.max_parallel = max(1, max_parallel)
        self.duplicate_failure_limit = max(1, duplicate_failure_limit)
        self.max_replans = max(0, max_replans)

    def run(
        self, plan: PlanDAG,
        execute: Callable[[PlanStep, str], ToolRunResult],
        verify: Callable[[PlanStep, ToolRunResult], ToolRunResult],
        approve: Optional[Callable[[PlanStep], bool]] = None,
        replan: Optional[Callable[[PlanDAG, PlanStep, ToolRunResult], Optional[PlanDAG]]] = None,
    ) -> PlanRunResult:
        results: Dict[str, ToolRunResult] = {}
        replan_count = 0
        self.store.save_plan(plan)
        while True:
            ready = plan.ready_steps()
            if not ready:
                break
            runnable: List[PlanStep] = []
            awaiting: List[str] = []
            for step in ready:
                if step.requires_approval and (approve is None or not approve(step)):
                    step.status = StepStatus.AWAITING_APPROVAL
                    awaiting.append(step.id)
                elif not self._preconditions_met(step, results):
                    step.status = StepStatus.BLOCKED
                else:
                    runnable.append(step)
            if awaiting and not runnable:
                self.store.save_plan(plan, "awaiting_approval")
                return PlanRunResult(plan, "awaiting_approval", results, awaiting)
            if not runnable:
                continue
            with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(runnable))) as pool:
                futures = {pool.submit(self._run_step, plan, step, execute, verify): step for step in runnable}
                for future in as_completed(futures):
                    step = futures[future]
                    result = future.result()
                    results[step.id] = result
                    if (step.status == StepStatus.FAILED and replan is not None
                            and replan_count < self.max_replans):
                        replacement = replan(plan, step, result)
                        if replacement is not None:
                            replan_count += 1
                            replacement.revision = plan.revision + 1
                            replacement.plan_id = plan.plan_id
                            replacement.validate()
                            plan = replacement
                            self.store.save_plan(plan, "replanned")
                            break
            self._block_dependants_of_failed_steps(plan)
            self.store.save_plan(plan)
        status = "completed" if all(s.status == StepStatus.COMPLETED for s in plan.steps) else "failed"
        self.store.save_plan(plan, status)
        return PlanRunResult(plan, status, results)

    def resume_approved(
        self, plan: PlanDAG, approved_step_ids: Iterable[str],
        execute: Callable[[PlanStep, str], ToolRunResult],
        verify: Callable[[PlanStep, ToolRunResult], ToolRunResult],
        replan: Optional[Callable[[PlanDAG, PlanStep, ToolRunResult], Optional[PlanDAG]]] = None,
    ) -> PlanRunResult:
        approved = set(approved_step_ids)
        awaiting = {step.id for step in plan.steps if step.status == StepStatus.AWAITING_APPROVAL}
        unknown = approved - awaiting
        if unknown:
            raise ValueError(f"승인 대기 중이 아닌 단계입니다: {sorted(unknown)}")
        for step in plan.steps:
            if step.id in approved:
                step.status = StepStatus.PENDING
        return self.run(plan, execute, verify, approve=lambda step: step.id in approved, replan=replan)

    @staticmethod
    def _preconditions_met(step: PlanStep, results: Dict[str, ToolRunResult]) -> bool:
        for condition in step.preconditions:
            if condition.startswith("artifact:"):
                kind = condition.split(":", 1)[1]
                artifacts = [artifact for result in results.values() for artifact in result.artifacts]
                if not any(artifact.kind == kind for artifact in artifacts):
                    return False
            elif condition != "always:true":
                return False
        return True

    def _run_step(self, plan: PlanDAG, step: PlanStep,
                  execute: Callable[[PlanStep, str], ToolRunResult],
                  verify: Callable[[PlanStep, ToolRunResult], ToolRunResult]) -> ToolRunResult:
        step.status = StepStatus.RUNNING
        strategies = step.retry_strategies or ["retry"]
        last = ToolRunResult.failed(tool_name=step.tool_name or step.id, error="실행되지 않았습니다.")
        for attempt in range(step.retry_budget + 1):
            strategy = strategies[min(attempt, len(strategies) - 1)]
            step.attempts += 1
            try:
                candidate = execute(step, strategy)
            except Exception as exc:
                candidate = ToolRunResult.failed(
                    tool_name=step.tool_name or step.id, error=f"{type(exc).__name__}: {exc}"
                )
            # The exact same verifier is mandatory after first execution and every recovery attempt.
            last = verify(step, candidate)
            signature = "" if last.succeeded else ErrorClassifier.signature(step, last)
            self.store.record_attempt(plan.plan_id, step, strategy, last, signature)
            step.observations.append({"attempt": step.attempts, "strategy": strategy,
                                      "status": last.status.value, "signature": signature})
            if last.succeeded and self._artifacts_match(step.expected_artifacts, last.artifacts):
                step.status = StepStatus.COMPLETED
                step.last_error_signature = ""
                return last
            step.last_error_signature = signature
            if signature and self.store.signature_count(plan.plan_id, step.id, signature) >= self.duplicate_failure_limit:
                break
            if ErrorClassifier.classify(last) == "permission":
                break
        step.status = StepStatus.FAILED
        return last

    @staticmethod
    def _artifacts_match(expected: List[Dict[str, Any]], actual: List[Artifact]) -> bool:
        if not expected:
            return True
        for contract in expected:
            if not any(
                (not contract.get("kind") or item.kind == contract["kind"])
                and (not contract.get("uri") or item.uri == contract["uri"])
                for item in actual
            ):
                return False
        return True

    @staticmethod
    def _block_dependants_of_failed_steps(plan: PlanDAG) -> None:
        failed = {step.id for step in plan.steps if step.status in {StepStatus.FAILED, StepStatus.BLOCKED}}
        changed = True
        while changed:
            changed = False
            for step in plan.steps:
                if step.status == StepStatus.PENDING and failed.intersection(step.dependencies):
                    step.status = StepStatus.BLOCKED
                    failed.add(step.id)
                    changed = True
