from typing import List, Dict, Any
from dataclasses import dataclass, field
import json
import re
from core.llm import get_llm_client
from core.scratchpad import get_scratchpad, Scratchpad
from core.context import ContextManager, get_context_manager
from core.tools import get_tools_description_text
from core.plan_runtime import PlanDAG, PlanStep
from core.plugin import get_plugin_registry


class PlanningError(RuntimeError):
    """The planner could not produce an executable, validated contract."""


def _parse_json_object(response: str) -> Dict[str, Any]:
    """Markdown 설명이 섞여도 첫 번째 유효한 JSON 객체만 안전하게 추출한다."""
    candidate = str(response or "").strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        if candidate.endswith("```"):
            candidate = candidate[:-3].strip()
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise json.JSONDecodeError("유효한 JSON 객체를 찾지 못했습니다.", candidate, 0)


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

    def __init__(
        self,
        *,
        llm=None,
        scratchpad: Scratchpad | None = None,
        context_manager: ContextManager | None = None,
    ):
        self.llm = llm or get_llm_client("planning")
        self.scratchpad = scratchpad or get_scratchpad()
        self.context_manager = context_manager or get_context_manager()

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
1. 각 작업은 정확히 하나의 Tool만 사용해야 합니다. 여러 Tool이 필요하면 작업을 여러 개로 나누세요.
2. 작업은 순서대로 실행될 수 있도록 의존성을 가져야 합니다. (예: "엑셀 생성" → "데이터 입력" → "저장")
3. 각 작업에 필요한 Tool을 명시하세요. (없으면 빈 리스트)
4. 작업은 한국어로 작성하세요.
5. 사용자가 지정한 파일 경로, 제목, 본문, 문구, 수치와 따옴표 안 원문은 해당 Tool의 tool_input에 정확히 보존하세요.
6. Tool Schema에서 선택 필드이더라도 사용자가 값을 지정했다면 절대 생략하지 마세요.
7. 문서에 제목이나 본문을 넣으라는 요청을 경로만 있는 빈 문서 생성으로 바꾸지 마세요.
8. description이나 verification에만 값을 적는 것은 실행 입력이 아닙니다. 실제 값은 반드시 tool_input에 넣으세요.

