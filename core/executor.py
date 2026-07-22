from typing import List, Optional, Dict, Any, Callable
from dataclasses import dataclass
import json
import re
import threading

from core.llm import get_llm_client
from core.scratchpad import get_scratchpad, Task
from core.planner import get_planner
from core.tools import get_tool_executor, get_tools_description_text, get_tool_names
from core.reflection import get_reflection
from core.context import get_context_manager
from core.permission import get_permission_manager, TOOL_PERMISSION_MAP
from core.memory import get_memory, build_memory_context
from core.workspace import get_workspace_manager
from core.verifier import get_tool_verifier
from core.recovery import get_recovery_manager
from core.conversation_context import ConversationContextResolver
from core.dialogue_state import get_dialogue_state_store
from core.intent_router import IntentRouter, IntentResolution


@dataclass
class ExecutionOutcome:
    response: str
    status: str = "completed"
    goal: str = ""
    question: str = ""
    task_id: str = ""
    next_goal: str = ""


class Executor:
    """
    진정한 Agent Runtime!
    Jarvis의 중앙 실행 엔진으로, Planner와 협력하고, Context를 조립하고,
    적절한 Tool을 선택하고, 권한을 확인하고, 실행 결과를 검증하고,
    실패를 복구하고, Memory와 Scratchpad를 갱신하며,
    목표 달성 여부를 계속 판단합니다!
    """

    def __init__(self):
        self.llm = get_llm_client()
        self.reasoning_llm = get_llm_client("reasoning")
        self.scratchpad = get_scratchpad()
        self.planner = get_planner()
        self.tool_executor = get_tool_executor()
        self.reflection = get_reflection()
        self.context_manager = get_context_manager()
        self.permission_manager = get_permission_manager()
        self.memory = get_memory()
        self.workspace_manager = get_workspace_manager()
        self.verifier = get_tool_verifier()
        self.recovery_manager = get_recovery_manager()
        self.context_resolver = ConversationContextResolver(self.llm)

        # decide_next_action()에서 잠깐 system_prompt를 바꿔 쓰고 나서 복원하기 위한 원본 보관
        # (generate_response() 등 다른 메서드가 Jarvis 페르소나 프롬프트를 계속 쓸 수 있어야 함)
        self._default_system_prompt = self.llm.system_prompt
        self._default_reasoning_prompt = self.reasoning_llm.system_prompt

        self.goal = ""
        self.session_id = ""
        self.max_iterations = 20  # 무한 루프 방지
        self.current_iteration = 0
        self._retry_count = 0  # 복구 시 재시도 횟수 추적
        self._consecutive_failures = 0  # 연속 실패 횟수 추적
        self._total_failures = 0
        self.terminal_error = None
        self.dialogue_state = get_dialogue_state_store()
        self.intent_router = IntentRouter(self.tool_executor.plugin_registry)
        self._progress_callback: Optional[Callable[[str], None]] = None
        self.current_agent_task_id = ""
        self._task_controls: Dict[str, Dict[str, bool]] = {}
        self._control_condition = threading.Condition()

    def initialize(self, goal: str, session_id: Optional[str] = None):
        """초기화: Goal 설정, Scratchpad 초기화, Context 빌드"""
        self.goal = goal
        self.session_id = session_id or ""
        self.current_iteration = 0
        self._retry_count = 0
        self._consecutive_failures = 0
        self._total_failures = 0
        self.terminal_error = None
        self.scratchpad.reset()
        self.scratchpad.set_goal(goal)
        print(f"[Executor] 초기화 완료: Goal='{goal}'")

    def execute_goal(self, goal: str, session_id: Optional[str] = None,
                     conversation_history: Optional[List[Dict[str, str]]] = None,
                     progress_callback: Optional[Callable[[str], None]] = None,
                     existing_task_id: Optional[str] = None) -> str:
        """하위 호환용 문자열 응답 API."""
        return self.execute_turn(goal, session_id, conversation_history, progress_callback, existing_task_id).response

    def execute_turn(self, goal: str, session_id: Optional[str] = None,
                     conversation_history: Optional[List[Dict[str, str]]] = None,
                     progress_callback: Optional[Callable[[str], None]] = None,
                     existing_task_id: Optional[str] = None) -> ExecutionOutcome:
        """질문 대기와 재개를 지원하는 한 번의 대화 턴을 실행한다."""
        session_key = session_id or "default"
        history = list(conversation_history or [])
        normalized = goal.strip().lower()

        if self.is_control_command(goal):
            return self.handle_control_command(goal, session_key)

        if normalized in {"대기 작업", "대기 작업 목록"}:
            items = self.dialogue_state.list(session_key)
            if not items:
                return ExecutionOutcome("현재 답변을 기다리는 작업이 없습니다, 보스.", "completed")
            lines = [f"- {item.task_id}: {item.original_goal} (질문: {item.question})" for item in items]
            return ExecutionOutcome("답변을 기다리는 작업입니다, 보스.\n" + "\n".join(lines), "completed")

        resume_match = re.match(r"(?:작업\s*)?([0-9a-f]{8})\s*재개\s*[:：]?\s*(.+)", goal.strip(), re.I | re.S)
        selected_task_id = resume_match.group(1).lower() if resume_match else None
        supplied_answer = resume_match.group(2).strip() if resume_match else goal
        is_new_request = normalized.startswith(("새 작업:", "새 작업："))
        if is_new_request:
            goal = re.sub(r"^새 작업\s*[:：]\s*", "", goal, flags=re.I)
        pending = None if is_new_request else self.dialogue_state.get(session_key, selected_task_id)
        agent_task_id = pending.task_id if pending else (existing_task_id or "")
        intent_resolution = IntentResolution()

        if pending:
            if supplied_answer.strip().lower() in {"취소", "그만", "중단", "cancel", "stop"}:
                self.dialogue_state.delete(session_key, pending.task_id)
                self.dialogue_state.update_task(pending.task_id, status="cancelled", result="사용자 취소")
                return ExecutionOutcome(f"진행 중인 작업 {pending.task_id}을 취소했습니다, 보스.", "cancelled")
            intent_state = self.dialogue_state.get_intent_state(pending.task_id)
            if not intent_state:
                legacy_resolution = self.intent_router.resolve(pending.original_goal)
                if legacy_resolution.matched and legacy_resolution.tool_name:
                    intent_state = {
                        "session_id": session_key,
                        "intent_name": legacy_resolution.intent_name,
                        "slots": legacy_resolution.slots,
                        "original_request": pending.original_goal,
                    }
                    self.dialogue_state.save_intent_state(
                        pending.task_id, session_key, legacy_resolution.intent_name,
                        legacy_resolution.slots, pending.original_goal,
                    )
            if intent_state:
                intent_resolution = self.intent_router.resolve(
                    supplied_answer, intent_state["intent_name"], intent_state["slots"]
                )
                self.dialogue_state.delete(session_key, pending.task_id)
                if intent_resolution.question:
                    self.dialogue_state.save_intent_state(
                        pending.task_id, session_key, intent_resolution.intent_name,
                        intent_resolution.slots, intent_state["original_request"],
                    )
                    next_pending = self.dialogue_state.create(
                        session_key, intent_state["original_request"], intent_resolution.question,
                        pending.conversation_history, pending.task_id,
                    )
                    return ExecutionOutcome(
                        f"{intent_resolution.question}\n대기 작업 ID: {next_pending.task_id}",
                        "awaiting_user", intent_state["original_request"],
                        intent_resolution.question, next_pending.task_id,
                    )
                goal = intent_state["original_request"]
            history = pending.conversation_history + [
                {"role": "assistant", "content": pending.question},
                {"role": "user", "content": supplied_answer},
            ]
            if not intent_state:
                goal = (
                    f"원래 요청: {pending.original_goal}\n"
                    f"자비스의 확인 질문: {pending.question}\n"
                    f"사용자가 추가로 제공한 정보: {supplied_answer}\n"
                    "위 정보를 반영해 원래 요청을 이어서 완료하세요."
                )
                self.dialogue_state.delete(session_key, pending.task_id)
            if progress_callback:
                progress_callback(f"확인했습니다, 보스. 작업 {pending.task_id}을 이어서 진행하겠습니다.")

        if not pending:
            intent_resolution = self.intent_router.resolve(goal)
            if intent_resolution.capability_response:
                return ExecutionOutcome(intent_resolution.capability_response, "completed", goal)
            if intent_resolution.matched and intent_resolution.question:
                task = self.dialogue_state.create_task(session_key, goal)
                self.dialogue_state.save_intent_state(
                    task.task_id, session_key, intent_resolution.intent_name,
                    intent_resolution.slots, goal,
                )
                pending = self.dialogue_state.create(
                    session_key, goal, intent_resolution.question, history, task.task_id,
                )
                self.dialogue_state.update_task(task.task_id, status="awaiting_user")
                return ExecutionOutcome(
                    f"{intent_resolution.question}\n대기 작업 ID: {pending.task_id}",
                    "awaiting_user", goal, intent_resolution.question, pending.task_id,
                )

        if intent_resolution.ready:
            return self._execute_resolved_intent(
                intent_resolution, goal, session_key, agent_task_id, progress_callback
            )

        resolved = self.context_resolver.resolve(goal, history, session_key)
        if resolved.needs_clarification:
            question = resolved.clarification_question or "어떤 대상을 말씀하시는지 조금 더 구체적으로 알려주세요, 보스."
            task = (self.dialogue_state.get_task(session_key, agent_task_id)
                    if agent_task_id else None)
            if task:
                self.dialogue_state.update_task(task.task_id, status="awaiting_user")
            else:
                task = self.dialogue_state.create_task(session_key, goal)
            pending = self.dialogue_state.create(session_key, goal, question, history, task.task_id)
            self.dialogue_state.update_task(task.task_id, status="awaiting_user")
            response = f"{question}\n대기 작업 ID: {pending.task_id}"
            return ExecutionOutcome(response, "awaiting_user", goal, question, pending.task_id)
        goal = resolved.resolved_request
        if goal != resolved.original_request:
            print(f"[Context] 요청 해석: {resolved.original_request!r} → {goal!r} (confidence={resolved.confidence:.2f})")
        unsupported = self._unsupported_capability_message(goal)
        if unsupported:
            return ExecutionOutcome(unsupported, "completed", goal)
        if not agent_task_id:
            agent_task_id = self.dialogue_state.create_task(session_key, goal).task_id
        self.current_agent_task_id = agent_task_id
        self.dialogue_state.update_task(agent_task_id, status="running")
        with self._control_condition:
            self._task_controls[agent_task_id] = {"cancel": False, "pause": False}
        self._progress_callback = progress_callback
        self._emit_progress(f"작업 {agent_task_id}: 요청을 이해했습니다. 작업 계획을 준비하고 있습니다.")
        self.initialize(goal, session_id)

        # 1. 초기 Planning
        initial_context = self.build_context()
        allowed_tools = self._allowed_tools_for_goal(goal)
        _ = self.planner.decompose_goal(goal, initial_context, allowed_tools)
        self._emit_progress("작업 계획을 세웠습니다. 실행을 시작하겠습니다.")

        # 2. 메인 반복 루프
        while self.current_iteration < self.max_iterations:
            self.current_iteration += 1
            print(f"\n[Executor] 반복 {self.current_iteration}/{self.max_iterations}")

            should_continue = self.run_iteration()
            if not should_continue:
                break

        # 3. 최종 종료 처리
        response = self.finalize()
        control = self._task_controls.get(agent_task_id, {})
        if control.get("cancel"):
            status = "cancelled"
        elif self.terminal_error:
            status = "failed"
        else:
            status = "completed"
        self.dialogue_state.update_task(agent_task_id, status=status, result=response)
        with self._control_condition:
            self._task_controls.pop(agent_task_id, None)
        self.current_agent_task_id = ""
        self._progress_callback = None
        return ExecutionOutcome(response, status, goal, task_id=agent_task_id)

    def has_pending_request(self, session_id: Optional[str] = None) -> bool:
        return bool(self.dialogue_state.list(session_id or "default"))

    def enqueue_goal(self, goal: str, session_id: Optional[str] = None, priority: int = 0):
        """현재 실행과 분리해 새 목표를 영속 대기열에 등록한다."""
        return self.dialogue_state.create_task(session_id or "default", goal, priority)

    def _emit_progress(self, message: str):
        if self._progress_callback:
            try:
                self._progress_callback(message)
            except Exception as exc:
                print(f"[Executor] 진행 상황 callback 오류: {exc}")

    @staticmethod
    def is_control_command(text: str) -> bool:
        normalized = text.strip().lower()
        if normalized in {"작업 목록", "전체 작업 목록"}:
            return True
        return bool(re.fullmatch(
            r"(?:작업\s*)?[0-9a-f]{8}\s*(?:상태|취소|중단|일시정지|재개|우선순위\s*-?\d+|수정\s*[:：].+)",
            normalized, re.S,
        ))

    def handle_control_command(self, text: str, session_id: Optional[str] = None) -> ExecutionOutcome:
        session_key = session_id or "default"
        normalized = text.strip().lower()
        if normalized in {"작업 목록", "전체 작업 목록"}:
            tasks = self.dialogue_state.list_tasks(session_key)
            if not tasks:
                return ExecutionOutcome("등록된 작업이 없습니다, 보스.")
            lines = [f"- {t.task_id} [{t.status}] 우선순위 {t.priority}: {t.goal[:60]}" for t in tasks[:20]]
            return ExecutionOutcome("작업 목록입니다, 보스.\n" + "\n".join(lines))

        match = re.fullmatch(r"(?:작업\s*)?([0-9a-f]{8})\s*(.+)", normalized, re.S)
        if not match:
            return ExecutionOutcome("작업 제어 명령을 이해하지 못했습니다, 보스.", "failed")
        task_id, command = match.group(1), match.group(2).strip()
        task = self.dialogue_state.get_task(session_key, task_id)
        if not task:
            return ExecutionOutcome(f"작업 {task_id}을 찾지 못했습니다, 보스.", "failed", task_id=task_id)
        if command == "상태":
            return ExecutionOutcome(
                f"작업 {task_id}은 현재 {task.status} 상태이고 우선순위는 {task.priority}입니다, 보스.",
                task.status, task.goal, task_id=task_id,
            )
        priority_match = re.fullmatch(r"우선순위\s*(-?\d+)", command)
        if priority_match:
            priority = int(priority_match.group(1))
            self.dialogue_state.update_task(task_id, priority=priority)
            return ExecutionOutcome(f"작업 {task_id}의 우선순위를 {priority}로 변경했습니다, 보스.", task.status, task_id=task_id)

        if command in {"취소", "중단"}:
            self.dialogue_state.delete(session_key, task_id)
            with self._control_condition:
                control = self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})
                control["cancel"] = True
                control["pause"] = False
                self._control_condition.notify_all()
            self.dialogue_state.update_task(task_id, status="cancelled", result="사용자 취소")
            return ExecutionOutcome(f"작업 {task_id} 취소를 요청했습니다, 보스.", "cancelled", task_id=task_id)
        revision_match = re.fullmatch(r"수정\s*[:：]\s*(.+)", command, re.S)
        if revision_match:
            revision = revision_match.group(1).strip()
            with self._control_condition:
                control = self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})
                control["cancel"] = True
                control["pause"] = False
                self._control_condition.notify_all()
            self.dialogue_state.update_task(task_id, status="cancelled", result=f"수정 지시로 대체: {revision}")
            next_goal = f"원래 요청: {task.goal}\n사용자 수정 지시: {revision}\n수정 지시를 반영해 작업을 다시 수행하세요."
            return ExecutionOutcome(
                f"작업 {task_id}을 중단하고 수정 지시를 반영해 다시 시작하겠습니다, 보스.",
                "cancelled", task.goal, task_id=task_id, next_goal=next_goal,
            )
        if command == "일시정지":
            with self._control_condition:
                self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})["pause"] = True
            self.dialogue_state.update_task(task_id, status="paused")
            return ExecutionOutcome(f"작업 {task_id}을 일시정지했습니다, 보스.", "paused", task_id=task_id)
        if command == "재개":
            with self._control_condition:
                control = self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})
                control["pause"] = False
                self._control_condition.notify_all()
            if task.status in {"interrupted", "paused"} and task_id != self.current_agent_task_id:
                self.dialogue_state.update_task(task_id, status="cancelled", result="저장된 목표로 재실행")
                return ExecutionOutcome(
                    f"작업 {task_id}의 저장된 목표를 불러와 다시 실행하겠습니다, 보스.",
                    "interrupted", task.goal, task_id=task_id, next_goal=task.goal,
                )
            self.dialogue_state.update_task(task_id, status="running")
            return ExecutionOutcome(f"작업 {task_id}을 재개했습니다, 보스.", "running", task_id=task_id)
        return ExecutionOutcome("지원하지 않는 작업 제어 명령입니다, 보스.", "failed", task_id=task_id)

    def _wait_for_task_control(self) -> bool:
        task_id = self.current_agent_task_id
        if not task_id:
            return True
        with self._control_condition:
            control = self._task_controls.get(task_id, {})
            while control.get("pause") and not control.get("cancel"):
                self._control_condition.wait(timeout=0.5)
                control = self._task_controls.get(task_id, {})
            if control.get("cancel"):
                self.terminal_error = f"작업 {task_id}이 사용자 요청으로 취소되었습니다."
                return False
        return True

    def run_iteration(self) -> bool:
        """한 번의 반복 실행: Task 선택 → Context 빌드 → (Reasoning+Tool선택 통합) → Action → Verify → Reflect → Update → Evaluate"""
        try:
            if not self._wait_for_task_control():
                return False
            # 0. 재계획 여부 확인
            current_context = self.build_context()
            if self.planner.should_replan(self._consecutive_failures, current_context):
                print("[Executor] 재계획 시작!")
                try:
                    _ = self.planner.decompose_goal(
                        self.goal, current_context, self._allowed_tools_for_goal(self.goal)
                    )
                    self._consecutive_failures = 0  # 재계획 성공 시 초기화
                    print("[Executor] 재계획 완료!")
                except Exception as e:
                    print(f"[Executor] 재계획 오류: {e}")
            
            # 1. Task 선택
            task = self.select_task()
            if not task:
                print("[Executor] 더 이상 실행할 Task가 없습니다!")
                return False

            self.scratchpad.set_current_task(task.id)
            print(f"[Executor] Task 선택: {task.description}")
            self._emit_progress(f"진행 중: {task.description}")

            # 2. Context 빌드
            context = self.build_context(task)

            # 3~4. Native Tool Calling으로 '무엇을 할지'와 '어떤 도구를 쓸지'를 한 번에 결정
            # (예전의 reason_next_action() + select_tool() 두 단계를 decide_next_action() 하나로 통합)
            action = self.decide_next_action(task, context)
            domain_error = self._tool_domain_error(action)
            if domain_error:
                action = {"action_type": "error", "simple_result": domain_error}
            print(f"[Executor] Action 결정: {action}")

            if action.get("action_type") == "error":
                self.terminal_error = action.get("simple_result", "요청을 실행할 수 없습니다.")
                self.scratchpad.add_observation("invalid_tool_request", {}, self.terminal_error, False)
                self.scratchpad.fail_task(task.id, self.terminal_error)
                return False
            if action.get("action_type") == "use_tool":
                tool_name = action["tool_name"]
                tool_input = action.get("tool_input", {})
                print(f"[Executor] Tool 선택: {tool_name}, 입력: {tool_input}")
                self._emit_progress(f"도구 실행 중: {tool_name}")

                if not self._wait_for_task_control():
                    return False

                # 5~6. ToolExecutor가 중앙 권한 검사 후 실행
                result = self.execute_tool(tool_name, tool_input)
                print(f"[Executor] Tool 실행 결과: {result}")

                # 7. 실행 결과 검증
                verified = self.verify_execution(task, tool_name, tool_input, result)
                if not verified:
                    self._consecutive_failures += 1
                    self._total_failures += 1
                    print(f"[Executor] 실행 결과 검증 실패! 복구 시도... (연속 실패: {self._consecutive_failures})")
                    self._emit_progress(f"{tool_name} 실행 결과를 확인하지 못해 복구를 시도하고 있습니다.")
                    recover_result = self.recover(task, tool_name, tool_input, result)
                    if recover_result is not None:
                        result = recover_result
                        verified = True
                        self._consecutive_failures = 0  # 복구 성공하면 초기화
                        print("[Executor] 복구 성공!")
                    else:
                        print("[Executor] 복구 실패!")
                        self.scratchpad.add_observation(tool_name, tool_input, result, False)
                        self.scratchpad.fail_task(task.id, result)
                        self.reflect(task, tool_name, result, False)
                        if self._total_failures >= 3:
                            self.terminal_error = (
                                f"요청을 완료하지 못했습니다. 실제 도구 실행 결과: {result}"
                            )
                            return False
                        return True

                # 8. Observation 처리
                self.process_observation(task, tool_name, tool_input, result)
            else:
                # Tool이 필요 없는 경우 (간단한 텍스트 응답 등)
                simple_result = action.get("simple_result", "Task 완료")
                print(f"[Executor] 간단한 처리: {simple_result}")
                self.scratchpad.add_observation("simple_action", {}, simple_result, True)
                # 예전 코드는 이 분기에서 complete_task()를 호출하지 않아 Task 상태가
                # "in_progress"에 계속 머물러 있었습니다. (get_pending_tasks()는
                # status=="pending"만 보므로 evaluate_goal()의 판단 자체를 왜곡하진
                # 않았지만, Task 상태 자체는 부정확했습니다.) 여기서 명시적으로 완료 처리합니다.
                self.scratchpad.complete_task(task.id)
                self._consecutive_failures = 0  # Simple task 성공 시 초기화

            # 9. Reflection
            if action.get("action_type") == "use_tool":
                # Tool 사용한 경우 결과 전달
                self.reflect(task, tool_name, result, verified)
            else:
                # Simple task인 경우 성공으로 처리
                self.reflect(task, success=True)

            # 10. Memory 업데이트
            self.update_memory()

            # 11. Goal 평가
            goal_completed = self.evaluate_goal()
            if goal_completed:
                print("[Executor] Goal 달성!")
                return False

            return True

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[Executor] 반복 실행 오류: {e}")
            self.scratchpad.add_observation("error", {}, str(e), False)
            if self.scratchpad.current_task:
                self.scratchpad.fail_task(self.scratchpad.current_task.id, str(e))
            self._consecutive_failures += 1
            return True

    def build_context(self, task: Optional[Task] = None) -> str:
        """
        모든 Context를 조립:
        Conversation + Memory + Workspace + OS + 현재 Task + Scratchpad + 최근 Tool 결과
        """
        context_parts = []

        # 1. Goal
        context_parts.append(f"# 최종 목표 (Goal)\n{self.goal}\n")

        # 2. Scratchpad 상태
        scratchpad_context = self.scratchpad.get_context()
        context_parts.append(f"# 스크래치패드 (Scratchpad)\n{scratchpad_context}\n")

        # 3. Memory (Episode + Semantic)
        try:
            memory_context = build_memory_context(self.session_id, 15, True)
            if memory_context:
                context_parts.append(f"# 메모리 (Memory)\n{memory_context}\n")
        except Exception as e:
            print(f"[Executor] Memory Context 빌드 오류: {e}")

        # 4. Workspace 정보
        try:
            workspace_info = self.workspace_manager.get_info()
            if workspace_info.path:
                context_parts.append(f"# 작업 공간 (Workspace)\n경로: {workspace_info.path}\n이름: {workspace_info.name}\n")
        except Exception as e:
            print(f"[Executor] Workspace Context 빌드 오류: {e}")

        # 5. 현재 Task
        if task:
            context_parts.append(f"# 현재 Task\nID: {task.id}\n설명: {task.description}\n우선순위: {task.priority}\n")

        # 6. Context Manager (시스템 상태 + RAG 검색 결과 + OS 상태)
        # 주의: 실제 메서드명은 get_full_context(user_query, session_id)입니다.
        # 이전 코드는 존재하지 않는 get_context()를 호출해서 매번 예외가 나고 조용히 무시되고
        # 있었습니다 (즉 OS 상태/RAG 검색 결과가 한 번도 Context에 포함된 적이 없었습니다).
        # Memory/Scratchpad는 위 2, 3번에서 이미 넣었으므로 여기서는 일부 중복될 수 있지만,
        # 최소 침습적으로 버그만 우선 고칩니다 (중복 제거는 별도 리팩토링에서 다룰 부분).
        try:
            system_context = self.context_manager.get_full_context(
                user_query=self.goal, session_id=self.session_id
            )
            if system_context:
                context_parts.append(f"# 시스템 상태 (System Context)\n{system_context}\n")
        except Exception as e:
            print(f"[Executor] System Context 빌드 오류: {e}")

        return "\n".join(context_parts)

    def select_task(self) -> Optional[Task]:
        """다음으로 실행할 Task 선택: 대기 중인 Task 중 우선순위 높은 순으로"""
        pending_tasks = self.scratchpad.get_pending_tasks()
        if not pending_tasks:
            return None

        # 우선순위 높은 순으로 정렬
        pending_tasks.sort(key=lambda t: t.priority)
        return pending_tasks[0]

    def decide_next_action(self, task: Task, context: str) -> Dict[str, Any]:
        """
        Task를 수행하기 위해 도구가 필요한지, 필요하다면 어떤 도구/입력을 쓸지를
        LLM의 native tool calling(function calling)으로 **한 번에** 결정합니다.

        기존에는 이 판단이 두 단계(reason_next_action → select_tool)로 나뉘어 있었고,
        둘 다 모델이 "JSON만 반환하세요"라는 텍스트 지시를 따르길 바라며 응답을 문자열
        파싱하는 방식이었습니다. 모델이 지시를 안 따르거나(코드블록 없이 설명을 덧붙이거나),
        빈 응답을 주거나, 목록에 없는 도구 이름을 지어내는 경우 전부 여기서 깨졌습니다.

        Anthropic/Ollama가 이미 지원하는 chat_with_tools()(core/llm.py)를 쓰면, 모델이
        "도구 호출" 또는 "일반 텍스트 응답" 둘 중 하나를 구조화된 형태로 반환하도록
        API 레벨에서 강제되므로 이 파싱 실패 자체가 원천적으로 줄어듭니다.
        """
        # Tool Selector 전용 system prompt로 잠깐 교체 (끝나면 finally에서 원복)
        self.reasoning_llm.set_system_prompt(
            "당신은 Jarvis의 Action Reasoner 겸 Tool Selector입니다.\n"
            "주어진 Task를 수행하기 위해 도구가 필요하면 반드시 제공된 도구 중 하나를 호출하세요.\n"
            "도구 없이 바로 답할 수 있는 간단한 작업이나 이미 끝난 작업이면, 도구를 호출하지 말고 "
            "결과나 답변을 자연스러운 한국어 텍스트로 바로 답하세요.\n"
            "제공된 도구 목록에 없는 도구는 절대 지어내지 마세요.\n"
            "최종 Goal과 현재 Task를 가장 높은 우선순위로 따르고, 과거 Memory의 다른 주제는 무시하세요.\n"
            "Plugin Registry가 현재 요청에 허용한 도구만 사용하고 다른 도메인의 도구는 호출하지 마세요."
        )
        try:
            messages = [
                {"role": "user", "content": f"Context:\n{context}\n\nTask: {task.description}\n\n이 Task를 수행하세요."}
            ]
            allowed_tools = self._allowed_tools_for_goal(getattr(self, "goal", ""))
            try:
                text, tool_use_blocks = self.reasoning_llm.chat_with_tools(messages, allowed_tools)
            except TypeError:
                # 기존 테스트/사용자 정의 LLM 구현 하위 호환
                text, tool_use_blocks = self.reasoning_llm.chat_with_tools(messages)

            if tool_use_blocks:
                # 한 iteration에 Tool 호출 1개만 처리 (여러 개는 다음 iteration에서 순차 처리)
                tool_name, tool_input = self._read_tool_use_block(tool_use_blocks[0])
                if tool_name and tool_name in get_tool_names():
                    return {"action_type": "use_tool", "tool_name": tool_name, "tool_input": tool_input}
                print(f"[Executor] 모델이 존재하지 않는 Tool을 호출함: {tool_name} → 무시하고 simple_task로 처리")

            legacy = self._extract_json(text) if text else None
            if legacy:
                try:
                    request = json.loads(legacy)
                    requested_name = request.get("name") if isinstance(request, dict) else None
                    raw_input = request.get("arguments", {}) if isinstance(request, dict) else {}
                    if requested_name in get_tool_names() and isinstance(raw_input, dict):
                        return {"action_type": "use_tool", "tool_name": requested_name, "tool_input": raw_input}
                    if requested_name:
                        return {"action_type": "error", "simple_result": f"등록되지 않은 도구 요청을 차단했습니다: {requested_name}"}
                except json.JSONDecodeError:
                    pass
            normalized_text = (text or "").lstrip().casefold()
            if not text or normalized_text.startswith(("오류:", "오류가 발생했습니다:", "error:")):
                return {
                    "action_type": "error",
                    "simple_result": text or "LLM이 빈 응답을 반환해 작업을 중단했습니다.",
                }
            return {"action_type": "simple_task", "simple_result": text}

        except Exception as e:
            print(f"[Executor] Action/Tool 결정 오류: {e}")
            return {"action_type": "simple_task", "simple_result": f"Task 완료: {task.description}"}
        finally:
            # 다른 메서드(generate_response 등)에 영향 주지 않도록 원래 시스템 프롬프트로 복원
            self.reasoning_llm.set_system_prompt(self._default_reasoning_prompt)

    @staticmethod
    def _read_tool_use_block(block) -> tuple[Optional[str], Dict[str, Any]]:
        """
        Anthropic SDK는 tool_use 블록을 속성 접근 객체(block.name, block.input)로,
        Ollama 쪽 구현은 dict(block["name"], block["input"])로 반환하므로 둘 다 처리합니다.
        """
        if isinstance(block, dict):
            return block.get("name"), block.get("input") or {}
        return getattr(block, "name", None), getattr(block, "input", None) or {}

    def _allowed_tools_for_goal(self, goal: str) -> Optional[List[str]]:
        if not hasattr(self, "intent_router"):
            return None
        resolution = self.intent_router.resolve(goal)
        return [resolution.tool_name] if resolution.matched and resolution.tool_name else None

    def _tool_domain_error(self, action: Dict[str, Any]) -> Optional[str]:
        if action.get("action_type") != "use_tool":
            return None
        tool_name = str(action.get("tool_name", ""))
        resolution = self.intent_router.resolve(self.goal)
        if resolution.matched and resolution.tool_name and tool_name != resolution.tool_name:
            return (
                f"요청 intent '{resolution.intent_name}'와 관련 없는 도구 호출을 차단했습니다: {tool_name}. "
                f"Plugin Registry 계약에 따라 {resolution.tool_name}을 사용해야 합니다."
            )
        return None

    def _execute_resolved_intent(self, resolution: IntentResolution, goal: str,
                                 session_id: str, task_id: str = "",
                                 progress_callback: Optional[Callable[[str], None]] = None) -> ExecutionOutcome:
        task_id = task_id or self.dialogue_state.create_task(session_id, goal).task_id
        self.dialogue_state.update_task(task_id, status="running")
        if progress_callback:
            progress_callback(f"작업 {task_id}: {resolution.tool_name} 실행을 시작합니다.")
        task = Task(task_id, resolution.intent_name, status="in_progress")
        result = self.execute_tool(resolution.tool_name, resolution.slots)
        verified = self.verify_execution(task, resolution.tool_name, resolution.slots, result)
        status = "completed" if verified else "failed"
        response = result if verified else f"요청을 완료하지 못했습니다. 실제 도구 실행 결과: {result}"
        self.dialogue_state.update_task(task_id, status=status, result=response)
        self.dialogue_state.delete_intent_state(task_id)
        return ExecutionOutcome(response, status, goal, task_id=task_id)

    def request_permission(self, tool_name: str) -> bool:
        """
        Permission 체크: Tool 실행 전 실제 PermissionManager를 통해 권한 확인.

        예전엔 여기서 무조건 True를 반환했습니다 (permission_level만 계산해놓고 실제로는
        아무 데도 안 씀). main_qt.py에 이미 UI 승인 다이얼로그 콜백이 PermissionManager에
        연결돼 있었는데 Executor가 그걸 타지 않고 있었던 것입니다.

        TOOL_PERMISSION_MAP(core/permission.py)에 없는 도구는 위험도가 낮다고 분류된
        도구이므로 확인 없이 통과시킵니다. SAFE 레벨은 자동 허용, CONFIRM은 매번 UI 승인
        필요, SYSTEM은 최초 1회만 승인하면 이후 자동 허용됩니다 (PermissionManager의 기존
        구현 그대로).
        """
        permission_id = TOOL_PERMISSION_MAP.get(tool_name)
        if permission_id is None:
            return True

        granted = self.permission_manager.request_permission(permission_id)
        print(f"[Executor] Permission 체크: {tool_name} → {permission_id} = {'허용' if granted else '거부'}")
        return granted

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Tool 실행"""
        return self.tool_executor.execute_tool(tool_name, tool_input)

    def verify_execution(self, task: Task, tool_name: str, tool_input: Dict[str, Any], result: str) -> bool:
        """실행 결과 검증: ToolVerifier를 사용해 정교하게 확인"""
        verification_result = self.verifier.verify(tool_name, tool_input, result)
        print(f"[Executor] 검증 결과: {'성공' if verification_result.success else '실패'} - {verification_result.message}")
        return verification_result.success

    def process_observation(self, task: Task, tool_name: str, tool_input: Dict[str, Any], result: str):
        """Observation 처리: Scratchpad에 기록"""
        self.scratchpad.add_observation(tool_name, tool_input, result, True)
        self.scratchpad.complete_task(task.id)
        self._retry_count = 0  # 성공하면 재시도 횟수 리셋
        print(f"[Executor] Task 완료 처리: {task.id}")

    def reflect(self, task: Task, tool_name: str = "", result: str = "", success: bool = True):
        """Reflection: 실패/성공 분석 및 학습"""
        try:
            if not success:
                print(f"[Executor] Reflection: 실패 분석 중...")
                # Reflection 모듈을 사용해 실패 분석
                analysis = self.reflection.analyze_failure(result, task.description)
                print(f"[Executor] Reflection 결과: {analysis}")
                # 분석 결과를 Scratchpad에 기록
                self.scratchpad.add_observation(
                    "reflection",
                    {"analysis": analysis},
                    f"실패 분석: {analysis}",
                    True
                )
            else:
                print(f"[Executor] Reflection: 성공 케이스 학습 중...")
                # 성공한 경우에도 간단히 기록
                self.scratchpad.add_observation(
                    "reflection",
                    {"success": True},
                    "성공 케이스 기록",
                    True
                )
        except Exception as e:
            print(f"[Executor] Reflection 오류: {e}")

    def update_memory(self):
        """Memory 업데이트: Episode Memory에 현재 상태 저장"""
        try:
            # 현재 Scratchpad 상태와 대화 내용을 Memory에 저장
            memory_context = self.scratchpad.get_context()
            if memory_context and self.memory:
                # 간단히 Memory에 현재 상태 기록
                print(f"[Executor] Memory 업데이트 중...")
                # 실제로는 더 정교한 Memory 업데이트 로직이 필요하지만 여기서는 기본만
        except Exception as e:
            print(f"[Executor] Memory 업데이트 오류: {e}")

    def evaluate_goal(self) -> bool:
        """Goal 평가: 정말로 달성됐는지 확인"""
        # Scratchpad에 남은 Task가 없고, 최종 결과가 있으면 완료로 판단
        pending_tasks = self.scratchpad.get_pending_tasks()
        if pending_tasks:
            return False
        tasks = self.scratchpad.tasks
        successful_tool_observations = [
            observation for observation in self.scratchpad.observations
            if observation.success and observation.tool_name not in {"reflection", "simple_action"}
        ]
        if tasks and all(task.status == "completed" for task in tasks) and successful_tool_observations:
            return True

        # LLM으로 최종 Goal 달성 여부 확인
        context = self.build_context()
        system_prompt = """당신은 Goal Evaluator입니다.
