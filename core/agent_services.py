"""Responsibility-focused services behind the compatibility Executor facade."""

from __future__ import annotations

from typing import List, Optional

from core.plan_runtime import PlanDAG


class PlanningService:
    """Owns validated plan creation."""

    def __init__(self, planner):
        self.planner = planner

    def create(self, goal: str, context: str, allowed_tools: Optional[List[str]]) -> PlanDAG:
        return self.planner.build_plan_dag(goal, context, allowed_tools)


class ResponseComposer:
    """Owns terminal status and user-facing completion summaries."""

    @staticmethod
    def terminal(
        *, response: str, cancelled: bool, terminal_error: Optional[str],
        completed_steps: int, failed_steps: int, retry_count: int,
    ) -> tuple[str, str]:
        if cancelled:
            return "cancelled", terminal_error or "사용자 요청으로 작업을 취소했습니다."
        if terminal_error:
            if completed_steps:
                return (
                    "partial",
                    f"일부 작업만 완료했습니다({completed_steps}단계 완료, "
                    f"{failed_steps or 1}단계 실패). {terminal_error}",
                )
            prefix = f"재시도 {retry_count}회 후 " if retry_count else ""
            return "failed", f"{prefix}{terminal_error}".strip()
        if retry_count:
            return "completed", f"재시도 {retry_count}회 후 완료했습니다.\n{response}"
        return "completed", response
