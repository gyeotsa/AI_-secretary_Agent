from typing import List, Dict, Any
from dataclasses import dataclass, field
import json
from core.llm import get_llm_client
from core.scratchpad import get_scratchpad, Scratchpad
from core.context import get_context_manager
from core.tools import get_tools_description_text
from core.plan_runtime import PlanDAG, PlanStep
from core.plugin import get_plugin_registry


@dataclass
class DecomposedTask:
    """분해된 작업 단위"""
    id: str
    description: str
    required_tools: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    priority: int = 0
    estimated_steps: int = 1
    tool_input: Dict[str, Any] = field(default_factory=dict)
    preconditions: List[str] = field(default_factory=list)
    expected_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    verification: Dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    approval_reason: str = ""
    retry_budget: int = 2
    retry_strategies: List[str] = field(default_factory=lambda: ["retry", "replan"])


class Planner:
    """
    사용자의 요청을 작은 작업으로 분해하는 Planner 클래스
    - LLM을 사용해 목표를 분해
    - Scratchpad에 작업 저장
    - Context Manager로 통합 컨텍스트 사용
    """

    def __init__(self):
        self.llm = get_llm_client("planning")
        self.scratchpad = get_scratchpad()
        self.context_manager = get_context_manager()

    def should_replan(self, consecutive_failures: int = 0, context: str = "") -> bool:
        """
        재계획이 필요한지 판단합니다.
        
        Args:
            consecutive_failures: 연속된 실패 횟수
            context: 현재 컨텍스트
            
        Returns:
            재계획 필요 여부
        """
        # 1. 연속 실패가 3회 이상이면 재계획
        if consecutive_failures >= 3:
            print(f"[Planner] 연속 실패 {consecutive_failures}회로 재계획 필요!")
            return True
            
        # 2. 대기 중인 Task가 없는데 목표가 달성되지 않았으면 재계획
        pending_tasks = self.scratchpad.get_pending_tasks()
        if not pending_tasks:
            print(f"[Planner] 대기 Task 없으나 목표 미달성으로 재계획 필요!")
            return True
            
        return False
        
    def decompose_goal(self, goal: str, context: str = "",
                       allowed_tool_names: List[str] | None = None) -> List[DecomposedTask]:
        """
        사용자의 목표를 작업으로 분해합니다.

        Args:
            goal: 사용자의 원래 요청
            context: 추가 컨텍스트 (대화 기록, RAG 결과 등)

        Returns:
            분해된 작업 리스트
        """
        # Scratchpad 초기화 및 목표 설정
        self.scratchpad.reset()
        self.scratchpad.set_goal(goal)

        # LLM에 전달할 시스템 프롬프트
        system_prompt = """당신은 AI 어시스턴트의 Planner입니다. 사용자의 요청을 작고 실행 가능한 작업으로 분해해야 합니다.

규칙:
1. 각 작업은 하나의 Tool만 사용하거나, 간단한 텍스트 답변으로 완료될 수 있어야 합니다.
2. 작업은 순서대로 실행될 수 있도록 의존성을 가져야 합니다. (예: "엑셀 생성" → "데이터 입력" → "저장")
3. 각 작업에 필요한 Tool을 명시하세요. (없으면 빈 리스트)
4. 작업은 한국어로 작성하세요.

응답 형식 (JSON만 반환하세요!):
{
    "tasks": [
        {
            "id": "task_1",
            "description": "첫 번째 작업 설명",
            "required_tools": ["tool_name_1", "tool_name_2"],
            "dependencies": [],
            "priority": 1,
            "estimated_steps": 1
            ,"tool_input": {},
            "preconditions": [],
            "expected_artifacts": [{"kind": "file", "uri": "예상 경로"}],
            "verification": {"method": "tool_verifier", "success_condition": "검증 조건"},
            "requires_approval": false,
            "approval_reason": "",
            "retry_budget": 2,
            "retry_strategies": ["retry", "replan"]
        },
        {
            "id": "task_2",
            "description": "두 번째 작업 설명",
            "required_tools": ["tool_name_3"],
            "dependencies": ["task_1"],
            "priority": 2,
            "estimated_steps": 1
        }
    ]
}

사용 가능한 Tool 목록 (실제 등록된 도구 기준, 이 목록에 없는 이름은 사용하지 마세요):
__TOOLS_TEXT__
"""
        # get_tools_schema()가 유일한 진실 공급원입니다.
        # (Executor.select_tool, ReActAgent도 동일한 목록을 참조합니다)
        # 주의: system_prompt에 JSON 예시의 리터럴 중괄호가 섞여 있으므로 .format()이 아니라
        # 단순 문자열 치환을 사용합니다 (.format()을 쓰면 그 중괄호들 때문에 KeyError가 납니다).
        system_prompt = system_prompt.replace(
            "__TOOLS_TEXT__", get_tools_description_text(include=allowed_tool_names)
        )

        # 사용자 프롬프트
        user_prompt = f"사용자 요청: {goal}\n\n"
        if context:
            user_prompt += f"추가 컨텍스트:\n{context}\n\n"
        user_prompt += "위 요청을 작업으로 분해해주세요."

        # LLM 호출
        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])
            if not response or response.lstrip().casefold().startswith(("오류:", "오류가 발생했습니다:", "error:")):
                raise RuntimeError(response or "Planner가 빈 응답을 반환했습니다.")

            # JSON 파싱
            # 응답에서 ```json ... ``` 부분 추출 (있으면)
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                response = response.split("```")[1].strip()

            result = json.loads(response)
            tasks_data = result.get("tasks", [])

            # DecomposedTask로 변환하고 Scratchpad에 저장
            decomposed_tasks = []
            for task_data in tasks_data:
                task = DecomposedTask(
                    id=task_data["id"],
                    description=task_data["description"],
                    required_tools=task_data.get("required_tools", []),
                    dependencies=task_data.get("dependencies", []),
                    priority=task_data.get("priority", 0),
                    estimated_steps=task_data.get("estimated_steps", 1)
                    ,tool_input=task_data.get("tool_input", {}),
                    preconditions=task_data.get("preconditions", []),
                    expected_artifacts=task_data.get("expected_artifacts", []),
                    verification=task_data.get("verification", {}),
                    requires_approval=bool(task_data.get("requires_approval", False)),
                    approval_reason=task_data.get("approval_reason", ""),
                    retry_budget=int(task_data.get("retry_budget", 2)),
                    retry_strategies=task_data.get("retry_strategies", ["retry", "replan"]),
                )
                decomposed_tasks.append(task)
                # Scratchpad에 Task로 추가 (기존 Task 클래스 사용)
                self.scratchpad.add_task(task.description, task.priority)

            print(f"[Planner] 목표를 {len(decomposed_tasks)}개의 작업으로 분해했습니다!")
            return decomposed_tasks

        except Exception as e:
            print(f"[Planner] 작업 분해 오류: {e}")
            import traceback
            traceback.print_exc()
            # 오류시 기본 작업 하나 반환
            fallback_task = DecomposedTask(
                id="task_1",
                description=f"사용자 요청 처리: {goal}",
                required_tools=[],
                dependencies=[],
                priority=1,
                estimated_steps=1
            )
            self.scratchpad.add_task(fallback_task.description, 1)
            return [fallback_task]

    def build_plan_dag(self, goal: str, context: str = "",
                       allowed_tool_names: List[str] | None = None) -> PlanDAG:
        """Create and validate the executable contract used by the coordinator."""
        tasks = self.decompose_goal(goal, context, allowed_tool_names)
        steps = []
        for task in tasks:
            tool_name = task.required_tools[0] if task.required_tools else ""
            capability = get_plugin_registry().get_capability(tool_name) if tool_name else None
            permission_checkpoint = bool(capability and (
                capability.side_effect == "external_send"
                or any(permission in {"filesystem_delete", "git_push", "mail_send"}
                       for permission in capability.required_permissions)
            ))
            requires_approval = task.requires_approval or permission_checkpoint
            approval_reason = task.approval_reason
            if requires_approval and not approval_reason:
                approval_reason = f"{tool_name} 단계가 외부 전송 또는 되돌리기 어려운 변경을 수행합니다."
            steps.append(PlanStep(
                id=task.id, description=task.description, tool_name=tool_name,
                tool_input=dict(task.tool_input), dependencies=list(task.dependencies),
                preconditions=list(task.preconditions),
                expected_artifacts=list(task.expected_artifacts),
                verification=dict(task.verification),
                requires_approval=requires_approval,
                approval_reason=approval_reason,
                retry_budget=task.retry_budget,
                retry_strategies=list(task.retry_strategies),
            ))
        return PlanDAG(goal=goal, steps=steps)

    def replan_from_observation(
        self, current: PlanDAG, failed_step: PlanStep, observation: str,
        context: str = "", allowed_tool_names: List[str] | None = None,
    ) -> PlanDAG:
        """Create a new revision grounded in the concrete failed observation."""
        replan_context = (
            f"{context}\n\n실패 단계: {failed_step.id} - {failed_step.description}\n"
            f"실제 관찰 결과: {observation[:2000]}\n"
            "같은 실패를 반복하지 말고 완료된 단계는 다시 계획하지 마세요."
        )
        replacement = self.build_plan_dag(current.goal, replan_context, allowed_tool_names)
        completed = [step for step in current.steps if step.status.value == "completed"]
        completed_descriptions = {step.description for step in completed}
        candidates = [step for step in replacement.steps if step.description not in completed_descriptions]
        revision = current.revision + 1
        rename = {step.id: f"r{revision}_{step.id}" for step in candidates}
        for step in candidates:
            original_id = step.id
            step.id = rename[original_id]
            step.dependencies = [rename.get(dep, dep) for dep in step.dependencies]
        return PlanDAG(
            goal=current.goal, steps=completed + candidates,
            plan_id=current.plan_id, revision=revision,
        )


# Singleton instance
_planner = None


def get_planner() -> Planner:
    """Planner 싱글톤 인스턴스 반환"""
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner
