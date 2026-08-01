"""Bounded recovery adapter backed by the executable Plan runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from core.plan_runtime import PlanCoordinator, PlanDAG, PlanStep
from core.scratchpad import Task
from core.tools import get_tool_executor
from core.tool_result import ToolRunResult


@dataclass
class RecoveryResult:
    success: bool
    result: Optional[ToolRunResult] = None
    message: str = ""
    retry_count: int = 0
    strategy: str = ""
    error_signature: str = ""
    repeated_failure_blocked: bool = False


class RecoveryManager:
    """Uses persisted signatures and never invents semantically unrelated tools."""

    def __init__(self):
        self.tool_executor = get_tool_executor()
        self.max_retries = 2
        self.coordinator = PlanCoordinator(duplicate_failure_limit=2)

    def recover(
        self, task: Task, tool_name: str, tool_input: Dict[str, Any],
        last_result: str, retry_count: int = 0,
        verify_callback: Optional[Callable[[ToolRunResult], ToolRunResult]] = None,
    ) -> RecoveryResult:
        remaining = max(0, self.max_retries - retry_count)
        if remaining <= 0:
            return RecoveryResult(
                False, message="재시도 예산을 모두 사용했습니다.", retry_count=retry_count
            )
        step = PlanStep(
            id=f"recovery-{getattr(task, 'id', 'task')}",
            description=f"{task.description} 복구", tool_name=tool_name,
            tool_input=dict(tool_input), retry_budget=remaining - 1,
            retry_strategies=["retry", "retry_after_observation"],
        )
        plan = PlanDAG(goal=task.description, steps=[step])

        def execute(current: PlanStep, strategy: str) -> ToolRunResult:
            return self.tool_executor.execute_tool(current.tool_name, dict(current.tool_input))

        def verify(current: PlanStep, candidate: ToolRunResult) -> ToolRunResult:
            return verify_callback(candidate) if verify_callback else candidate

        outcome = self.coordinator.run(plan, execute, verify)
        total = retry_count + step.attempts
        result = outcome.results.get(step.id)
        if outcome.status == "completed" and result is not None:
            return RecoveryResult(
                True, result=result, message=f"재시도 {step.attempts}회 후 복구했습니다.",
                retry_count=total,
                strategy=step.observations[-1]["strategy"] if step.observations else "retry",
            )
        signature = step.last_error_signature
        repeated = bool(signature and step.attempts < remaining)
        return RecoveryResult(
            False, result=result,
            message=("동일한 실패가 반복되어 추가 실행을 차단했습니다."
                     if repeated else "재시도 예산 안에서 복구하지 못했습니다."),
            retry_count=total,
            strategy=step.observations[-1]["strategy"] if step.observations else "retry",
            error_signature=signature, repeated_failure_blocked=repeated,
        )


_recovery_manager: Optional[RecoveryManager] = None


def get_recovery_manager() -> RecoveryManager:
    global _recovery_manager
    if _recovery_manager is None:
        _recovery_manager = RecoveryManager()
    return _recovery_manager