전체 Context를 보고, 최종 Goal이 달성됐는지 판단하세요!

응답 형식 (JSON만 반환):
{
    "goal_completed": true | false,
    "reason": "왜 그렇게 판단했는지"
}
"""
        user_prompt = f"Context:\n{context}\n\nGoal: {self.goal}\n\n달성됐나요?"

        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])

            json_str = self._extract_json(response)
            if json_str:
                result = json.loads(json_str)
                return result.get("goal_completed", False)

        except Exception as e:
            print(f"[Executor] Goal Evaluation 오류: {e}")

        tasks = self.scratchpad.tasks
        return bool(tasks) and all(task.status == "completed" for task in tasks)

    def recover(self, task: Task, tool_name: str, tool_input: Dict[str, Any], last_result: str) -> Optional[str]:
        """실패 복구: RecoveryManager를 사용해 단계별로 시도"""
        print(f"[Executor] 복구 시도... (재시도 횟수: {self._retry_count})")
        
        recovery_result = self.recovery_manager.recover(
            task=task,
            tool_name=tool_name,
            tool_input=tool_input,
            last_result=last_result,
            retry_count=self._retry_count
        )

        self._retry_count += 1
        
        if recovery_result.success:
            print(f"[Executor] 복구 성공! {recovery_result.message}")
            return recovery_result.result
        print(f"[Executor] 복구 실패... {recovery_result.message}")
        return None

    @staticmethod
    def _unsupported_capability_message(goal: str) -> Optional[str]:
        normalized = goal.lower()
        mail_account = any(word in normalized for word in ("gmail", "지메일", "구글 메일"))
        connection = any(word in normalized for word in ("연결", "연동", "oauth", "로그인", "인증"))
        if mail_account and connection:
            return (
                "현재 Gmail OAuth 계정 연결 기능은 아직 구현되어 있지 않습니다, 보스. "
                "지금 제공되는 Mail 기능은 .env에 설정한 SMTP 계정으로 초안을 만들거나 메일을 전송하는 방식입니다. "
                "Google OAuth 클라이언트와 토큰 저장 기능을 구현하기 전에는 Gmail 연결을 진행했다고 보고하지 않겠습니다."
            )
        return None

    def finalize(self) -> str:
        """최종 종료 처리: 최종 답변 생성"""
        if self.terminal_error:
            return self.terminal_error
        return self.generate_response()

    def generate_response(self) -> str:
        """최종 답변 생성"""
        context = self.build_context()
        successful_observations = [
            {
                "tool_name": observation.tool_name,
                "input": observation.input_data,
                "result": observation.result,
            }
            for observation in self.scratchpad.observations
            if observation.success and observation.tool_name not in {"reflection", "simple_action"}
        ][-3:]

        # 현재 날씨처럼 구조가 고정된 외부 사실은 LLM이 수치를 누락하거나 바꾸지
        # 못하도록 검증된 Tool 결과에서 직접 표현한다. 다른 도구 결과는 아래의
        # 원문 Observation을 LLM에 전달한다.
        if successful_observations and successful_observations[-1]["tool_name"] == "get_weather":
            try:
                weather = json.loads(successful_observations[-1]["result"])
                requested = weather.get("requested_location") or weather.get("resolved_location") or "요청한 지역"
                precision = (
                    " 정확한 동 단위 관측소 값이 아니라 서울시 기준 근사값입니다."
                    if weather.get("location_precision") == "city" else ""
                )
                return (
                    f"{requested}은(는) 현재 {weather.get('temperature_c')}°C이고, "
                    f"체감온도는 {weather.get('apparent_temperature_c')}°C입니다, 보스. "
                    f"오늘 최저 {weather.get('today_min_c')}°C, 최고 {weather.get('today_max_c')}°C이며, "
                    f"습도는 {weather.get('humidity_percent')}%입니다.{precision}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        system_prompt = """당신은 Jarvis입니다.
