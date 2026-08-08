"""Inference-effort policy based on structured runtime facts."""
from __future__ import annotations

from dataclasses import dataclass
from core.intent_router import IntentResolution


@dataclass(frozen=True)
class ReasoningDecision:
    level: str
    use_planner: bool
    max_iterations: int
    reason: str


class ReasoningPolicy:
    def decide(self, resolution: IntentResolution, *, required_tool_count: int = 0,
               has_pending_task: bool = False, risky: bool = False) -> ReasoningDecision:
        if risky:
            return ReasoningDecision("high", True, 20, "위험하거나 되돌리기 어려운 작업")
        if has_pending_task:
            return ReasoningDecision("medium", False, 8, "대기 작업 재개")
        if resolution.matched and resolution.ready and required_tool_count <= 1:
            return ReasoningDecision("low", False, 4, "단일 Registry intent")
        if resolution.request_type == "conversation" and required_tool_count == 0:
            return ReasoningDecision("minimal", False, 1, "일반 대화")
        if required_tool_count > 1 or resolution.ambiguous:
            return ReasoningDecision("high", True, 20, "복합 또는 모호한 작업")
        return ReasoningDecision("medium", True, 10, "구조화되지 않은 실행 요청")