응답 형식 (JSON만 반환하세요!):
{
    "tasks": [
        {
            "id": "task_1",
            "description": "첫 번째 작업 설명",
            "required_tools": ["tool_name_1"],
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

        # A malformed plan is corrected as a plan, never disguised as a
        # tool-free task.  The old fallback made the runtime ask the general LLM
        # to improvise an execution result and was a major source of false
        # completions and unrelated tool choices.
        last_error: Exception | None = None
        correction = ""
        for attempt in range(1, 4):
            try:
                prompt = user_prompt
                if correction:
                    prompt += (
                        "\n\n이전 계획은 실행 계약 검증에 실패했습니다. 다음 오류를 모두 "
                        f"고쳐 전체 JSON을 다시 반환하세요:\n{correction}"
                    )
                response = self.llm.chat([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ])
                if not response:
                    raise PlanningError("Planner가 빈 응답을 반환했습니다.")
                result = _parse_json_object(response)
                decomposed_tasks = self._validated_tasks(
                    result.get("tasks"), allowed_tool_names,
                    original_goal=goal,
                )
                for task in decomposed_tasks:
                    self.scratchpad.add_task(task.description, task.priority)
                print(
                    f"[Planner] 목표를 {len(decomposed_tasks)}개의 검증된 작업으로 "
                    f"분해했습니다 (시도 {attempt}/3)."
                )
                return decomposed_tasks
            except Exception as exc:
                # Provider/transport failures are not repairable by sending the
                # same request two more times inside this method.
                if getattr(exc, "code", "") in {"connection", "timeout", "authentication"}:
                    raise
                last_error = exc
                correction = str(exc)[:2000]
                print(f"[Planner] 계획 검증 실패 {attempt}/3: {correction}")

        self.scratchpad.reset()
        self.scratchpad.set_goal(goal)
        raise PlanningError(
            "3회 시도 후에도 실행 가능한 계획을 만들지 못했습니다: "
            f"{last_error or '알 수 없는 계획 오류'}"
        ) from last_error

    @staticmethod
    def _validated_tasks(
        tasks_data: Any,
        allowed_tool_names: List[str] | None,
        *,
        original_goal: str = "",
    ) -> List[DecomposedTask]:
        if not isinstance(tasks_data, list) or not tasks_data:
            raise PlanningError("tasks는 비어 있지 않은 배열이어야 합니다.")
        allowed = set(allowed_tool_names) if allowed_tool_names is not None else None
        registry = get_plugin_registry()
        tasks: List[DecomposedTask] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(tasks_data):
            if not isinstance(raw, dict):
                raise PlanningError(f"tasks[{index}]는 객체여야 합니다.")
            task_id = str(raw.get("id", "")).strip()
            description = str(raw.get("description", "")).strip()
            if not task_id or task_id in seen_ids:
                raise PlanningError(f"tasks[{index}]의 id가 없거나 중복입니다: {task_id!r}")
            if not description:
                raise PlanningError(f"{task_id}의 description이 비어 있습니다.")
            seen_ids.add(task_id)
            tools = raw.get("required_tools", [])
            if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], str):
                raise PlanningError(f"{task_id}에는 정확히 하나의 required_tools가 필요합니다.")
            tool_name = tools[0].strip()
            if not tool_name or registry.get_capability(tool_name) is None:
                raise PlanningError(f"{task_id}가 등록되지 않은 Tool을 요청했습니다: {tool_name!r}")
            if allowed is not None and tool_name not in allowed:
                raise PlanningError(f"{task_id}가 현재 loadout 밖의 Tool을 요청했습니다: {tool_name}")
            dependencies = raw.get("dependencies", [])
            if not isinstance(dependencies, list) or not all(isinstance(dep, str) for dep in dependencies):
                raise PlanningError(f"{task_id}의 dependencies는 문자열 배열이어야 합니다.")
            tool_input = raw.get("tool_input", {})
            if not isinstance(tool_input, dict):
                raise PlanningError(f"{task_id}의 tool_input은 객체여야 합니다.")
            retry_budget = max(0, min(5, int(raw.get("retry_budget", 2))))
            tasks.append(DecomposedTask(
                id=task_id,
                description=description,
                required_tools=[tool_name],
                dependencies=list(dependencies),
                priority=int(raw.get("priority", 0)),
                estimated_steps=max(1, int(raw.get("estimated_steps", 1))),
                tool_input=dict(tool_input),
                preconditions=list(raw.get("preconditions", []) or []),
                expected_artifacts=list(raw.get("expected_artifacts", []) or []),
                verification=dict(raw.get("verification", {}) or {}),
                requires_approval=bool(raw.get("requires_approval", False)),
                approval_reason=str(raw.get("approval_reason", "") or ""),
                retry_budget=retry_budget,
                retry_strategies=list(raw.get("retry_strategies", ["retry", "replan"]) or []),
            ))
        unknown = sorted({dep for task in tasks for dep in task.dependencies} - seen_ids)
        if unknown:
            raise PlanningError(f"존재하지 않는 dependency가 있습니다: {unknown}")
        if any(task.id in task.dependencies for task in tasks):
            raise PlanningError("작업은 자기 자신에 의존할 수 없습니다.")
        Planner._validate_goal_grounding(tasks, original_goal)
        # PlanDAG performs the canonical cycle validation before execution.
        PlanDAG(goal="validation", steps=[PlanStep(
            id=task.id,
            description=task.description,
            tool_name=task.required_tools[0],
            dependencies=list(task.dependencies),
        ) for task in tasks])
        return tasks

    @staticmethod
    def _quoted_literals(text: str) -> List[str]:
        """Return concrete values explicitly quoted by the user."""
        source = str(text or "")
        values: List[str] = []
        for pattern in (
            r'"([^"\r\n]+)"', r"'([^'\r\n]+)'",
            r"“([^”\r\n]+)”", r"‘([^’\r\n]+)’",
        ):
            values.extend(match.strip() for match in re.findall(pattern, source) if match.strip())
        return list(dict.fromkeys(values))

    @staticmethod
    def _validate_goal_grounding(tasks: List[DecomposedTask], original_goal: str) -> None:
        """Reject executable plans that silently drop concrete user content.

        The invariant is enforced at the shared Planner boundary rather than in
        one Word-specific branch, so every current and future tool is protected.
        """
        goal = str(original_goal or "").strip()
        if not goal:
            return
        serialized_inputs = json.dumps(
            [task.tool_input for task in tasks], ensure_ascii=False, default=str,
        )
        missing = [value for value in Planner._quoted_literals(goal)
                   if value not in serialized_inputs]
        if missing:
            raise PlanningError(
                "사용자가 지정한 원문이 tool_input에서 누락되었습니다: "
                + ", ".join(repr(value) for value in missing)
            )

        compact = re.sub(r"\s+", "", goal.casefold())
        blank_requested = bool(re.search(r"(?:빈문서|빈파일|공백|내용없이)", compact))
        content_requested = bool(re.search(
            r"(?:제목|본문|문구|내용)(?:은|는|을|를|:|으로|이라고|라는)", compact
        ))
        if blank_requested or not content_requested:
            return
        document_fields = {
            "word_create_document": ("title", "paragraphs"),
            "hwpx_create_document": ("title", "paragraphs"),
            "pdf_create_document": ("title", "paragraphs"),
            "powerpoint_create_presentation": ("title", "slides"),
        }
        for task in tasks:
            tool_name = task.required_tools[0] if task.required_tools else ""
            fields = document_fields.get(tool_name)
            if fields and not any(
                task.tool_input.get(field) not in (None, "", []) for field in fields
            ):
                raise PlanningError(
                    f"{tool_name}가 요청된 제목·본문·문구를 tool_input에 담지 않았습니다."
                )

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