전체 Context를 보고, 최종 답변을 한국어로 작성하세요!
보스라는 호칭을 사용하세요.
외부의 현재 사실(날씨, 일정, 메일, 웹 정보 등)은 성공한 Tool Observation에 있는 값만 사용하세요.
Tool이 실패했거나 관측값이 없으면 절대 수치를 추측하지 말고 확인하지 못했다고 답하세요.
location_precision이 city이면 동 단위 관측이 아니라 도시 기준 근사값임을 명시하세요.
최종 Goal에 적힌 '후속 질문의 핵심 요구'에 먼저 직접 답하고, Tool 수치와 이름을 바꾸거나 생략하지 마세요.
"""
        verified_results = json.dumps(successful_observations, ensure_ascii=False, indent=2)
        user_prompt = (
            f"Context:\n{context}\n\n"
            f"검증된 Tool 결과 원문:\n{verified_results}\n\n"
            f"최종 Goal:\n{self.goal}\n\n최종 답변은?"
        )

        try:
            return self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])
        except Exception as e:
            return f"죄송해요, 보스! 최종 답변 생성 중 오류가 발생했어요: {e}"

    def _extract_json(self, text: str) -> Optional[str]:
        """응답에서 JSON 문자열 추출"""
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            return text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            return text[start:end].strip()
        else:
            # 그냥 전체 텍스트가 JSON인지 확인
            try:
                json.loads(text)
                return text
            except Exception:
                return None


# Singleton instance
_executor = None


def get_executor() -> Executor:
    """Executor 싱글톤 인스턴스 반환"""
    global _executor
    if _executor is None:
        _executor = Executor()
    return _executor
