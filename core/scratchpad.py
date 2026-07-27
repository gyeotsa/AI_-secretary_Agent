from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Task:
    """작업 단위를 표현하는 클래스"""
    id: str
    description: str
    status: str = "pending"  # pending, in_progress, completed, failed
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None


@dataclass
class Observation:
    """Tool 실행 결과를 표현하는 클래스"""
    tool_name: str
    input_data: Dict[str, Any]
    result: str
    success: bool
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Failure:
    """실패 이력을 표현하는 클래스"""
    task_id: str
    reason: str
    timestamp: datetime = field(default_factory=datetime.now)


class Scratchpad:
    """
    작업 메모리(Working Memory)로, 현재 진행 중인 작업의 상태를 저장합니다.
    - 목표
    - 분해된 작업 목록
    - 현재 진행 중인 작업
    - Tool 실행 결과
    - 실패 이력
    - 다음 행동
    """

    def __init__(self):
        self.goal: str = ""
        self.tasks: List[Task] = []
        self.current_task: Optional[Task] = None
        self.observations: List[Observation] = []
        self.failures: List[Failure] = []
        self.next_action: Optional[str] = None
        self.created_at: datetime = datetime.now()

    def set_goal(self, goal: str):
        """사용자의 목표를 설정합니다"""
        self.goal = goal

    def add_task(self, task_description: str, priority: int = 0) -> str:
        """새 작업을 추가하고 task ID를 반환합니다"""
        task_id = f"task_{len(self.tasks) + 1}"
        task = Task(
            id=task_id,
            description=task_description,
            priority=priority
        )
        self.tasks.append(task)
        return task_id

    def set_current_task(self, task_id: str) -> bool:
        """현재 진행 중인 작업을 설정합니다"""
        for task in self.tasks:
            if task.id == task_id:
                task.status = "in_progress"
                self.current_task = task
                return True
        return False

    def complete_task(self, task_id: str) -> bool:
        """작업을 완료 상태로 표시합니다"""
        for task in self.tasks:
            if task.id == task_id:
                task.status = "completed"
                task.completed_at = datetime.now()
                if self.current_task and self.current_task.id == task_id:
                    self.current_task = None
                return True
        return False

    def fail_task(self, task_id: str, reason: str) -> bool:
        """작업을 실패 상태로 표시하고 실패 이력을 추가합니다"""
        for task in self.tasks:
            if task.id == task_id:
                task.status = "failed"
                self.failures.append(Failure(task_id=task_id, reason=reason))
                if self.current_task and self.current_task.id == task_id:
                    self.current_task = None
                return True
        return False

    def add_observation(self, tool_name: str, input_data: Dict[str, Any], result: str, success: bool):
        """Tool 실행 결과를 추가합니다"""
        self.observations.append(Observation(
            tool_name=tool_name,
            input_data=input_data,
            result=str(result),
            success=success
        ))

    def set_next_action(self, action: str):
        """다음 행동을 설정합니다"""
        self.next_action = action

    def get_completed_tasks(self) -> List[Task]:
        """완료된 작업 목록을 반환합니다"""
        return [task for task in self.tasks if task.status == "completed"]

    def get_pending_tasks(self) -> List[Task]:
        """대기 중인 작업 목록을 반환합니다"""
        return [task for task in self.tasks if task.status == "pending"]

    def reset(self):
        """스크래치패드를 초기화합니다"""
        self.goal = ""
        self.tasks = []
        self.current_task = None
        self.observations = []
        self.failures = []
        self.next_action = None

    def get_context(self) -> str:
        """
        스크래치패드의 내용을 LLM에 입력할 수 있는 텍스트 형식으로 반환합니다.
        Planner나 Executor에서 사용합니다.
        """
        context_lines = []
        context_lines.append(f"[목표] {self.goal}")
        context_lines.append("")

        # 작업 목록
        if self.tasks:
            context_lines.append("[작업 목록]")
            for task in self.tasks:
                status_icon = {
                    "pending": "⏳",
                    "in_progress": "🔄",
                    "completed": "✅",
                    "failed": "❌"
                }.get(task.status, "⏳")
                context_lines.append(f"{status_icon} {task.id}: {task.description} (우선순위: {task.priority})")
            context_lines.append("")

        # 현재 작업
        if self.current_task:
            context_lines.append(f"[현재 작업] {self.current_task.id}: {self.current_task.description}")
            context_lines.append("")

        # 최근 관찰 결과
        if self.observations:
            context_lines.append("[최근 관찰 결과]")
            for obs in self.observations[-3:]:  # 최근 3개만
                status = "✅ 성공" if obs.success else "❌ 실패"
                context_lines.append(f"- {obs.tool_name}: {status}")
                context_lines.append(f"  결과: {obs.result[:100]}...")
            context_lines.append("")

        # 실패 이력
        if self.failures:
            context_lines.append("[실패 이력]")
            for failure in self.failures[-3:]:  # 최근 3개만
                context_lines.append(f"- Task {failure.task_id}: {failure.reason}")
            context_lines.append("")

        # 다음 행동
        if self.next_action:
            context_lines.append(f"[다음 행동] {self.next_action}")

        return "\n".join(context_lines)


# Singleton instance
_scratchpad = None


def get_scratchpad() -> Scratchpad:
    """Scratchpad 싱글톤 인스턴스를 반환합니다"""
    global _scratchpad
    if _scratchpad is None:
        _scratchpad = Scratchpad()
    return _scratchpad
