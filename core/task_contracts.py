"""Persistent task contracts and supervisor transitions for specialist execution.

The contract is the single, inspectable agreement between planning, a specialist,
tool execution, verification and the UI.  It deliberately stores evidence and
artifacts separately so an assistant cannot claim success from prose alone.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from contextlib import contextmanager, nullcontext
import json
import sqlite3
import threading
import time
import uuid

from core.runtime.event_bus import Event, get_event_bus


class ContractStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AcceptanceCriterion:
    key: str
    description: str
    kind: str = "evidence"
    required: bool = True


@dataclass(frozen=True)
class ResourceBudget:
    timeout_seconds: float = 120.0
    vram_mb: int = 0
    ram_mb: int = 1024
    max_parallel: int = 1


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    strategies: tuple[str, ...] = ("retry", "replan")
    escalate_after: int = 2


@dataclass
class TaskContract:
    contract_id: str
    goal: str
    specialist: str
    input_contract: dict[str, Any]
    output_contract: dict[str, Any]
    acceptance_criteria: list[AcceptanceCriterion]
    allowed_tools: list[str]
    required_permissions: list[str]
    resource_budget: ResourceBudget
    retry_policy: RetryPolicy
    escalation_target: str = "user"
    parent_id: str = ""
    plan_id: str = ""
    status: ContractStatus = ContractStatus.QUEUED
    attempts: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str = ""
    cancel_requested: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = ContractStatus(self.status)
        if not self.goal.strip() or not self.specialist.strip():
            raise ValueError("작업 계약에는 목표와 담당 전문가가 필요합니다.")
        if self.resource_budget.timeout_seconds <= 0:
            raise ValueError("작업 계약 timeout은 0보다 커야 합니다.")
        if self.retry_policy.max_attempts < 1:
            raise ValueError("작업 계약은 최소 1회 실행을 허용해야 합니다.")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskContract":
        payload = dict(value)
        payload["acceptance_criteria"] = [
            item if isinstance(item, AcceptanceCriterion) else AcceptanceCriterion(**item)
            for item in payload.get("acceptance_criteria", [])
        ]
        if not isinstance(payload.get("resource_budget"), ResourceBudget):
            payload["resource_budget"] = ResourceBudget(**payload.get("resource_budget", {}))
        if not isinstance(payload.get("retry_policy"), RetryPolicy):
            retry = dict(payload.get("retry_policy", {}))
            retry["strategies"] = tuple(retry.get("strategies", ("retry", "replan")))
            payload["retry_policy"] = RetryPolicy(**retry)
        return cls(**payload)


class TaskContractStore:
    def __init__(self, db_path: str = "data/task_contracts.db"):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS task_contracts(
                contract_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                specialist TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contract_updated ON task_contracts(updated_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contract_status ON task_contracts(status,updated_at DESC)")

    def save(self, contract: TaskContract) -> TaskContract:
        contract.updated_at = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(contract.to_dict(), ensure_ascii=False, default=str)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("""INSERT INTO task_contracts
                (contract_id,parent_id,plan_id,specialist,goal,status,payload,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(contract_id) DO UPDATE SET
                status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at""",
                (contract.contract_id, contract.parent_id, contract.plan_id, contract.specialist,
                 contract.goal, contract.status.value, payload, contract.updated_at))
        return contract

    def get(self, contract_id: str) -> Optional[TaskContract]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT payload FROM task_contracts WHERE contract_id=?", (contract_id,)).fetchone()
        return TaskContract.from_dict(json.loads(row[0])) if row else None

    def list_recent(self, limit: int = 50, statuses: Iterable[str] = ()) -> list[TaskContract]:
        values = tuple(item.value if isinstance(item, ContractStatus) else str(item) for item in statuses)
        sql = "SELECT payload FROM task_contracts"
        params: list[Any] = []
        if values:
            sql += f" WHERE status IN ({','.join('?' for _ in values)})"
            params.extend(values)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [TaskContract.from_dict(json.loads(row[0])) for row in rows]

    def find_for_step(self, parent_id: str, plan_id: str,
                      step_id: str) -> Optional[TaskContract]:
        """Return the newest persisted contract for one concrete plan node.

        A plan can be paused for approval and resumed in a later chat turn.  The
        step id therefore belongs in the persisted input contract instead of in
        an in-memory mapping only.
        """
        if not parent_id or not plan_id or not step_id:
            return None
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT payload FROM task_contracts WHERE parent_id=? AND plan_id=? "
                "ORDER BY updated_at DESC LIMIT 100", (parent_id, plan_id),
            ).fetchall()
        for row in rows:
            contract = TaskContract.from_dict(json.loads(row[0]))
            if str(contract.input_contract.get("step_id", "")) == str(step_id):
                return contract
        return None


class SupervisorRuntime:
    """Owns contract lifecycle and evidence-based completion decisions."""

    _TERMINAL = {ContractStatus.COMPLETED, ContractStatus.FAILED,
                 ContractStatus.ESCALATED, ContractStatus.CANCELLED}

    def __init__(self, store: TaskContractStore | None = None):
        self.store = store or TaskContractStore()
        self.bus = get_event_bus()
        self._resource_lock = threading.RLock()
        self._parallel_slots: dict[tuple[str, int], threading.BoundedSemaphore] = {}

    def create_contract(
        self, *, goal: str, specialist: str, input_contract: dict[str, Any] | None = None,
        output_contract: dict[str, Any] | None = None,
        acceptance_criteria: Iterable[AcceptanceCriterion] = (), allowed_tools: Iterable[str] = (),
        required_permissions: Iterable[str] = (), resource_budget: ResourceBudget | None = None,
        retry_policy: RetryPolicy | None = None, escalation_target: str = "user",
        parent_id: str = "", plan_id: str = "",
    ) -> TaskContract:
        contract = TaskContract(
            contract_id=uuid.uuid4().hex[:12], goal=goal, specialist=specialist,
            input_contract=dict(input_contract or {}), output_contract=dict(output_contract or {}),
            acceptance_criteria=list(acceptance_criteria), allowed_tools=list(dict.fromkeys(allowed_tools)),
            required_permissions=list(dict.fromkeys(required_permissions)),
            resource_budget=resource_budget or ResourceBudget(), retry_policy=retry_policy or RetryPolicy(),
            escalation_target=escalation_target, parent_id=parent_id, plan_id=plan_id,
        )
        self.store.save(contract)
        self._publish(contract, "task_contract.created")
        return contract

    def transition(self, contract: TaskContract, status: ContractStatus | str,
                   *, failure_reason: str = "") -> TaskContract:
        next_status = ContractStatus(status)
        if contract.status in self._TERMINAL and next_status != contract.status:
            raise ValueError(f"종료된 작업 계약은 변경할 수 없습니다: {contract.contract_id}")
        contract.status = next_status
        if failure_reason:
            contract.failure_reason = str(failure_reason)[:2000]
        self.store.save(contract)
        self._publish(contract, f"task_contract.{next_status.value}")
        return contract

    def begin_attempt(self, contract: TaskContract) -> TaskContract:
        if contract.status == ContractStatus.FAILED and contract.attempts < contract.retry_policy.max_attempts:
            contract.status = ContractStatus.QUEUED
            contract.failure_reason = ""
            self.store.save(contract)
            self._publish(contract, "task_contract.retrying")
        contract.attempts += 1
        if contract.attempts > contract.retry_policy.max_attempts:
            return self.transition(contract, ContractStatus.ESCALATED,
                                   failure_reason="재시도 예산을 초과했습니다.")
        return self.transition(contract, ContractStatus.RUNNING)

    def verify(self, contract: TaskContract, *, artifacts: Iterable[dict[str, Any]] = (),
               evidence: Iterable[dict[str, Any]] = (), failure_reason: str = "") -> TaskContract:
        self.transition(contract, ContractStatus.VERIFYING)
        contract.artifacts = [dict(item) for item in artifacts]
        contract.evidence = [dict(item) for item in evidence]
        missing: list[str] = []
        for criterion in contract.acceptance_criteria:
            if not criterion.required:
                continue
            collection = contract.artifacts if criterion.kind == "artifact" else contract.evidence
            if not any(
                criterion.key == "*"
                or str(item.get("kind") or item.get("type") or item.get("key")) == criterion.key
                for item in collection
            ):
                missing.append(criterion.description)
        if failure_reason or missing:
            reason = failure_reason or "검수 근거 누락: " + ", ".join(missing)
            if contract.attempts >= contract.retry_policy.escalate_after:
                return self.transition(contract, ContractStatus.ESCALATED, failure_reason=reason)
            return self.transition(contract, ContractStatus.FAILED, failure_reason=reason)
        return self.transition(contract, ContractStatus.COMPLETED)

    def snapshot(self, limit: int = 30) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.store.list_recent(limit)]

    def request_cancel(self, contract_id: str, *, reason: str = "사용자가 취소를 요청했습니다.") -> TaskContract:
        """Persist cancellation intent so running workers and the UI share one truth."""
        contract = self.store.get(str(contract_id))
        if contract is None:
            raise KeyError(f"작업 계약을 찾지 못했습니다: {contract_id}")
        if contract.status in self._TERMINAL:
            return contract
        contract.cancel_requested = True
        contract.failure_reason = str(reason)[:2000]
        if contract.status in {
            ContractStatus.QUEUED, ContractStatus.AWAITING_APPROVAL, ContractStatus.FAILED,
        }:
            return self.transition(contract, ContractStatus.CANCELLED, failure_reason=reason)
        self.store.save(contract)
        self._publish(contract, "task_contract.cancel_requested")
        return contract

    def cancellation_requested(self, contract: TaskContract) -> bool:
        current = self.store.get(contract.contract_id)
        if current is not None and current.cancel_requested:
            contract.cancel_requested = True
            contract.failure_reason = current.failure_reason
        return bool(contract.cancel_requested)

    @contextmanager
    def resource_guard(self, contract: TaskContract) -> Iterator[dict[str, Any]]:
        """Enforce a contract's RAM, VRAM, concurrency and wall-time admission.

        Tool-level cancellation still belongs to PluginRegistry.  This guard is
        the process-wide admission boundary: it refuses work that cannot fit,
        serializes contracts according to ``max_parallel`` and records overruns
        instead of allowing a late result to be presented as successful work.
        """
        budget = contract.resource_budget
        started = time.monotonic()
        timeout = float(budget.timeout_seconds)
        available_ram_mb: int | None = None
        try:
            import psutil
            available_ram_mb = int(psutil.virtual_memory().available / 1024 ** 2)
        except ImportError:
            pass
        if available_ram_mb is not None and int(budget.ram_mb) > available_ram_mb:
            raise MemoryError(
                f"RAM 요청 {int(budget.ram_mb)}MB가 현재 가용 {available_ram_mb}MB를 초과합니다."
            )

        slot_key = (contract.specialist, max(1, int(budget.max_parallel)))
        with self._resource_lock:
            slot = self._parallel_slots.get(slot_key)
            if slot is None:
                slot = threading.BoundedSemaphore(slot_key[1])
                self._parallel_slots[slot_key] = slot
        if not slot.acquire(timeout=timeout):
            raise TimeoutError(f"동시 실행 슬롯 대기 시간이 초과되었습니다: {contract.specialist}")

        gpu_context = nullcontext(None)
        if int(budget.vram_mb) > 0:
            from core.gpu_scheduler import get_gpu_resource_queue
            gpu_context = get_gpu_resource_queue().reserve(
                contract.specialist, int(budget.vram_mb), timeout=timeout,
            )
        try:
            with gpu_context as admission:
                waited_ms = round((time.monotonic() - started) * 1000, 2)
                data = {
                    "contract_id": contract.contract_id,
                    "specialist": contract.specialist,
                    "ram_mb": int(budget.ram_mb),
                    "vram_mb": int(budget.vram_mb),
                    "max_parallel": int(budget.max_parallel),
                    "waited_ms": waited_ms,
                    "gpu_device": getattr(admission, "device", "none"),
                }
                self.bus.publish(Event(
                    type="task_contract.resource_admitted", source="supervisor", data=data,
                ))
                yield data
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                raise TimeoutError(
                    f"작업 계약 실행 시간이 {timeout:g}초를 초과했습니다 "
                    f"(실제 {elapsed:.2f}초)."
                )
        finally:
            slot.release()

    def _publish(self, contract: TaskContract, event_type: str) -> None:
        self.bus.publish(Event(type=event_type, source="supervisor", data={
            "contract_id": contract.contract_id, "goal": contract.goal,
            "specialist": contract.specialist, "status": contract.status.value,
            "attempts": contract.attempts, "failure_reason": contract.failure_reason,
        }))


_supervisor: SupervisorRuntime | None = None


def get_supervisor_runtime() -> SupervisorRuntime:
    global _supervisor
    if _supervisor is None:
        _supervisor = SupervisorRuntime()
    return _supervisor
