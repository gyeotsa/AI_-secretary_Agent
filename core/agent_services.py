"""Responsibility-focused services behind the compatibility Executor facade."""

from __future__ import annotations

from typing import List, Optional

from core.plan_runtime import PlanDAG, PlanStep


class ConversationService:
    """Generates ordinary dialogue without exposing the Tool runtime."""

    def __init__(self, llm):
        self.llm = llm

    def respond(self, message: str, history, *, assistant_name: str = "",
                voice_name: str = "", address: str = "보스", style: str = "") -> str:
        if assistant_name and message.strip().casefold() == assistant_name.casefold():
            return f"응, 듣고 있어. {address}."
        recent = [
            {"role": item.get("role", "user"), "content": str(item.get("content", ""))}
            for item in list(history)[-6:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        persona = f"선택 음성: {voice_name}. 대화 스타일: {style}" if style else ""
        prompt = (
            f"당신은 로컬 개인 비서 '{assistant_name or '자비스'}'입니다. 지금 요청은 도구 실행이 아닌 일반 대화입니다. "
            "도구를 찾거나 실행했다고 주장하지 마세요. 최근 발화의 맥락과 감정을 먼저 반영하고 "
            "자연스럽고 간결한 한국어로 답하세요. 최신 정보가 필요하면 확인이 필요하다고 말하세요. "
            f"사용자 호칭은 '{address}'이며 답변에서 최대 한 번만 사용하세요. {persona}"
        )
        messages = [{"role": "system", "content": prompt}, *recent,
                    {"role": "user", "content": message}]
        response = str(self.llm.chat(messages) or "").strip()
        return response or f"응, 듣고 있어. 무슨 이야기부터 해볼까, {address}?"


class PlanningService:
    """Owns validated plan creation."""

    def __init__(self, planner):
        self.planner = planner

    def create(self, goal: str, context: str, allowed_tools: Optional[List[str]]) -> PlanDAG:
        if hasattr(self.planner, "build_plan_dag"):
            return self.planner.build_plan_dag(goal, context, allowed_tools)
        tasks = self.planner.decompose_goal(goal, context, allowed_tools)
        return PlanDAG(goal, [PlanStep(
            id=task.id, description=task.description,
            tool_name=(getattr(task, "required_tools", []) or [""])[0],
            dependencies=list(getattr(task, "dependencies", []) or []),
        ) for task in tasks])


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
