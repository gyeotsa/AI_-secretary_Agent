from typing import List, Optional, Dict, Any, Callable
from dataclasses import asdict, dataclass, replace
import json
import re
import threading
import time

from core.llm import get_llm_client, OllamaClient
from core.answer_verification import AnswerVerificationService
from core.scratchpad import Scratchpad, Task
from core.planner import Planner, PlanningError
from core.tools import get_tool_executor, get_tools_description_text, get_tool_names
from core.reflection import Reflection
from core.context import ContextManager
from core.permission import get_permission_manager, TOOL_PERMISSION_MAP
from core.memory import get_memory, build_memory_context, build_relevant_knowledge_context
from core.workspace import get_workspace_manager
from core.verifier import get_tool_verifier
from core.recovery import get_recovery_manager
from core.conversation_context import ConversationContextResolver, ResolvedRequest
from core.dialogue_state import get_dialogue_state_store
from core.intent_router import IntentRouter, IntentResolution
from core.custom_tts import load_custom_voice_profiles
from core.model_registry import get_model_role_router
from core.tool_result import ToolRunResult, ToolRunStatus
from core.plan_runtime import PlanCoordinator, PlanDAG, PlanRunResult, PlanStep
from core.agent_services import (
    ConversationService, PlanningService, ResponseComposer, guard_conversation_response,
)
from core.assistant_settings import get_assistant_settings
from core.response_realizer import ResponseRealizer
from core.learning_runtime import get_learning_runtime, record_runtime_event
from core.tool_loadout import ToolLoadoutSelector
from core.reasoning_policy import ReasoningPolicy
from core.evaluation_runtime import seed_core_evaluation_cases
from core.task_contracts import (
    AcceptanceCriterion,
    ContractStatus,
    ResourceBudget,
    RetryPolicy,
    get_supervisor_runtime,
)
from core.quality_metrics import get_quality_metric_store
from core.utterance_scope import analyze_utterance_scope, allows_execution_follow_up
from core.turn_context import (
    TurnExecutionContext, bind_turn_context, current_turn_context, check_turn_cancelled,
)
from core.plugin import ToolCancelledError


def _safe_contract_value(value: Any) -> Any:
    """Make runtime contracts JSON-safe without leaking arbitrary object reprs."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _safe_contract_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_contract_value(item) for item in value]
    return {"type": type(value).__name__}


def _local_answer_draft_client(contract):
    client = OllamaClient("code" if contract.requires_code else "document")
    # Release this request's model after the phase; never unload arbitrary
    # models belonging to another app/session in order to make room.
    client.profile = replace(client.profile, keep_alive="0")
    return client


@dataclass
class ExecutionOutcome:
    response: str
    status: str = "completed"
    goal: str = ""
    question: str = ""
    task_id: str = ""
    next_goal: str = ""
    tool_result: Optional[ToolRunResult] = None
    # A specialist workspace may legitimately require several tools (for
    # example create -> render -> inspect).  Keeping only ``tool_result`` made
    # every multi-step run lose its evidence at the workspace review boundary.
    # The singular field remains for compatibility with existing callers.
    tool_results: tuple[ToolRunResult, ...] = ()
    retry_count: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    grounded_conversation: bool = False
    unverified_completion_claim: bool = False
    response_truncated: bool = False
    unsupported_activity_claim: bool = False
    answer_review: Optional[Dict[str, Any]] = None


class Executor:
    """
    진정한 Agent Runtime!
    Jarvis의 중앙 실행 엔진으로, Planner와 협력하고, Context를 조립하고,
    적절한 Tool을 선택하고, 권한을 확인하고, 실행 결과를 검증하고,
    실패를 복구하고, Memory와 Scratchpad를 갱신하며,
    목표 달성 여부를 계속 판단합니다!
    """
    _EXECUTION_REQUEST_PATTERN = re.compile(
        r"(?:생성|작성|수정|변경|삭제|저장|전송|전달|발송|실행|설치|등록|예약|열기|닫기|"
        r"켜기|끄기|다운로드|업로드|검색|조회|분석|진단|검사|점검|편집|변환|재생).{0,20}"
        r"(?:해\s*줘|해주세요|해줄래|부탁|실행|처리)"
        r"|(?:만들어|고쳐|지워|보내|전달해|실행해|설치해|등록해|예약해|열어|닫아|켜|꺼|"
        r"찾아|검색해|조회해|분석해|진단해|검사해|점검해|편집해|변환해|"
        r"재생해|틀어)\s*(?:줘|주세요|줄래)?",
        re.IGNORECASE | re.DOTALL,
    )
    _GENERIC_ACTION_REQUEST_PATTERN = re.compile(
        r"(?:해\s*줘|해주세요|해줄래|해볼래|해봐|부탁해|부탁합니다|"
        r"실행해|처리해|줘|주세요|줄래)[.!?\s]*$",
        re.IGNORECASE,
    )
    _SOCIAL_ONLY_PATTERN = re.compile(
        r"^(?:안녕|반가워|고마워|감사해|잘\s*지내|심심해|힘들어|오늘\s*기분.{0,12})[.!?\s]*$",
        re.IGNORECASE,
    )
    # Claiming an approval is an atomic state transition.  This process-local
    # guard prevents two simultaneous UI/voice callbacks from executing the same
    # external-send task before either callback observes the other's transition.
    _APPROVAL_CLAIM_LOCK = threading.Lock()

    def __init__(self):
        self.llm = get_llm_client("conversation")
        self.reasoning_llm = get_llm_client("reasoning")
        # Execution state must belong to this runtime instance.  Sharing the
        # legacy singleton scratchpad let a voice turn overwrite a GUI/workspace
        # turn's goal, observations, and current task.
        self.scratchpad = Scratchpad()
        self.context_manager = ContextManager(scratchpad=self.scratchpad)
        self.planner = Planner(scratchpad=self.scratchpad, context_manager=self.context_manager)
        self.tool_executor = get_tool_executor()
        self.reflection = Reflection(scratchpad=self.scratchpad)
        self.permission_manager = get_permission_manager()
        self.memory = get_memory()
        self.workspace_manager = get_workspace_manager()
        self.verifier = get_tool_verifier()
        self.recovery_manager = get_recovery_manager()
        self.context_resolver = ConversationContextResolver(self.llm)
        self.response_realizer = ResponseRealizer(self.llm)

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
        from core.semantic_request import SemanticRequestInterpreter
        self.semantic_interpreter = SemanticRequestInterpreter(
            self.reasoning_llm, self.tool_executor.plugin_registry
        )
        self.model_role_router = get_model_role_router()
        self._progress_callback: Optional[Callable[[str], None]] = None
        self.current_agent_task_id = ""
        self._task_controls: Dict[str, Dict[str, bool]] = {}
        self._approval_inflight: set[str] = set()
        self._control_condition = threading.Condition()
        # The executor still owns mutable plan/goal fields. Serialize complete
        # turns until those fields are migrated into an immutable turn context.
        self._turn_lock = threading.RLock()
        self.plan_coordinator = PlanCoordinator()
        self.planning_service = PlanningService(self.planner)
        self.response_composer = ResponseComposer()
        self.conversation_service = ConversationService(
            self.llm, answer_verifier=AnswerVerificationService(),
            generation_client_factory=_local_answer_draft_client,
        )
        self.current_plan: Optional[PlanDAG] = None
        self.learning_runtime = get_learning_runtime()
        seed_core_evaluation_cases(self.learning_runtime)
        self.tool_loadout = ToolLoadoutSelector(self.tool_executor.plugin_registry)
        self.reasoning_policy = ReasoningPolicy()
        # The supervisor is part of the real execution path, not a display-only
        # Command Center data source. Every executable DAG node receives a
        # persisted input/output/permission/resource/verification contract.
        self.supervisor = get_supervisor_runtime()
        self.quality_metrics = get_quality_metric_store()

    def initialize(self, goal: str, session_id: Optional[str] = None):
        """초기화: Goal 설정, Scratchpad 초기화, Context 빌드"""
        self.goal = goal
        self.session_id = session_id or ""
        self.current_iteration = 0
        self._retry_count = 0
        self._consecutive_failures = 0
        self._total_failures = 0
        self.terminal_error = None
        self.current_plan = None
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
                     existing_task_id: Optional[str] = None,
                     allowed_tool_names: Optional[List[str]] = None,
                     execution_context: str = "",
                     turn_context: Optional[TurnExecutionContext] = None) -> ExecutionOutcome:
        """Execute one turn atomically against this runtime's mutable state."""
        turn_context = turn_context or current_turn_context()
        turn_lock = getattr(self, "_turn_lock", None)
        if turn_lock is None:
            # A few compatibility tests and integrations construct a lightweight
            # Executor via __new__. Give those callers the same safety contract.
            turn_lock = threading.RLock()
            self._turn_lock = turn_lock
        with turn_lock:
            try:
                with bind_turn_context(turn_context):
                    check_turn_cancelled()
                    if turn_context is not None:
                        session_id = turn_context.session_id
                    outcome = self._execute_turn_traced(
                        goal, session_id, conversation_history, progress_callback,
                        existing_task_id, allowed_tool_names, execution_context,
                    )
                    if not (outcome.task_id and outcome.status in {"completed", "partial", "unverified"}):
                        check_turn_cancelled()
                    return outcome
            except ToolCancelledError as exc:
                return ExecutionOutcome(str(exc), "cancelled", goal)

    def _execute_turn_traced(self, goal: str, session_id: Optional[str] = None,
                     conversation_history: Optional[List[Dict[str, str]]] = None,
                     progress_callback: Optional[Callable[[str], None]] = None,
                     existing_task_id: Optional[str] = None,
                     allowed_tool_names: Optional[List[str]] = None,
                     execution_context: str = "") -> ExecutionOutcome:
        """Record one complete, privacy-redacted trajectory around the runtime turn."""
        session_key = session_id or "default"
        turn_started = time.perf_counter()
        learning_runtime = getattr(self, "learning_runtime", None)
        if learning_runtime is None:
            learning_runtime = get_learning_runtime()
            self.learning_runtime = learning_runtime
        trajectory_id = learning_runtime.begin(
            goal, session_id=session_key, workspace=self._workspace_scope(),
            metadata={"existing_task_id": existing_task_id or ""},
        )
        try:
            outcome = self._execute_turn_impl(
                goal, session_id, conversation_history, progress_callback,
                existing_task_id, allowed_tool_names, execution_context,
            )
            learning_runtime.finish(
                trajectory_id, status=outcome.status, response=outcome.response,
                metadata={"task_id": outcome.task_id, "retry_count": outcome.retry_count,
                          "completed_steps": outcome.completed_steps,
                          "failed_steps": outcome.failed_steps,
                          "answer_review": _safe_contract_value(outcome.answer_review),
                          "unsupported_activity_claim": outcome.unsupported_activity_claim},
            )
            self._record_turn_quality(goal, outcome, turn_started, conversation_history)
            return outcome
        except ToolCancelledError as exc:
            learning_runtime.finish(
                trajectory_id, status="cancelled", response=str(exc),
                metadata={"cancelled": True},
            )
            raise
        except Exception as exc:
            learning_runtime.event("runtime_exception", {"error": str(exc)}, trajectory_id)
            learning_runtime.finish(
                trajectory_id, status="failed", response="",
                metadata={"exception": type(exc).__name__},
            )
            quality_metrics = getattr(self, "quality_metrics", None) or get_quality_metric_store()
            self.quality_metrics = quality_metrics
            quality_metrics.record(
                "runtime_completion", 0.0, success=False,
                context={"goal": goal[:300], "exception": type(exc).__name__},
            )
            quality_metrics.record(
                "latency_ms", (time.perf_counter() - turn_started) * 1000,
                success=False, context={"goal": goal[:300]},
            )
            raise

    def _record_turn_quality(self, goal: str, outcome: ExecutionOutcome,
                             started_at: float,
                             history: Optional[List[Dict[str, str]]] = None) -> None:
        """Record only observable runtime outcomes; never invent success data."""
        latency = (time.perf_counter() - started_at) * 1000
        succeeded = outcome.status == "completed" and not outcome.unverified_completion_claim
        quality_metrics = getattr(self, "quality_metrics", None) or get_quality_metric_store()
        self.quality_metrics = quality_metrics
        quality_metrics.record(
            "runtime_completion", 1.0 if succeeded else 0.0, success=succeeded,
            context={"goal": goal[:300], "status": outcome.status,
                     "task_id": outcome.task_id},
        )
        quality_metrics.record(
            "latency_ms", latency, success=succeeded,
            context={"goal": goal[:300], "status": outcome.status},
        )
        if outcome.answer_review and outcome.answer_review.get("status") != "not_required":
            review_passed = outcome.answer_review.get("status") == "passed"
            quality_metrics.record(
                "answer_review", float(review_passed), success=review_passed,
                context={"review": _safe_contract_value(outcome.answer_review),
                         "status": outcome.status},
            )
        if outcome.unsupported_activity_claim:
            quality_metrics.record(
                "unsupported_activity_claim", 1.0, success=False,
                context={"status": outcome.status, "blocked": True, "tool_count": 0},
            )
        if outcome.status in {"awaiting_input", "awaiting_user"}:
            question = str(outcome.question or outcome.response).strip()
            quality_metrics.record(
                "clarification_requested", 1.0,
                success=True, context={"question": question[:500]},
            )
        execution_request = not outcome.grounded_conversation and not analyze_utterance_scope(goal, history).discussion and bool(
            self._EXECUTION_REQUEST_PATTERN.search(goal)
            or self._GENERIC_ACTION_REQUEST_PATTERN.search(goal)
        )
        if outcome.unverified_completion_claim:
            quality_metrics.record(
                "false_completion", 1.0, success=False,
                context={"goal": goal[:300], "task_id": outcome.task_id,
                         "tool_count": 0, "blocked": True},
            )
        elif execution_request and outcome.status == "completed":
            results = list(outcome.tool_results or ())
            if outcome.tool_result is not None and all(
                item is not outcome.tool_result for item in results
            ):
                results.append(outcome.tool_result)
            # A step counter proves only that the scheduler advanced.  It does
            # not prove that a side effect happened or that its result was
            # verified.  Every executed result exposed by the DAG must be a
            # successful typed result with concrete evidence.
            verified = bool(results) and all(
                result.succeeded and bool(result.evidence)
                for result in results
            )
            quality_metrics.record(
                "false_completion", 0.0 if verified else 1.0,
                success=verified, context={"goal": goal[:300],
                                           "task_id": outcome.task_id,
                                           "tool_count": len(results)},
            )

    def record_acceptance(self, outcome: ExecutionOutcome, verdict: dict,
                          *, session_id: str, workspace_path: str) -> bool:
        """Persist final goal evidence, separately from a tool's completion."""
        if (not outcome.task_id or not isinstance(verdict, dict)
                or type(verdict.get("passed")) is not bool
                or outcome.status not in {"completed", "partial", "unverified"}):
            return False
        accepted = self.dialogue_state.record_acceptance(
            session_id, outcome.task_id, workspace_path,
            _safe_contract_value(verdict), outcome.response,
        )
        if not accepted:
            return False
        task = self.dialogue_state.get_task(session_id, outcome.task_id, workspace_path)
        passed = bool(task and task.status == "completed" and verdict["passed"])
        if task is not None:
            outcome.status = task.status
        metrics = getattr(self, "quality_metrics", None) or get_quality_metric_store()
        self.quality_metrics = metrics
        context = {"task_id": outcome.task_id, "status": outcome.status,
                   "review": _safe_contract_value(verdict)}
        for key in ("task_success", "specialist_artifact_quality"):
            metrics.record(key, float(passed), success=passed, context=context)
        record_runtime_event("goal_acceptance", **context)
        return True

    def _execute_turn_impl(self, goal: str, session_id: Optional[str] = None,
                     conversation_history: Optional[List[Dict[str, str]]] = None,
                     progress_callback: Optional[Callable[[str], None]] = None,
                     existing_task_id: Optional[str] = None,
                     allowed_tool_names: Optional[List[str]] = None,
                     execution_context: str = "") -> ExecutionOutcome:
        """질문 대기와 재개를 지원하는 한 번의 대화 턴을 실행한다."""
        session_key = session_id or "default"
        workspace_scope = self._workspace_scope()
        history = list(conversation_history or [])
        normalized = goal.strip().lower()
        tool_scope = self._validated_tool_scope(allowed_tool_names)

        def terminal_outcome(response: str, status: str = "completed",
                             pending_question: str = "") -> ExecutionOutcome:
            """Close a persisted queued task even when no Tool/Planner path is needed."""
            blocked_claim = bool(getattr(response, "unverified_completion", False))
            unsupported_activity = bool(getattr(response, "unsupported_activity", False))
            truncated = bool(getattr(response, "truncated", False))
            answer_review = getattr(response, "answer_review", None)
            review_data = answer_review.to_dict() if answer_review is not None else None
            if blocked_claim or unsupported_activity:
                status = "failed"
            elif truncated:
                status = "partial"
            elif review_data and review_data.get("status") == "incomplete":
                status = "partial"
            elif review_data and review_data.get("status") in {"failed", "unverified"}:
                status = "unverified"
            response = str(response)
            if existing_task_id:
                task = self.dialogue_state.get_task(
                    session_key, existing_task_id, workspace_scope
                )
                if task and task.status == "queued":
                    self.dialogue_state.transition_task(existing_task_id, "running")
                    self.dialogue_state.transition_task(
                        existing_task_id, status, result=response,
                        pending_question=pending_question,
                    )
                return ExecutionOutcome(
                    response, status, goal, question=pending_question,
                    task_id=existing_task_id,
                    unverified_completion_claim=blocked_claim,
                    response_truncated=truncated,
                    unsupported_activity_claim=unsupported_activity,
                    answer_review=review_data,
                )
            return ExecutionOutcome(
                response, status, goal, question=pending_question,
                unverified_completion_claim=blocked_claim,
                response_truncated=truncated,
                unsupported_activity_claim=unsupported_activity,
                answer_review=review_data,
            )

        utterance_scope = analyze_utterance_scope(goal, history)
        if utterance_scope.discussion:
            # A question about a command is not the answer to an outstanding
            # recipient/body slot either.  Keep that task untouched and answer
            # without granting the Planner the embedded command's authority.
            return terminal_outcome(self._respond_conversationally(goal, history))

        if self.is_control_command(goal):
            return self.handle_control_command(goal, session_key)

        if getattr(self, "semantic_interpreter", None) is not None:
            # Model confidence cannot confer authority denied by the current
            # utterance. Quoted payloads were masked by the shared scope parser.
            if utterance_scope.negated:
                return terminal_outcome("요청하신 작업은 실행하지 않겠습니다.", "cancelled")
            if utterance_scope.conditional:
                question = (f"‘{utterance_scope.condition}’ 조건을 아직 확인하지 않았습니다. "
                            "조건을 먼저 확인할까요, 아니면 지금 실행하라는 뜻인가요?")
                task = (self.dialogue_state.get_task(session_key, existing_task_id, workspace_scope)
                        if existing_task_id else None)
                task = task or self.dialogue_state.create_task(session_key, goal, workspace_path=workspace_scope)
                self.dialogue_state.delete(session_key, task.task_id)
                self.dialogue_state.create(session_key, goal, question, history, task.task_id, workspace_scope)
                return ExecutionOutcome(question, "awaiting_user", goal, question, task.task_id)

        # Production natural-language decisions cross one semantic boundary.
        # Legacy declarative routing remains available to compatibility callers,
        # but a lexical match may no longer overrule a semantic read/change intent.
        semantic = None
        interpreter = getattr(self, "semantic_interpreter", None)
        if interpreter is not None:
            pending_semantic = self.dialogue_state.get(session_key, None, workspace_scope)
            pending_state = (self.dialogue_state.get_intent_state(pending_semantic.task_id)
                             if pending_semantic else None) or {}
            if pending_semantic:
                pending_state = {**pending_state,
                    "original_request": pending_semantic.original_goal,
                    "question": pending_semantic.question,
                    "task_id": pending_semantic.task_id}
            else:
                recent = self.dialogue_state.get_recent_intent(session_key, workspace_scope, completed_only=True)
                if recent:
                    # Evidence for referents, not a demand to continue the previous
                    # task. The interpreter decides new vs. continue explicitly.
                    pending_state = {"recent_completed": recent}
            semantic = interpreter.interpret(
                goal, history=history, pending=pending_state, allowed_tools=tool_scope,
            )
            check_turn_cancelled()
            record_runtime_event("semantic_decision", relation=semantic.relation,
                                 operation=semantic.operation, intent=semantic.intent_name,
                                 grounded=semantic.grounded, reason=semantic.reason)
            if semantic.grounded and semantic.relation == "cancel" and not semantic.needs_clarification:
                return self._cancel_scoped_tasks(session_key, semantic.control_scope, goal)
            if semantic.is_grounded_conversation:
                outcome = terminal_outcome(self._respond_conversationally(
                    goal, history, semantic_decision=semantic,
                ))
                outcome.grounded_conversation = True
                return outcome
            semantic_resolution = semantic.to_resolution(self.intent_router.registry)
            if semantic_resolution.matched:
                continuing = bool(pending_semantic and semantic.relation in {"continue", "correct"})
                task = (self.dialogue_state.get_task(session_key, pending_semantic.task_id, workspace_scope)
                        if continuing else None)
                if (semantic_resolution.ready
                        and not self.intent_router.resolution_preserves_user_content(goal, semantic_resolution)):
                    # Schema validity and substring grounding are not enough:
                    # a truncated quoted body can satisfy both. Apply the same
                    # literal completeness contract to every direct execution.
                    # Validate the current utterance so an explicit correction
                    # is not forced to retain a superseded old literal.
                    semantic_resolution.question = (
                        "인용하신 원문이나 본문 일부가 실행 명세에서 누락되어 실행하지 않았습니다. "
                        "사용할 파일명과 본문을 확인해 주세요."
                    )
                if semantic_resolution.question:
                    task = task or (self.dialogue_state.get_task(session_key, existing_task_id, workspace_scope)
                                    if existing_task_id else None)
                    task = task or self.dialogue_state.create_task(session_key, goal, workspace_path=workspace_scope)
                    original_goal = pending_semantic.original_goal if continuing else goal
                    self.dialogue_state.delete(session_key, task.task_id)
                    self.dialogue_state.save_intent_state(task.task_id, session_key,
                        semantic_resolution.intent_name, semantic_resolution.slots, original_goal)
                    self.dialogue_state.create(session_key, original_goal, semantic_resolution.question,
                                               history, task.task_id, workspace_scope)
                    if task.status == "awaiting_user":
                        self.dialogue_state.update_task(task.task_id, pending_question=semantic_resolution.question)
                    else:
                        self.dialogue_state.transition_task(task.task_id, "awaiting_user")
                    return ExecutionOutcome(semantic_resolution.question, "awaiting_user", goal,
                                            semantic_resolution.question, task.task_id)
                if semantic_resolution.ready:
                    if continuing:
                        self.dialogue_state.delete(session_key, task.task_id)
                    return self._execute_resolved_intent(
                        semantic_resolution, pending_semantic.original_goal if continuing else goal, session_key,
                        task.task_id if task else (existing_task_id or ""), progress_callback,
                    )
            if semantic.needs_clarification or not semantic.grounded or not semantic.tool_names:
                if semantic.reason.startswith(("semantic_interpretation_failed", "semantic_model_unavailable")):
                    if semantic.reason.endswith((":context_saturated", ":truncated_output")):
                        message = "모델의 입력 또는 출력 길이 한도에 도달해 실행하지 않았습니다. 요청이나 첨부 설명을 나누어 다시 시도해 주세요."
                    elif semantic.reason.endswith(":connection") or semantic.reason == "semantic_model_unavailable":
                        message = "요청 해석 모델에 연결하지 못해 작업을 실행하지 않았습니다. 모델 상태를 확인한 뒤 다시 시도해 주세요."
                    elif semantic.reason.endswith(":timeout"):
                        message = "요청 해석 모델의 응답 시간이 초과되어 작업을 실행하지 않았습니다."
                    else:
                        message = "요청 해석 모델이 유효한 실행 명세를 반환하지 않아 작업을 실행하지 않았습니다."
                    return terminal_outcome(
                        message,
                        "failed",
                    )
                question = semantic.clarification_question or (
                    "요청의 대상과 필요한 작업을 아직 확실히 연결하지 못했어요. "
                    "어떤 결과를 원하시는지 한 번만 더 설명해 주세요."
                )
                continuing = bool(pending_semantic and semantic.relation in {"continue", "correct"})
                task = (self.dialogue_state.get_task(session_key, pending_semantic.task_id, workspace_scope)
                        if continuing else None)
                task = task or (self.dialogue_state.get_task(session_key, existing_task_id, workspace_scope)
                                if existing_task_id else None)
                task = task or self.dialogue_state.create_task(session_key, goal, workspace_path=workspace_scope)
                original_goal = pending_semantic.original_goal if continuing else goal
                self.dialogue_state.delete(session_key, task.task_id)
                self.dialogue_state.create(session_key, original_goal, question,
                                           history, task.task_id, workspace_scope)
                if task.status != "awaiting_user":
                    self.dialogue_state.transition_task(task.task_id, "awaiting_user")
                return ExecutionOutcome(question, "awaiting_user", original_goal, question, task.task_id)
            # A multi-step/unnamed intent continues through the existing observed
            # DAG planner. The tool set comes from semantic discovery, not keywords.
            execution_context += "\n[검증된 요청 의미]\n" + json.dumps({
                "operation": semantic.operation, "relation": semantic.relation,
                "slots": semantic.slots, "tools": semantic.tool_names,
            }, ensure_ascii=False)

        if normalized in {"대기 작업", "대기 작업 목록"}:
            items = self.dialogue_state.list(session_key, workspace_scope)
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
        pending = (
            None if is_new_request
            else self.dialogue_state.get(session_key, selected_task_id, workspace_scope)
        )
        if semantic is not None:
            pending = None

        # 먼저 가벼운 Registry 분류로 완결된 독립 명령과 이미 구조화할 수 있는
        # 후속 명령을 판별한다. 이 결과는 아직 실행하지 않는다. 모든 요청을 곧바로
        # LLM 문맥 해석기에 보내면 해석기 한 번의 오판이 메모장 실행·앱 별칭 추가
        # 같은 명확한 명령까지 확인 질문으로 가로채는 단일 실패점이 된다.
        direct_resolution = (IntentResolution() if semantic is not None
                             else self.intent_router.resolve(goal))
        if (tool_scope is not None and direct_resolution.matched
                and direct_resolution.tool_name not in tool_scope):
            # The specialist supervisor has already fixed the capability domain.
            # Ignore an unrelated lexical hit rather than escaping that contract.
            direct_resolution = IntentResolution()
        if direct_resolution.negated:
            if pending is not None:
                pending_intent = self.dialogue_state.get_intent_state(pending.task_id) or {}
                if pending_intent.get("intent_name") == direct_resolution.intent_name:
                    self.dialogue_state.delete(session_key, pending.task_id)
                    self.dialogue_state.delete_intent_state(pending.task_id)
                    self.dialogue_state.transition_task(
                        pending.task_id, "cancelled", result="사용자가 작업 실행을 명시적으로 금지함",
                    )
                    return ExecutionOutcome(
                        f"진행 중인 작업 {pending.task_id}을 취소했고 실행하지 않겠습니다, 보스.",
                        "cancelled", goal, task_id=pending.task_id,
                    )
                return ExecutionOutcome(
                    f"요청하신 작업은 실행하지 않겠습니다. 기존 대기 작업 {pending.task_id}은 "
                    "다른 작업이므로 그대로 유지합니다, 보스.",
                    "cancelled", goal,
                )
            return terminal_outcome(
                "요청하신 작업은 실행하지 않겠습니다, 보스.", "cancelled",
            )
        deterministic_follow_up = False
        allow_recent_execution = semantic is None and allows_execution_follow_up(history)
        if not pending and not direct_resolution.matched and allow_recent_execution:
            recent_intent = self.dialogue_state.get_recent_intent(
                session_key, workspace_scope
            )
            if (
                recent_intent
                and self.intent_router.is_contextual_follow_up(
                    goal, recent_intent["intent_name"]
                )
            ):
                deterministic_follow_up = True
            elif self.intent_router.is_contextual_follow_up(goal):
                historical = self.intent_router.resolve_from_history(goal, history)
                deterministic_follow_up = bool(historical.matched)

        # Registry가 확정하지 못한 생략/수정 발화만 LLM 문맥 해석기로 복원한 뒤
        # 다시 라우팅한다. 따라서 "빨간색으로 바꿔줘" 같은 발화는 복원된 대상에
        # 맞춰 라우팅되지만, 완결된 독립 도구 명령은 불필요한 LLM 판정에 막히지 않는다.
        resolved = ResolvedRequest(goal, goal)
        may_need_context = ConversationContextResolver._may_depend_on_context(goal)
        ambiguous_target_action = bool(
            not direct_resolution.matched
            and (
                self._EXECUTION_REQUEST_PATTERN.search(goal)
                or self._GENERIC_ACTION_REQUEST_PATTERN.search(goal)
            )
            and re.search(
                r"(?:파일|문서|보고서|사진|이미지|시안|작업|내용)"
                r"(?:을|를|은|는|이|가)?",
                goal,
                re.IGNORECASE,
            )
        )
        if (
            semantic is None
            and
            not pending
            and not is_new_request
            and not deterministic_follow_up
            and not direct_resolution.matched
            and (may_need_context or ambiguous_target_action)
        ):
            context_resolver = getattr(self, "context_resolver", None)
            if context_resolver is not None:
                resolved = context_resolver.resolve(goal, history, session_key)
            if resolved.needs_clarification:
                question = (
                    resolved.clarification_question
                    or "어떤 대상을 말씀하시는지 조금 더 구체적으로 알려주세요."
                )
                task = (
                    self.dialogue_state.get_task(
                        session_key, existing_task_id, workspace_scope
                    ) if existing_task_id else None
                ) or self.dialogue_state.create_task(
                    session_key, goal, workspace_path=workspace_scope
                )
                pending_question = self.dialogue_state.create(
                    session_key, goal, question, history, task.task_id,
                    workspace_scope,
                )
                self.dialogue_state.transition_task(task.task_id, "awaiting_user")
                return ExecutionOutcome(
                    f"{question}\n대기 작업 ID: {pending_question.task_id}",
                    "awaiting_user", goal, question, pending_question.task_id,
                )
            goal = resolved.resolved_request
            if goal != resolved.original_request:
                print(
                    f"[Context] 요청 해석: {resolved.original_request!r} → {goal!r} "
                    f"(confidence={resolved.confidence:.2f})"
                )
                direct_resolution = self.intent_router.resolve(goal)

        tool_loadout = getattr(self, "tool_loadout", None)
        if tool_loadout is None:
            tool_loadout = ToolLoadoutSelector(self.intent_router.registry)
            self.tool_loadout = tool_loadout
        reasoning_policy = getattr(self, "reasoning_policy", None)
        if reasoning_policy is None:
            reasoning_policy = ReasoningPolicy()
            self.reasoning_policy = reasoning_policy
        loadout = tool_loadout.select(
            goal, direct_resolution, allowed_tools=tool_scope,
        )
        effort = reasoning_policy.decide(
            direct_resolution, required_tool_count=len(loadout.tool_names),
            has_pending_task=False,
            risky=any(
                (contract := self.intent_router.registry.get_capability(name))
                and contract.side_effect in {"external_send"}
                for name in loadout.tool_names
            ),
        )
        record_runtime_event(
            "routing_decision", intent=direct_resolution.intent_name,
            confidence=direct_resolution.confidence, alternatives=direct_resolution.alternatives,
            loadout=list(loadout.tool_names), loadout_reason=loadout.reason,
            reasoning_level=effort.level, use_planner=effort.use_planner,
        )
        if direct_resolution.matched:
            print(
                f"[Router] intent={direct_resolution.intent_name} "
                f"type={direct_resolution.request_type} confidence={direct_resolution.confidence:.2f} "
                f"reason={direct_resolution.routing_reason} "
                f"alternatives={direct_resolution.alternatives}"
            )
        # An independently recognisable execution request starts a new task instead
        # of being consumed as an answer to an unrelated/stale pending question.
        if (pending and not selected_task_id and direct_resolution.explicit
                and direct_resolution.execution_requested):
            self.dialogue_state.delete(session_key, pending.task_id)
            self.dialogue_state.delete_intent_state(pending.task_id)
            self.dialogue_state.transition_task(
                pending.task_id, status="cancelled", result="새로운 명시적 요청으로 대체됨"
            )
            pending = None
        elif (pending and not selected_task_id
              and self._is_independent_declaration(supplied_answer)):
            # Persistent preferences and definitions are complete new turns, not an
            # answer to an unrelated slot question.  Let the normal conversation and
            # memory pipeline process them instead of trapping the user in a loop.
            self.dialogue_state.delete(session_key, pending.task_id)
            self.dialogue_state.delete_intent_state(pending.task_id)
            self.dialogue_state.transition_task(
                pending.task_id, status="cancelled", result="새로운 사용자 규칙으로 대체됨"
            )
            pending = None
        agent_task_id = pending.task_id if pending else (existing_task_id or "")
        intent_resolution = IntentResolution()

        if pending:
            if supplied_answer.strip().lower() in {"취소", "그만", "중단", "cancel", "stop"}:
                self.dialogue_state.delete(session_key, pending.task_id)
                self.dialogue_state.transition_task(
                    pending.task_id, "cancelled", result="사용자 취소"
                )
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
                original_scope = analyze_utterance_scope(intent_state["original_request"])
                if (original_scope.conditional and self.intent_router.resolve(
                        intent_state["original_request"]
                ).request_type in {"change", "execute", "external_send"}):
                    # A short slot answer ("응", a date, or a recipient) is
                    # not an observation satisfying an outstanding condition.
                    # A new explicit action is handled above; otherwise keep
                    # this contract pending instead of dropping its predicate.
                    return ExecutionOutcome(
                        f"{pending.question}\n대기 작업 ID: {pending.task_id}",
                        "awaiting_user", intent_state["original_request"],
                        pending.question, pending.task_id,
                    )
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
                        pending.conversation_history, pending.task_id, workspace_scope,
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
                context_resolver = getattr(self, "context_resolver", None)
                if context_resolver is not None:
                    resolved = context_resolver.resolve(goal, history, session_key)
                    if resolved.needs_clarification:
                        question = (
                            resolved.clarification_question
                            or "어떤 대상을 말씀하시는지 조금 더 구체적으로 알려주세요."
                        )
                        next_pending = self.dialogue_state.create(
                            session_key, pending.original_goal, question, history,
                            pending.task_id, workspace_scope,
                        )
                        self.dialogue_state.transition_task(
                            pending.task_id, "awaiting_user"
                        )
                        return ExecutionOutcome(
                            f"{question}\n대기 작업 ID: {next_pending.task_id}",
                            "awaiting_user", pending.original_goal, question,
                            next_pending.task_id,
                        )
                    goal = resolved.resolved_request
            if progress_callback:
                progress_callback(f"확인했습니다, 보스. 작업 {pending.task_id}을 이어서 진행하겠습니다.")

        if not pending:
            intent_resolution = direct_resolution
            if intent_resolution.matched:
                recent_intent = self.dialogue_state.get_recent_intent(session_key, workspace_scope)
                if (
                    recent_intent
                    and allow_recent_execution
                    and recent_intent["intent_name"] == intent_resolution.intent_name
                    and self.intent_router.is_contextual_follow_up(
                        goal, intent_resolution.intent_name
                    )
                ):
                    intent_resolution = self.intent_router.resolve(
                        goal,
                        recent_intent["intent_name"],
                        recent_intent["slots"],
                    )
            if (intent_resolution.matched and intent_resolution.tool_name
                    and not intent_resolution.execution_requested
                    and not intent_resolution.capability_response):
                return terminal_outcome(
                    "대상은 들었지만 어떤 작업을 할지 명확히 인식하지 못했습니다. "
                    "원하는 동작을 다시 말씀해 주세요, 보스.",
                )
            if not intent_resolution.matched and allow_recent_execution:
                recent_intent = self.dialogue_state.get_recent_intent(session_key, workspace_scope)
                if (recent_intent and self.intent_router.is_contextual_follow_up(
                        goal, recent_intent["intent_name"]
                )):
                    intent_resolution = self.intent_router.resolve(
                        goal, recent_intent["intent_name"], recent_intent["slots"]
                    )
                elif self.intent_router.is_contextual_follow_up(goal):
                    intent_resolution = self.intent_router.resolve_from_history(goal, history)
            if intent_resolution.capability_response:
                return terminal_outcome(intent_resolution.capability_response)
            if intent_resolution.ambiguous:
                return terminal_outcome(
                    intent_resolution.question,
                    pending_question=intent_resolution.question,
                )
            if intent_resolution.matched and intent_resolution.question:
                task = (
                    self.dialogue_state.get_task(
                        session_key, existing_task_id, workspace_scope
                    ) if existing_task_id else None
                ) or self.dialogue_state.create_task(
                    session_key, goal, workspace_path=workspace_scope
                )
                self.dialogue_state.save_intent_state(
                    task.task_id, session_key, intent_resolution.intent_name,
                    intent_resolution.slots, goal,
                )
                self.dialogue_state.update_task(
                    task.task_id, context_confidence=intent_resolution.confidence
                )
                pending = self.dialogue_state.create(
                    session_key, goal, intent_resolution.question, history, task.task_id,
                    workspace_scope,
                )
                self.dialogue_state.transition_task(task.task_id, "awaiting_user")
                return ExecutionOutcome(
                    f"{intent_resolution.question}\n대기 작업 ID: {pending.task_id}",
                    "awaiting_user", goal, intent_resolution.question, pending.task_id,
                )

        if intent_resolution.ready:
            if self.intent_router.resolution_preserves_user_content(goal, intent_resolution):
                return self._execute_resolved_intent(
                    intent_resolution, goal, session_key, agent_task_id, progress_callback
                )
            # A fast-path intent that dropped quoted or labelled content is not
            # executable. Let the constrained Planner reconstruct the complete
            # Registry call instead of creating a superficially valid empty file.
            intent_resolution = IntentResolution()

        # 선언형 Intent가 없는 요청도 Registry 설명과 의미 있게 맞는 실행 요청이면
        # 동적 Tool Loadout을 거쳐 Planner가 처리한다. 예전에는 이 지점에서 모두
        # 일반 대화로 종료되어 등록된 도구 대부분이 사실상 접근 불가능했다.
        if (not intent_resolution.matched
                and hasattr(self, "llm")
                and hasattr(self, "tool_executor")):
            if semantic is None and not self._should_attempt_registry_execution(goal, tool_scope):
                return terminal_outcome(self._respond_conversationally(goal, history))

        unsupported = self._unsupported_capability_message(goal) if semantic is None else ""
        if unsupported:
            return terminal_outcome(unsupported)
        if not agent_task_id:
            agent_task_id = self.dialogue_state.create_task(
                session_key, goal, workspace_path=workspace_scope
            ).task_id
        self.current_agent_task_id = agent_task_id
        if not self.dialogue_state.transition_task(agent_task_id, "running"):
            return ExecutionOutcome(
                "현재 작업 상태에서는 실행을 시작할 수 없습니다, 보스.",
                "failed", goal, task_id=agent_task_id,
            )
        with self._control_condition:
            self._task_controls[agent_task_id] = {"cancel": False, "pause": False}
        self._progress_callback = progress_callback
        self._emit_progress(f"작업 {agent_task_id}: 요청을 이해했습니다. 작업 계획을 준비하고 있습니다.")
        self.initialize(goal, session_id)

        # 1. 초기 Planning
        initial_context = self.build_context()
        if str(execution_context or "").strip():
            # Specialist contracts and memories belong to the planner context,
            # not to the user's utterance. Mixing machine metadata into `goal`
            # poisoned intent routing with unrelated tool names (for example a
            # Word request becoming ambiguous with HWPX/PDF).
            initial_context = (
                f"{initial_context}\n\n[전문가 작업공간 실행 맥락]\n"
                f"{str(execution_context).strip()}"
            )
        allowed_tools = (list(semantic.tool_names) if semantic is not None
                         else self._allowed_tools_for_goal(goal, tool_scope))
        required_tools: list[str] = []
        if semantic is not None:
            # These are the validated operations selected by the semantic
            # interpreter, not the broader discovery shortlist. Every selected
            # operation must survive planning, including independent branches.
            required_tools = list(dict.fromkeys(semantic.tool_names))
        elif direct_resolution.compound:
            required_tools = list(dict.fromkeys(
                name for name in (
                    [direct_resolution.tool_name]
                    + [
                        str(item.get("tool_name", "")).strip()
                        or str(item.get("tool", "")).strip()
                        for item in direct_resolution.alternatives
                        if isinstance(item, dict)
                    ]
                )
                if name and (allowed_tools is None or name in allowed_tools)
            ))
        planning_service = getattr(self, "planning_service", None)
        if planning_service is None:
            planning_service = PlanningService(self.planner)
            self.planning_service = planning_service
        try:
            self.current_plan = planning_service.create(
                goal, initial_context, allowed_tools, required_tools,
            )
            check_turn_cancelled()
        except ToolCancelledError as exc:
            self.dialogue_state.transition_task(agent_task_id, "cancelled", result=str(exc))
            with self._control_condition:
                self._task_controls.pop(agent_task_id, None)
            self.current_agent_task_id = ""
            self._progress_callback = None
            return ExecutionOutcome(str(exc), "cancelled", goal, task_id=agent_task_id)
        except Exception as exc:
            # A planning/provider failure is a real failed turn.  Never convert
            # it to a vague tool-free fallback or leave a task stuck in running.
            user_message = getattr(exc, "user_message", None)
            if callable(user_message):
                response = user_message()
            elif isinstance(exc, PlanningError):
                response = (
                    "실행 가능한 작업 계획을 만들지 못했습니다. 실제 작업은 수행하지 "
                    "않았으며, 요청을 더 작은 단계로 나누거나 도구 상태를 확인해야 합니다."
                )
            else:
                response = "작업 계획 생성 중 오류가 발생해 실제 작업을 수행하지 않았습니다."
            print(f"[Executor] 계획 생성 실패: {type(exc).__name__}: {exc}")
            self.dialogue_state.transition_task(
                agent_task_id, "failed", result=response,
            )
            with self._control_condition:
                self._task_controls.pop(agent_task_id, None)
            self.current_agent_task_id = ""
            self._progress_callback = None
            return ExecutionOutcome(response, "failed", goal, task_id=agent_task_id)
        planned_tasks = self.current_plan.steps
        self.dialogue_state.update_task(
            agent_task_id,
            plan=self.current_plan.to_dict()["steps"],
            plan_id=self.current_plan.plan_id,
        )
        model_role_router = getattr(self, "model_role_router", None)
        if model_role_router is not None:
            planned_tools = [task.tool_name for task in planned_tasks if task.tool_name]
            specialist_role = model_role_router.route(
                allowed_tools=allowed_tools or planned_tools
            )
            self.reasoning_llm = get_llm_client(specialist_role)
            self._default_reasoning_prompt = self.reasoning_llm.system_prompt
            print(
                f"[ModelRouter] role={specialist_role}, "
                f"model={getattr(self.reasoning_llm, 'model', 'external')}"
            )
        self._emit_progress("작업 계획을 세웠습니다. 실행을 시작하겠습니다.")

        # 오래된 단위 테스트나 외부 어댑터가 최소 Executor 객체에 빈 계획을
        # 주입하던 경우에만 기존 finalize 계약을 유지한다. 실제 런타임에서
        # 빈 계획을 성공으로 처리하면 실행하지 않은 작업을 완료했다고 말할 수
        # 있으므로, 완전한 Executor에서는 명시적으로 실패시킨다.
        if not planned_tasks:
            if not hasattr(self, "tool_executor"):
                response = self.finalize()
                self.dialogue_state.transition_task(
                    agent_task_id, "completed", result=response
                )
                return ExecutionOutcome(
                    response, "completed", goal, task_id=agent_task_id
                )
            response = (
                "실행 가능한 작업 계획을 만들지 못했습니다. 실제 작업은 수행되지 "
                "않았으며 완료로 기록하지 않았습니다."
            )
            self.dialogue_state.transition_task(
                agent_task_id, "failed", result=response
            )
            return ExecutionOutcome(response, "failed", goal, task_id=agent_task_id)

        # 2. 모든 계획 작업은 동일한 계약 기반 DAG 경로를 사용한다.  이전
        # run_iteration 루프는 하위 호환 메서드로만 남겨 두며 사용자 실행
        # 경로에서는 더 이상 별도의 JSON/Tool 선택 루프를 만들지 않는다.
        try:
            run = self.execute_plan_dag(
                self.current_plan, allowed_tool_names=allowed_tools,
            )
            return self._finish_plan_run(run, goal=goal, task_id=agent_task_id)
        except ToolCancelledError as exc:
            self.dialogue_state.transition_task(agent_task_id, "cancelled", result=str(exc))
            return ExecutionOutcome(str(exc), "cancelled", goal, task_id=agent_task_id)
        finally:
            with self._control_condition:
                self._task_controls.pop(agent_task_id, None)
            self.current_agent_task_id = ""
            self._progress_callback = None

    def _serialized_current_plan(self, scratchpad_tasks) -> List[Dict[str, Any]]:
        if self.current_plan is None:
            return [{"id": task.id, "description": task.description, "status": task.status}
                    for task in scratchpad_tasks]
        status_by_description = {task.description: task.status for task in scratchpad_tasks}
        payload = self.current_plan.to_dict()["steps"]
        for step in payload:
            legacy = status_by_description.get(step["description"])
            if legacy:
                step["status"] = {
                    "completed": "completed", "failed": "failed", "in_progress": "running"
                }.get(legacy, legacy)
        return payload

    def execute_plan_dag(
        self, plan: PlanDAG,
        approval_callback: Optional[Callable[[PlanStep], bool]] = None,
        approved_step_ids: Optional[List[str]] = None,
        replan_callback: Optional[Callable[[PlanDAG, PlanStep, ToolRunResult], Optional[PlanDAG]]] = None,
        allowed_tool_names: Optional[List[str]] = None,
    ) -> PlanRunResult:
        """Execute a prevalidated DAG through Tool, contract and verification boundaries."""
        contracts: Dict[str, Any] = {}

        def ensure_contract(step: PlanStep):
            existing = contracts.get(step.id)
            if existing is not None:
                return existing
            supervisor = getattr(self, "supervisor", None) or get_supervisor_runtime()
            self.supervisor = supervisor
            persisted = supervisor.store.find_for_step(
                self.current_agent_task_id, plan.plan_id, step.id
            )
            if persisted is not None:
                contracts[step.id] = persisted
                return persisted
            plugin_registry = getattr(self.tool_executor, "plugin_registry", None)
            if plugin_registry is None:
                plugin_registry = getattr(self.intent_router, "registry", None)
            capability = (
                plugin_registry.get_capability(step.tool_name)
                if plugin_registry is not None else None
            )
            permissions = list(getattr(capability, "required_permissions", ()) or ())
            permission = TOOL_PERMISSION_MAP.get(step.tool_name)
            if permission and permission not in permissions:
                permissions.append(permission)
            expected = list(step.expected_artifacts)
            criteria = [
                AcceptanceCriterion(
                    str(item.get("kind") or "artifact"),
                    str(item.get("description") or item.get("kind") or "필수 산출물"),
                    "artifact",
                ) for item in expected
            ]
            if not criteria:
                criteria.append(AcceptanceCriterion(
                    "*", "도구 실행 검증 근거", "evidence"
                ))
            timeout = float(getattr(capability, "timeout_seconds", 120.0) or 120.0)
            max_retries = int(getattr(capability, "max_retries", 0) or 0)
            role_router = getattr(self, "model_role_router", None)
            specialist = (
                role_router.route(allowed_tools=[step.tool_name])
                if role_router is not None else "general"
            )
            contract = supervisor.create_contract(
                goal=step.description or f"{step.tool_name} 실행",
                specialist=specialist,
                input_contract={"step_id": step.id, "tool": step.tool_name, "schema": _safe_contract_value(
                    getattr(capability, "input_schema", {})
                ), "value": _safe_contract_value(step.tool_input)},
                output_contract={"schema": _safe_contract_value(
                    getattr(capability, "output_schema", {})
                ), "expected_artifacts": expected,
                    "verification": _safe_contract_value(step.verification)},
                acceptance_criteria=criteria, allowed_tools=[step.tool_name],
                required_permissions=permissions,
                resource_budget=ResourceBudget(timeout_seconds=timeout),
                retry_policy=RetryPolicy(
                    max_attempts=max(1, step.retry_budget + 1, max_retries + 1),
                    strategies=tuple(step.retry_strategies or ["retry"]),
                    escalate_after=max(1, step.retry_budget + 1),
                ),
                escalation_target="user" if step.requires_approval else "planner",
                parent_id=self.current_agent_task_id, plan_id=plan.plan_id,
            )
            if step.requires_approval:
                self.supervisor.transition(contract, ContractStatus.AWAITING_APPROVAL)
            contracts[step.id] = contract
            return contract

        def execute(step: PlanStep, strategy: str) -> ToolRunResult:
            contract = ensure_contract(step)
            if self.supervisor.cancellation_requested(contract):
                if contract.status not in self.supervisor._TERMINAL:
                    self.supervisor.transition(contract, ContractStatus.CANCELLED,
                                               failure_reason="사용자가 작업을 취소했습니다.")
                return ToolRunResult.failed(tool_name=step.tool_name or step.id,
                                            error="사용자가 작업을 취소했습니다.")
            if contract.status == ContractStatus.AWAITING_APPROVAL:
                # PlanCoordinator invokes execute only after its approval callback
                # accepted the step, so the contract can now enter the run state.
                contract.status = ContractStatus.QUEUED
                self.supervisor.store.save(contract)
            self.supervisor.begin_attempt(contract)
            if not step.tool_name:
                return ToolRunResult.failed(
                    tool_name=step.id, error="실행 단계에 Tool이 지정되지 않았습니다."
                )
            started = time.perf_counter()
            try:
                with self.supervisor.resource_guard(contract):
                    raw_result = self.tool_executor.execute_tool(
                        step.tool_name, dict(step.tool_input)
                    )
                if self.supervisor.cancellation_requested(contract):
                    self.supervisor.transition(contract, ContractStatus.CANCELLED,
                                               failure_reason="실행 중 취소 요청을 반영했습니다.")
                    return ToolRunResult.failed(tool_name=step.tool_name,
                                                error="실행 중 취소되었습니다.")
            except (MemoryError, TimeoutError) as exc:
                raw_result = ToolRunResult.failed(
                    tool_name=step.tool_name,
                    error=f"작업 계약 자원 제한: {exc}",
                )
            duration_ms = (time.perf_counter() - started) * 1000.0
            return self.build_tool_run_result(
                None, step.tool_name, dict(step.tool_input), raw_result, duration_ms
            )

        def verify(step: PlanStep, candidate: ToolRunResult) -> ToolRunResult:
            # Tool이 구조적으로 UNVERIFIED를 반환했다면 범용 문자열 verifier로
            # 다시 해석해 성공으로 승격하지 않는다. 특히 외부 메시지는 Enter가
            # 전달됐지만 새 발신 말풍선을 보지 못한 상태가 있을 수 있으며, 이를
            # 성공으로 바꾸면 실제 미전송/중복 전송을 구별할 수 없게 된다.
            if candidate.status == ToolRunStatus.UNVERIFIED:
                verified = candidate
            elif candidate.succeeded and candidate.evidence:
                verified = candidate
            else:
                verification = self.verifier.verify(step.tool_name, step.tool_input, candidate.raw_output)
                verified = ToolRunResult.from_verification(
                    tool_name=step.tool_name, raw_output=candidate.raw_output,
                    verification=verification, duration_ms=candidate.duration_ms,
                )
            contract = ensure_contract(step)
            if contract.status == ContractStatus.CANCELLED:
                return candidate
            self.supervisor.verify(
                contract,
                artifacts=[asdict(item) for item in verified.artifacts],
                evidence=[asdict(item) for item in verified.evidence],
                failure_reason="" if verified.succeeded else str(
                    verified.error or verified.raw_output
                ),
            )
            return verified

        if replan_callback is None:
            def replan_callback(current, failed, result):
                # 외부 전송은 결과가 불확실한 순간 재실행하면 같은 메시지가 두 번
                # 전송될 수 있다. UNVERIFIED와 external_send는 자동 재계획하지 않고
                # 관찰된 상태를 그대로 사용자에게 돌려준다.
                registry = getattr(self.intent_router, "registry", None)
                capability = (
                    registry.get_capability(failed.tool_name)
                    if registry is not None and failed.tool_name else None
                )
                if (
                    result.status in {ToolRunStatus.UNVERIFIED, ToolRunStatus.CANCELLED}
                    or getattr(capability, "side_effect", "") == "external_send"
                ):
                    return None
                return self.planner.replan_from_observation(
                    current, failed, result.raw_output, self.build_context(),
                    (list(allowed_tool_names) if allowed_tool_names is not None
                     else self._allowed_tools_for_goal(current.goal)),
                )
        coordinator = getattr(self, "plan_coordinator", None) or PlanCoordinator()
        self.plan_coordinator = coordinator
        if approved_step_ids is not None:
            outcome = coordinator.resume_approved(
                plan, approved_step_ids, execute, verify, replan=replan_callback,
            )
        else:
            outcome = coordinator.run(
                plan, execute, verify, approve=approval_callback, replan=replan_callback,
            )
        # Approval can stop the coordinator before execute() has been called.
        # Materialize those contracts so the Command Center exposes the actual
        # pending decision, its permission scope and its escalation target.
        for step in outcome.plan.steps:
            if step.status.value == "awaiting_approval":
                ensure_contract(step)
        if self.current_agent_task_id:
            self.dialogue_state.update_task(
                self.current_agent_task_id, plan=outcome.plan.to_dict()["steps"],
                plan_id=outcome.plan.plan_id,
                retry_count=sum(step.attempts - 1 for step in outcome.plan.steps),
                verification_status=outcome.status,
            )
            if outcome.status == "awaiting_approval":
                self.dialogue_state.transition_task(
                    self.current_agent_task_id, "awaiting_approval",
                    pending_question="사람 승인이 필요한 실행 단계가 대기 중입니다.",
                )
        return outcome

    def _finish_plan_run(self, outcome: PlanRunResult, *, goal: str,
                         task_id: str) -> ExecutionOutcome:
        """Persist and present one evidence-bearing DAG result without LLM claims."""
        steps = outcome.plan.steps
        completed = sum(step.status.value == "completed" for step in steps)
        failed = sum(step.status.value in {"failed", "blocked"} for step in steps)
        retries = sum(max(0, step.attempts - 1) for step in steps)
        artifacts = [asdict(item) for result in outcome.results.values()
                     for item in result.artifacts]
        evidence = [asdict(item) for result in outcome.results.values()
                    for item in result.evidence]
        last_tool = next((step.tool_name for step in reversed(steps)
                          if step.status.value == "completed"), "")

        if outcome.status == "awaiting_approval":
            pending_steps = [
                step for step in steps if step.id in set(outcome.awaiting_approval)
            ]
            response = self._approval_request_message(pending_steps)
            self.dialogue_state.update_task(
                task_id, result=response, pending_question=response,
                artifacts=artifacts, evidence=evidence, last_tool=last_tool,
                retry_count=retries, plan=outcome.plan.to_dict()["steps"],
                plan_id=outcome.plan.plan_id, verification_status=outcome.status,
            )
            return ExecutionOutcome(
                response, "awaiting_approval", goal, question=response,
                task_id=task_id, retry_count=retries,
                completed_steps=completed, failed_steps=failed,
            )

        successful_outputs: list[str] = []
        for step in steps:
            result = outcome.results.get(step.id)
            if result is not None and result.succeeded and str(result.raw_output).strip():
                successful_outputs.append(str(result.raw_output).strip())
        successful_outputs = list(dict.fromkeys(successful_outputs))
        if outcome.status == "completed":
            response = "\n".join(successful_outputs[-5:]) or "검증된 모든 작업을 완료했습니다."
            status = "completed"
        elif outcome.status == "cancelled":
            response = ("이미 완료된 단계의 결과를 보존하고 나머지 실행을 중단했습니다."
                        if completed else "실행을 취소했습니다.")
            status = "partial" if completed else "cancelled"
        else:
            errors = []
            for step in steps:
                result = outcome.results.get(step.id)
                if step.status.value in {"failed", "blocked"}:
                    detail = str((result.error if result else "") or
                                 (result.raw_output if result else "") or step.description)
                    errors.append(f"{step.description}: {detail}")
            response = "작업을 완료하지 못했습니다. " + "; ".join(errors[:5])
            status = "partial" if completed else "failed"
        self.dialogue_state.transition_task(
            task_id, status, result=response, artifacts=artifacts,
            evidence=evidence, last_tool=last_tool, retry_count=retries,
            plan=outcome.plan.to_dict()["steps"], plan_id=outcome.plan.plan_id,
            verification_status=outcome.status,
        )
        return ExecutionOutcome(
            response, status, goal, task_id=task_id, retry_count=retries,
            completed_steps=completed, failed_steps=failed,
            tool_result=(next(iter(outcome.results.values()))
                         if len(outcome.results) == 1 else None),
            tool_results=tuple(
                outcome.results[step.id]
                for step in steps
                if step.id in outcome.results
            ),
        )

    @staticmethod
    def _present_terminal_state(
        *,
        response: str,
        cancelled: bool,
        terminal_error: Optional[str],
        completed_steps: int,
        failed_steps: int,
        retry_count: int,
    ) -> tuple[str, str]:
        """실행 종료 상태를 사용자 문구와 동일한 단일 계약으로 변환한다."""
        if cancelled:
            return (
                "cancelled",
                terminal_error or "사용자 요청으로 작업을 취소했습니다.",
            )
        if terminal_error:
            if completed_steps:
                return (
                    "partial",
                    f"일부 작업만 완료되었습니다({completed_steps}단계 완료, "
                    f"{failed_steps or 1}단계 실패). {terminal_error}",
                )
            retry_note = f" 재시도 {retry_count}회 후에도" if retry_count else ""
            return "failed", f"{retry_note.strip()} {terminal_error}".strip()
        if retry_count:
            return (
                "completed",
                f"재시도 {retry_count}회 후 완료했습니다.\n{response}",
            )
        return "completed", response

    def has_pending_request(self, session_id: Optional[str] = None) -> bool:
        return bool(self.dialogue_state.list(
            session_id or "default", self._workspace_scope()
        ))

    def enqueue_goal(self, goal: str, session_id: Optional[str] = None, priority: int = 0):
        """현재 실행과 분리해 새 목표를 영속 대기열에 등록한다."""
        return self.dialogue_state.create_task(
            session_id or "default", goal, priority,
            workspace_path=self._workspace_scope(),
        )

    def _workspace_scope(self) -> str:
        turn_context = current_turn_context()
        if turn_context is not None:
            return turn_context.workspace_path
        manager = getattr(self, "workspace_manager", None)
        if manager is None or not manager.is_set():
            return ""
        return manager.get_workspace_path() or ""

    def _emit_progress(self, message: str):
        if self._progress_callback:
            try:
                self._progress_callback(message)
            except Exception as exc:
                print(f"[Executor] 진행 상황 callback 오류: {exc}")

    @staticmethod
    def _is_contextual_approval_command(text: str) -> bool:
        """Return whether text is an unambiguous approval utterance.

        This deliberately excludes vague words such as ``계속``.  A short
        approval may select a task only when the current session has exactly
        one approval-pending task; ambiguity is resolved by the caller rather
        than by an LLM guess.
        """
        normalized = re.sub(r"[.!?。！？]+$", "", str(text).strip().lower())
        normalized = re.sub(r"\s+", "", normalized)
        return bool(re.fullmatch(
            r"(?:승인|허용)(?:해|해줘|해주세요|해요|합니다|할게|할게요|"
            r"할께|할께요|하겠습니다)?",
            normalized,
        ))

    @staticmethod
    def _approval_request_message(steps: List[PlanStep]) -> str:
        """Describe approval scope without exposing internal intent IDs."""
        reasons = list(dict.fromkeys(
            step.approval_reason.strip() for step in steps
            if step.approval_reason.strip()
        ))
        lines = [reasons[0] if len(reasons) == 1 else
                 "다음 외부 작업을 실행하기 전에 승인이 필요합니다."]
        label_map = {
            "provider": "서비스", "recipient": "대상", "target": "대상",
            "to": "대상", "message": "내용", "content": "내용", "text": "내용",
        }
        visible: list[str] = []
        for step in steps:
            for key, value in step.tool_input.items():
                key_text = str(key).casefold()
                if any(token in key_text for token in
                       ("password", "passwd", "secret", "token", "auth", "credential")):
                    continue
                if key_text not in label_map or isinstance(value, (dict, list, tuple, set)):
                    continue
                text = str(value).strip()
                if text:
                    visible.append(f"{label_map[key_text]}: {text[:200]}")
        for item in dict.fromkeys(visible):
            lines.append(item)
        lines.append("계속하려면 ‘승인’이라고 말씀해 주세요.")
        return "\n".join(lines)

    @staticmethod
    def _is_independent_declaration(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
        if re.search(r"(?:기억해(?:\s*둬)?|잊지\s*마)", normalized):
            return True
        if not re.match(r"^(?:앞으로|이제부터|항상|기본적으로)\b", normalized):
            return False
        return bool(re.search(
            r"(?:말하면|부르면|뜻|의미|규칙|설정|사용|적용|알아들|기억)", normalized,
        ))

    @staticmethod
    def is_control_command(text: str) -> bool:
        from core.semantic_request import explicit_control
        if explicit_control(text) is not None:
            return True
        normalized = text.strip().lower()
        if normalized in {"작업 목록", "전체 작업 목록", "대기 작업", "대기 작업 목록"}:
            return True
        if Executor._is_contextual_approval_command(normalized):
            return True
        return bool(re.fullmatch(
            r"(?:작업\s*)?[0-9a-f]{8}(?:\s*(?:상태|승인|취소|중단|일시정지|재개|"
            r"우선순위\s*-?\d+|수정\s*[:：].+))?",
            normalized, re.S,
        ))

    @staticmethod
    def _intent_signature(resolution: IntentResolution) -> tuple[str, str, str]:
        """Return a stable signature for deduplicating equivalent direct intents."""
        def normalize_recipient(value: str) -> str:
            text = re.sub(r"\s+", " ", value).strip().casefold()
            text = text.strip(" \t\r\n'\"`.,!?·ㆍ:;()[]{}<>《》〈〉「」『』")
            text = re.sub(r"(?:에게|한테|께)$", "", text).strip()
            # Never guess that a trailing Korean syllable is a case marker.
            # Contact names such as ``김하이`` and ``김하`` may both exist, so
            # recipient deduplication must preserve the exact confirmed name.
            return text

        def normalize(value: Any, key: str = "") -> Any:
            if isinstance(value, dict):
                return {
                    str(item_key): normalize(item, str(item_key).casefold())
                    for item_key, item in sorted(value.items())
                }
            if isinstance(value, (list, tuple)):
                return [normalize(item, key) for item in value]
            if isinstance(value, str):
                if key in {"recipient", "to", "recipient_name"}:
                    return normalize_recipient(value)
                return re.sub(r"\s+", " ", value).strip().casefold()
            return value

        return (
            resolution.intent_name,
            resolution.tool_name,
            json.dumps(normalize(resolution.slots), ensure_ascii=False, sort_keys=True),
        )

    def _approval_task_resolution(self, task) -> IntentResolution:
        """Resolve an approval from its latest persisted Registry state.

        A slot-fill task's original goal can be intentionally incomplete.  Its
        persisted ``intent_name`` and ``slots`` are the canonical, most recent
        user-confirmed state and must be preferred over reparsing the old goal.
        """
        intent_name = str(getattr(task, "intent_name", "") or "")
        slots = dict(getattr(task, "slots", None) or {})
        if intent_name and slots:
            schema = next(
                (
                    intent
                    for _plugin, intent in self.intent_router.registry.get_all_intents()
                    if intent.name == intent_name
                ),
                None,
            )
            if schema is not None:
                missing = [
                    slot.name for slot in schema.slots
                    if slot.required and (
                        slot.name not in slots
                        or slots[slot.name] is None
                        or (isinstance(slots[slot.name], str)
                            and not slots[slot.name].strip())
                    )
                ]
                errors = self.intent_router.registry.validate_tool_call(
                    schema.tool_name, slots, schema.request_type,
                )
                if not missing and not errors:
                    return IntentResolution(
                        matched=True,
                        intent_name=schema.name,
                        tool_name=schema.tool_name,
                        slots=slots,
                        explicit=True,
                        execution_requested=True,
                        confidence=float(getattr(task, "context_confidence", 0.0) or 0.9),
                        domain=schema.domain,
                        action=schema.action,
                        request_type=schema.request_type,
                        freshness=schema.freshness,
                        requires_sources=schema.requires_sources,
                        routing_reason="persisted_registry_approval",
                    )
        return self.intent_router.resolve(task.goal)

    def _cancel_equivalent_approval_tasks(
        self, session_id: str, workspace_scope: str,
        resolution: IntentResolution, exclude_task_id: str = "",
    ) -> int:
        """Replace stale duplicate approvals with the newest canonical request.

        An earlier Planner path may have persisted an invalid or differently shaped
        plan for the same external send.  Keeping both forces the user to select
        between duplicates and risks executing the stale plan.  Only exact Registry
        intent/tool/slot matches are cancelled; unrelated pending sends remain.
        """
        wanted = self._intent_signature(resolution)
        cancelled = 0
        for task in self.dialogue_state.list_tasks(
            session_id, include_finished=False, workspace_path=workspace_scope,
        ):
            if task.status != "awaiting_approval" or task.task_id == exclude_task_id:
                continue
            existing = self._approval_task_resolution(task)
            capability = (
                self.intent_router.registry.get_capability(existing.tool_name)
                if existing.ready else None
            )
            if (not existing.ready
                    or not capability
                    or capability.side_effect != "external_send"
                    or self._intent_signature(existing) != wanted):
                continue
            self.dialogue_state.transition_task(
                task.task_id, "cancelled", result="동일한 새 요청으로 대체됨",
                pending_question="",
            )
            self.dialogue_state.delete(session_id, task.task_id)
            self.dialogue_state.delete_intent_state(task.task_id)
            cancelled += 1
        return cancelled

    def _coalesce_equivalent_approval_tasks(
        self, session_id: str, pending: List[Any],
    ) -> List[Any]:
        """Collapse only Registry-proven equivalent stale approvals.

        ``list_tasks`` returns newest tasks first.  The newest canonical task is
        retained, while older tasks are cancelled only when intent, tool and
        normalized slots all match.  Different recipients or message bodies are
        deliberately kept separate and continue to require an explicit ID.
        """
        signatures: set[tuple[str, str, str]] = set()
        survivors: List[Any] = []
        for task in pending:
            resolution = self._approval_task_resolution(task)
            capability = (
                self.intent_router.registry.get_capability(resolution.tool_name)
                if resolution.ready else None
            )
            if not resolution.ready or not capability or capability.side_effect != "external_send":
                survivors.append(task)
                continue
            signature = self._intent_signature(resolution)
            if signature not in signatures:
                signatures.add(signature)
                survivors.append(task)
                continue
            self.dialogue_state.transition_task(
                task.task_id, "cancelled", result="중복 승인 대기 작업 정리됨",
                pending_question="",
            )
            self.dialogue_state.delete(session_id, task.task_id)
            self.dialogue_state.delete_intent_state(task.task_id)
        return survivors

    def _canonical_external_send_approval_plan(
        self, task,
    ) -> tuple[Optional[PlanDAG], Optional[IntentResolution]]:
        """Rebuild a stale approval task from its Registry contract when possible."""
        resolution = self._approval_task_resolution(task)
        if not resolution.ready:
            return None, None
        capability = self.intent_router.registry.get_capability(resolution.tool_name)
        if not capability or capability.side_effect != "external_send":
            return None, None
        errors = self.intent_router.registry.validate_tool_call(
            resolution.tool_name, resolution.slots, resolution.request_type,
        )
        if errors:
            raise ValueError("승인 작업의 도구 계약 오류: " + "; ".join(errors))
        plan = PlanDAG(goal=task.goal, steps=[PlanStep(
            id=f"intent-{task.task_id}",
            description=resolution.intent_name,
            tool_name=resolution.tool_name,
            tool_input=dict(resolution.slots),
            requires_approval=True,
            approval_reason="외부 대상에게 데이터를 전송하는 작업입니다.",
            retry_budget=int(getattr(capability, "max_retries", 0) or 0),
            verification={
                "required": bool(getattr(capability, "verification_required", True)),
            },
            status="awaiting_approval",
        )])
        self.dialogue_state.update_task(
            task.task_id,
            intent_name=resolution.intent_name,
            slots=resolution.slots,
            context_confidence=resolution.confidence,
            plan=plan.to_dict()["steps"],
            plan_id=plan.plan_id,
        )
        return plan, resolution

    def _cancel_scoped_tasks(self, session_key: str, scope: str, goal: str) -> ExecutionOutcome:
        tasks = self.dialogue_state.list_tasks(
            session_key, include_finished=False, workspace_path=self._workspace_scope()
        )
        if scope == "pending":
            tasks = [t for t in tasks if t.status in {"queued", "awaiting_user", "awaiting_approval"}]
        elif scope == "current":
            current = getattr(self, "current_agent_task_id", "")
            active = [t for t in tasks if t.task_id == current]
            if active:
                tasks = active
            elif len(tasks) > 1:
                return ExecutionOutcome(
                    "중단할 작업이 여러 개예요. 작업 번호 또는 ‘모든 작업 취소’를 지정해 주세요.",
                    "awaiting_user", goal,
                )
        results = [self.handle_control_command(f"{t.task_id} 취소", session_key) for t in tasks]
        cancelled = [r.task_id for r in results if r.status == "cancelled"]
        failed = [r.task_id for r in results if r.status != "cancelled"]
        response = (f"작업 {len(cancelled)}개를 취소했습니다." if cancelled
                    else "현재 취소할 작업이 없습니다.")
        if failed:
            response += " 취소하지 못한 작업: " + ", ".join(failed)
        return ExecutionOutcome(response, "partial" if failed else "cancelled", goal)

    def handle_control_command(self, text: str, session_id: Optional[str] = None) -> ExecutionOutcome:
        session_key = session_id or "default"
        normalized = text.strip().lower()
        from core.semantic_request import explicit_control
        control = explicit_control(text)
        if control and control[0] == "cancel":
            return self._cancel_scoped_tasks(session_key, control[1], text)
        if normalized in {"대기 작업", "대기 작업 목록"}:
            items = self.dialogue_state.list(session_key, self._workspace_scope())
            response = ("답변을 기다리는 작업입니다.\n" + "\n".join(
                f"- {item.task_id}: {item.original_goal} (질문: {item.question})" for item in items
            )) if items else "현재 답변을 기다리는 작업이 없습니다."
            return ExecutionOutcome(response, "completed")
        if normalized in {"작업 목록", "전체 작업 목록"}:
            tasks = self.dialogue_state.list_tasks(
                session_key, workspace_path=self._workspace_scope()
            )
            if not tasks:
                return ExecutionOutcome("등록된 작업이 없습니다, 보스.")
            lines = [f"- {t.task_id} [{t.status}] 우선순위 {t.priority}: {t.goal[:60]}" for t in tasks[:20]]
            return ExecutionOutcome("작업 목록입니다, 보스.\n" + "\n".join(lines))

        if (control and control[0] == "approve") or self._is_contextual_approval_command(normalized):
            pending = [
                item for item in self.dialogue_state.list_tasks(
                    session_key, include_finished=False,
                    workspace_path=self._workspace_scope(),
                )
                if item.status == "awaiting_approval"
            ]
            pending = self._coalesce_equivalent_approval_tasks(session_key, pending)
            if not pending:
                return ExecutionOutcome(
                    "현재 승인 대기 중인 작업이 없습니다, 보스.", "completed"
                )
            if len(pending) > 1:
                choices = "\n".join(
                    f"- 작업 {item.task_id}: {item.goal[:80]}" for item in pending[:10]
                )
                return ExecutionOutcome(
                    "승인 대기 중인 작업이 여러 개라 임의로 실행하지 않았습니다. "
                    "승인할 작업 번호를 지정해 주세요.\n" + choices,
                    "awaiting_approval",
                    question="작업 ID만 입력하거나 ‘작업 ID 승인’ 형식으로 지정해 주세요.",
                )
            task = pending[0]
            task_id, command = task.task_id, "승인"
        else:
            match = re.fullmatch(
                r"(?:작업\s*)?([0-9a-f]{8})(?:\s*(.*))?", normalized, re.S
            )
            if not match:
                return ExecutionOutcome("작업 제어 명령을 이해하지 못했습니다, 보스.", "failed")
            task_id = match.group(1)
            command = (match.group(2) or "").strip()
            task = self.dialogue_state.get_task(
                session_key, task_id, self._workspace_scope()
            )
        if not task:
            return ExecutionOutcome(f"작업 {task_id}을 찾지 못했습니다, 보스.", "failed", task_id=task_id)
        if not command:
            if task.status == "awaiting_approval":
                # The UI asks the user to choose one of several approval tasks.  In
                # that context the exact ID itself is an unambiguous selection and
                # must never be sent to the conversation LLM.
                command = "승인"
            else:
                return ExecutionOutcome(
                    f"작업 {task_id}은 현재 {task.status} 상태입니다. "
                    "승인 대기 작업이면 ‘작업 ID 승인’이라고 입력해 주세요, 보스.",
                    task.status, task.goal, task_id=task_id,
                )
        if command == "상태":
            return ExecutionOutcome(
                f"작업 {task_id}은 현재 {task.status} 상태이고 우선순위는 {task.priority}입니다, 보스.",
                task.status, task.goal, task_id=task_id,
            )
        if command == "승인":
            with self._APPROVAL_CLAIM_LOCK:
                task = self.dialogue_state.get_task(
                    session_key, task_id, self._workspace_scope()
                )
                if not task or task.status != "awaiting_approval":
                    return ExecutionOutcome(
                        f"작업 {task_id}은 현재 승인 대기 상태가 아닙니다, 보스.",
                        "failed", getattr(task, "goal", ""), task_id=task_id,
                    )
                if not self.dialogue_state.transition_task(task_id, "running"):
                    return ExecutionOutcome(
                        f"작업 {task_id}의 승인을 선점하지 못해 실행하지 않았습니다, 보스.",
                        "failed", task.goal, task_id=task_id,
                    )
                approval_inflight = getattr(self, "_approval_inflight", None)
                if approval_inflight is None:
                    approval_inflight = set()
                    self._approval_inflight = approval_inflight
                approval_inflight.add(task_id)
            outcome = None
            try:
                plan, approved_resolution = self._canonical_external_send_approval_plan(task)
                if plan is None:
                    if not task.plan_id or not task.plan:
                        raise ValueError("저장된 실행 계획이 없습니다.")
                    plan = PlanDAG(
                        goal=task.goal, steps=[PlanStep(**item) for item in task.plan],
                        plan_id=task.plan_id,
                    )
                approved = [step.id for step in plan.steps
                            if step.status.value == "awaiting_approval"]
                if not approved:
                    raise ValueError("승인 대기 단계가 없습니다.")
                for step in plan.steps:
                    if step.id not in approved:
                        continue
                    if not step.tool_name:
                        raise ValueError(
                            f"승인 단계 {step.id}에 실행 도구가 없어 안전하게 실행할 수 없습니다."
                        )
                    errors = self.intent_router.registry.validate_tool_call(
                        step.tool_name, step.tool_input,
                        approved_resolution.request_type if approved_resolution else "external_send",
                    )
                    if errors:
                        raise ValueError(
                            f"승인 단계 {step.id}의 도구 계약 오류: " + "; ".join(errors)
                        )
                self.current_agent_task_id = task_id
                run = self.execute_plan_dag(plan, approved_step_ids=approved)
                outcome = self._finish_plan_run(run, goal=task.goal, task_id=task_id)
                tool_run = next(iter(run.results.values()), None)
                if outcome.status == "completed" and tool_run is not None:
                    response = self.intent_router.registry.present_result(
                        tool_run.tool_name, tool_run.raw_output
                    )
                    outcome.response = response
                    self.dialogue_state.update_task(task_id, result=response)
                    resolved_intent_name = (
                        approved_resolution.intent_name
                        if approved_resolution else task.intent_name
                    )
                    resolved_slots = (
                        approved_resolution.slots if approved_resolution else task.slots
                    )
                    if resolved_intent_name:
                        self.dialogue_state.save_recent_intent(
                            session_key, resolved_intent_name, resolved_slots, task.goal,
                            task_id=task_id, workspace_path=self._workspace_scope(),
                        )
                if outcome.status != "awaiting_approval":
                    self.dialogue_state.delete_intent_state(task_id)
                return outcome
            except ToolCancelledError as exc:
                if outcome is not None:
                    return outcome
                self.dialogue_state.transition_task(task_id, "cancelled", result=str(exc))
                return ExecutionOutcome(str(exc), "cancelled", task.goal, task_id=task_id)
            except Exception as exc:
                if outcome is not None:
                    # Presentation failures cannot undo an observed external
                    # effect or authorize the user to retry it blindly.
                    return outcome
                self.dialogue_state.update_task(
                    task_id, status="failed", result=f"승인 후 재개 실패: {exc}",
                    verification_status="failed",
                )
                return ExecutionOutcome(
                    f"작업 {task_id} 승인 후 재개에 실패했습니다: {exc}",
                    "failed", task.goal, task_id=task_id,
                )
            finally:
                self.current_agent_task_id = ""
                with self._APPROVAL_CLAIM_LOCK:
                    getattr(self, "_approval_inflight", set()).discard(task_id)
        priority_match = re.fullmatch(r"우선순위\s*(-?\d+)", command)
        if priority_match:
            priority = int(priority_match.group(1))
            self.dialogue_state.update_task(task_id, priority=priority)
            return ExecutionOutcome(f"작업 {task_id}의 우선순위를 {priority}로 변경했습니다, 보스.", task.status, task_id=task_id)

        if command in {"취소", "중단"}:
            with self._APPROVAL_CLAIM_LOCK:
                if task_id in getattr(self, "_approval_inflight", set()):
                    return ExecutionOutcome(
                        f"작업 {task_id}은 승인을 선점해 외부 실행을 시작한 상태라 "
                        "취소 완료로 표시할 수 없습니다, 보스.",
                        "running", task.goal, task_id=task_id,
                    )
                if not self.dialogue_state.transition_task(
                    task_id, "cancelled", result="사용자 취소"
                ):
                    return ExecutionOutcome(
                        f"작업 {task_id}은 현재 {task.status} 상태라 취소할 수 없습니다, 보스.",
                        "failed", task_id=task_id,
                    )
            self.dialogue_state.delete(session_key, task_id)
            with self._control_condition:
                control = self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})
                control["cancel"] = True
                control["pause"] = False
                self._control_condition.notify_all()
            return ExecutionOutcome(f"작업 {task_id} 취소를 요청했습니다, 보스.", "cancelled", task_id=task_id)
        revision_match = re.fullmatch(r"수정\s*[:：]\s*(.+)", command, re.S)
        if revision_match:
            revision = revision_match.group(1).strip()
            with self._APPROVAL_CLAIM_LOCK:
                if task_id in getattr(self, "_approval_inflight", set()):
                    return ExecutionOutcome(
                        f"작업 {task_id}은 승인을 선점해 외부 실행을 시작한 상태라 "
                        "지금 수정할 수 없습니다, 보스.",
                        "running", task.goal, task_id=task_id,
                    )
                if not self.dialogue_state.transition_task(
                    task_id, "cancelled", result=f"수정 지시로 대체: {revision}"
                ):
                    return ExecutionOutcome(
                        f"작업 {task_id}은 현재 {task.status} 상태라 수정할 수 없습니다, 보스.",
                        "failed", task_id=task_id,
                    )
            with self._control_condition:
                control = self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})
                control["cancel"] = True
                control["pause"] = False
                self._control_condition.notify_all()
            next_goal = f"원래 요청: {task.goal}\n사용자 수정 지시: {revision}\n수정 지시를 반영해 작업을 다시 수행하세요."
            return ExecutionOutcome(
                f"작업 {task_id}을 중단하고 수정 지시를 반영해 다시 시작하겠습니다, 보스.",
                "cancelled", task.goal, task_id=task_id, next_goal=next_goal,
            )
        if command == "일시정지":
            if not self.dialogue_state.transition_task(task_id, "paused"):
                return ExecutionOutcome(
                    f"작업 {task_id}은 현재 {task.status} 상태라 일시정지할 수 없습니다, 보스.",
                    "failed", task_id=task_id,
                )
            with self._control_condition:
                self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})["pause"] = True
            return ExecutionOutcome(f"작업 {task_id}을 일시정지했습니다, 보스.", "paused", task_id=task_id)
        if command == "재개":
            with self._control_condition:
                control = self._task_controls.setdefault(task_id, {"cancel": False, "pause": False})
                control["pause"] = False
                self._control_condition.notify_all()
            if task.status in {"interrupted", "paused"} and task_id != self.current_agent_task_id:
                if not self.dialogue_state.transition_task(
                    task_id, "queued", result=""
                ):
                    return ExecutionOutcome(
                        f"작업 {task_id}을 재개할 수 없습니다, 보스.", "failed", task_id=task_id
                    )
                return ExecutionOutcome(
                    f"작업 {task_id}의 저장된 목표를 실행 대기열에 복구했습니다, 보스.",
                    "queued", task.goal, task_id=task_id,
                )
            if not self.dialogue_state.transition_task(task_id, "running"):
                return ExecutionOutcome(
                    f"작업 {task_id}은 현재 {task.status} 상태라 재개할 수 없습니다, 보스.",
                    "failed", task_id=task_id,
                )
            return ExecutionOutcome(f"작업 {task_id}을 재개했습니다, 보스.", "running", task_id=task_id)
        return ExecutionOutcome("지원하지 않는 작업 제어 명령입니다, 보스.", "failed", task_id=task_id)

    def _wait_for_task_control(self) -> bool:
        check_turn_cancelled()
        task_id = self.current_agent_task_id
        if not task_id:
            return True
        with self._control_condition:
            control = self._task_controls.get(task_id, {})
            while control.get("pause") and not control.get("cancel"):
                self._control_condition.wait(timeout=0.5)
                check_turn_cancelled()
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
                started_at = time.perf_counter()
                result = self.execute_tool(tool_name, tool_input)
                print(f"[Executor] Tool 실행 결과: {result}")

                # 7. 실행 결과 검증
                tool_run = self.build_tool_run_result(
                    task,
                    tool_name,
                    tool_input,
                    result,
                    (time.perf_counter() - started_at) * 1000,
                )
                if self.current_agent_task_id:
                    self.dialogue_state.update_task(
                        self.current_agent_task_id,
                        artifacts=[asdict(item) for item in tool_run.artifacts],
                        evidence=[asdict(item) for item in tool_run.evidence],
                        last_tool=tool_name,
                        retry_count=self._retry_count,
                    )
                result = tool_run.raw_output
                verified = tool_run.succeeded
                if tool_run.status == ToolRunStatus.UNVERIFIED:
                    self.terminal_error = (
                        f"{tool_name} 도구는 실행됐지만 결과를 검증할 방법이 없어 "
                        "완료로 확정하지 않았습니다."
                    )
                    self.scratchpad.add_observation(
                        tool_name, tool_input, result, False
                    )
                    self.scratchpad.fail_task(task.id, self.terminal_error)
                    return False
                if not verified:
                    self._consecutive_failures += 1
                    self._total_failures += 1
                    print(f"[Executor] 실행 결과 검증 실패! 복구 시도... (연속 실패: {self._consecutive_failures})")
                    self._emit_progress(f"{tool_name} 실행 결과를 확인하지 못해 복구를 시도하고 있습니다.")
                    recovered_tool_run = self.recover(task, tool_name, tool_input, result)
                    if recovered_tool_run is not None:
                        tool_run = recovered_tool_run
                        result = recovered_tool_run.raw_output
                        verified = recovered_tool_run.succeeded
                        if self.current_agent_task_id:
                            self.dialogue_state.update_task(
                                self.current_agent_task_id,
                                artifacts=[asdict(item) for item in recovered_tool_run.artifacts],
                                evidence=[asdict(item) for item in recovered_tool_run.evidence],
                                last_tool=recovered_tool_run.tool_name,
                                retry_count=self._retry_count,
                            )
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
            memory_context = build_memory_context(self.session_id, 15, True, self.goal)
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
        # 대화 기억과 scratchpad는 위에서 이미 현재 turn 기준으로 조립했으므로
        # ContextManager가 같은 내용을 다시 넣지 않도록 명시적으로 제외한다.
        try:
            system_context = self.context_manager.get_full_context(
                user_query=self.goal,
                session_id=self.session_id,
                include_conversation=False,
                include_scratchpad=False,
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
        # 역할별 LLM 클라이언트는 여러 GUI 작업과 전문가가 공유할 수 있다. 전역
        # system_prompt를 잠깐 바꾸는 방식은 동시 호출에서 서로의 역할 프롬프트를
        # 덮어쓰므로, 이 호출에만 적용되는 system 메시지로 전달한다.
        from core.assistant_settings import get_assistant_settings
        action_system_prompt = (
            f"당신은 {get_assistant_settings().assistant_name}의 Action Reasoner 겸 Tool Selector입니다.\n"
            "주어진 Task를 수행하기 위해 도구가 필요하면 반드시 제공된 도구 중 하나를 호출하세요.\n"
            "도구 없이 바로 답할 수 있는 간단한 작업이나 이미 끝난 작업이면, 도구를 호출하지 말고 "
            "결과나 답변을 자연스러운 한국어 텍스트로 바로 답하세요.\n"
            "제공된 도구 목록에 없는 도구는 절대 지어내지 마세요.\n"
            "최종 Goal과 현재 Task를 가장 높은 우선순위로 따르고, 과거 Memory의 다른 주제는 무시하세요.\n"
            "Plugin Registry가 현재 요청에 허용한 도구만 사용하고 다른 도메인의 도구는 호출하지 마세요."
        )
        try:
            messages = [
                {"role": "system", "content": action_system_prompt},
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
            return {
                "action_type": "error",
                "simple_result": f"작업 방법을 결정하지 못했습니다: {e}",
            }

    @staticmethod
    def _read_tool_use_block(block) -> tuple[Optional[str], Dict[str, Any]]:
        """
        Anthropic SDK는 tool_use 블록을 속성 접근 객체(block.name, block.input)로,
        Ollama 쪽 구현은 dict(block["name"], block["input"])로 반환하므로 둘 다 처리합니다.
        """
        if isinstance(block, dict):
            return block.get("name"), block.get("input") or {}
        return getattr(block, "name", None), getattr(block, "input", None) or {}

    def _validated_tool_scope(
        self, allowed_tool_names: Optional[List[str]],
    ) -> Optional[List[str]]:
        if allowed_tool_names is None:
            return None
        registry = getattr(getattr(self, "intent_router", None), "registry", None)
        if registry is None:
            return []
        return list(dict.fromkeys(
            str(name) for name in allowed_tool_names
            if registry.get_capability(str(name)) is not None
        ))

    def _allowed_tools_for_goal(
        self, goal: str, allowed_tool_names: Optional[List[str]] = None,
    ) -> Optional[List[str]]:
        if not hasattr(self, "intent_router"):
            return None
        if not hasattr(self, "tool_loadout"):
            self.tool_loadout = ToolLoadoutSelector(self.intent_router.registry)
        resolution = self.intent_router.resolve(goal)
        tool_scope = self._validated_tool_scope(allowed_tool_names)
        if (tool_scope is not None and resolution.matched
                and resolution.tool_name not in tool_scope):
            resolution = IntentResolution()
        loadout = self.tool_loadout.select(
            goal, resolution, allowed_tools=tool_scope,
        )
        return list(loadout.tool_names)

    def _should_attempt_registry_execution(
        self, goal: str, allowed_tool_names: Optional[List[str]] = None,
    ) -> bool:
        """Route broad action requests without forcing ordinary chat into tools."""
        normalized = " ".join(str(goal or "").split())
        scope = analyze_utterance_scope(normalized)
        if scope.discussion or scope.negated or scope.conditional:
            return False
        normalized = scope.routing_text
        if not normalized or self._SOCIAL_ONLY_PATTERN.fullmatch(normalized):
            return False
        if not (
            self._EXECUTION_REQUEST_PATTERN.search(normalized)
            or self._GENERIC_ACTION_REQUEST_PATTERN.search(normalized)
        ):
            return False
        if not hasattr(self, "intent_router"):
            return False
        if not hasattr(self, "tool_loadout"):
            self.tool_loadout = ToolLoadoutSelector(self.intent_router.registry)
        loadout = self.tool_loadout.select(
            normalized, IntentResolution(),
            allowed_tools=self._validated_tool_scope(allowed_tool_names),
        )
        # A non-empty, descriptor-grounded loadout is required. This prevents a
        # generic verb such as "해줘" from making the local model invent a tool.
        return bool(
            loadout.tool_names
            and loadout.reason == "registry_descriptor_match"
            and loadout.confidence >= 0.08
        )

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
        workspace_scope = self._workspace_scope()
        contract_errors = self.intent_router.registry.validate_tool_call(
            resolution.tool_name, resolution.slots, resolution.request_type
        )
        if contract_errors:
            return ExecutionOutcome(
                "도구 계약 검사를 통과하지 못했습니다: " + "; ".join(contract_errors),
                "failed", goal, task_id=task_id,
            )
        capability = self.intent_router.registry.get_capability(resolution.tool_name)
        requires_approval = bool(
            capability and capability.side_effect == "external_send"
        )
        if requires_approval:
            self._cancel_equivalent_approval_tasks(
                session_id, workspace_scope, resolution,
                exclude_task_id=task_id,
            )
        task_id = task_id or self.dialogue_state.create_task(
            session_id, goal, workspace_path=workspace_scope
        ).task_id
        if not self.dialogue_state.transition_task(
            task_id, "running", workspace_path=workspace_scope,
            intent_name=resolution.intent_name, slots=resolution.slots,
            context_confidence=resolution.confidence,
            plan=[{
                "id": task_id,
                "description": resolution.intent_name,
                "status": "running",
                "required_tools": [resolution.tool_name],
            }],
        ):
            return ExecutionOutcome(
                "현재 작업 상태에서는 도구 실행을 시작할 수 없습니다, 보스.",
                "failed", goal, task_id=task_id,
            )
        if progress_callback:
            progress_callback(f"작업 {task_id}: {resolution.tool_name} 실행을 시작합니다.")
        plan = PlanDAG(goal=goal, steps=[PlanStep(
            id=f"intent-{task_id}", description=resolution.intent_name,
            tool_name=resolution.tool_name, tool_input=dict(resolution.slots),
            requires_approval=requires_approval,
            approval_reason="외부 대상에게 데이터를 전송하는 작업입니다."
            if requires_approval else "",
            retry_budget=int(getattr(capability, "max_retries", 0) or 0),
            verification={"required": bool(getattr(capability, "verification_required", True))},
        )])
        self.current_agent_task_id = task_id
        self.dialogue_state.update_task(
            task_id, plan=plan.to_dict()["steps"], plan_id=plan.plan_id,
        )
        outcome = None
        try:
            run = self.execute_plan_dag(plan)
            outcome = self._finish_plan_run(run, goal=goal, task_id=task_id)
            tool_run = next(iter(run.results.values()), None)
            if outcome.status == "completed" and tool_run is not None:
                response = self.intent_router.registry.present_result(
                    resolution.tool_name, tool_run.raw_output
                )
                realizer = getattr(self, "response_realizer", None)
                if realizer is not None:
                    settings = get_assistant_settings()
                    response = realizer.realize(
                        response, tool_name=resolution.tool_name,
                        user_request=goal, assistant_name=settings.assistant_name,
                        address=settings.get("user_address"),
                        style=settings.get("response_style"),
                    )
                outcome.response = response
                self.dialogue_state.update_task(task_id, result=response)
                self.dialogue_state.save_recent_intent(
                    session_id, resolution.intent_name, resolution.slots, goal,
                    task_id=task_id, workspace_path=workspace_scope,
                )
            if outcome.status != "awaiting_approval":
                self.dialogue_state.delete_intent_state(task_id)
            return outcome
        except ToolCancelledError as exc:
            if outcome is not None:
                return outcome
            self.dialogue_state.transition_task(task_id, "cancelled", result=str(exc))
            self.dialogue_state.delete(session_id, task_id)
            return ExecutionOutcome(str(exc), "cancelled", goal, task_id=task_id)
        finally:
            self.current_agent_task_id = ""

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

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        """Tool 실행"""
        record_runtime_event("tool_requested", tool_name=tool_name, tool_input=tool_input)
        result = self.tool_executor.execute_tool(tool_name, tool_input)
        record_runtime_event(
            "tool_completed", tool_name=tool_name,
            status=(result.status.value if isinstance(result, ToolRunResult) else "legacy"),
            result=(result.to_dict() if isinstance(result, ToolRunResult) else result),
        )
        return result

    def verify_execution(self, task: Task, tool_name: str, tool_input: Dict[str, Any], result: str) -> bool:
        """실행 결과 검증: ToolVerifier를 사용해 정교하게 확인"""
        return self.build_tool_run_result(task, tool_name, tool_input, result).succeeded

    def build_tool_run_result(
        self,
        task: Task,
        tool_name: str,
        tool_input: Dict[str, Any],
        result,
        duration_ms: float = 0.0,
    ) -> ToolRunResult:
        """직접 타입 결과는 보존하고 Legacy 문자열만 검증기로 변환한다."""
        if isinstance(result, ToolRunResult):
            if result.tool_name != tool_name:
                return ToolRunResult.failed(
                    tool_name=tool_name,
                    error=(
                        f"Tool 결과 이름이 요청과 일치하지 않습니다: "
                        f"{result.tool_name}"
                    ),
                    raw_output=result.raw_output,
                    duration_ms=duration_ms,
                )
            if result.succeeded and not result.evidence:
                result.status = ToolRunStatus.UNVERIFIED
                result.error = None
            if result.duration_ms <= 0:
                result.duration_ms = max(0.0, float(duration_ms))
            print(
                f"[Executor] 구조화 결과: {result.status.value} "
                f"(evidence={len(result.evidence)}, artifacts={len(result.artifacts)}, "
                f"duration_ms={result.duration_ms:.1f})"
            )
            return result
        verification = self.verifier.verify(tool_name, tool_input, result)
        tool_run = ToolRunResult.from_verification(
            tool_name=tool_name,
            raw_output=result,
            verification=verification,
            duration_ms=duration_ms,
        )
        print(
            f"[Executor] 검증 결과: "
            f"{tool_run.status.value} - {verification.message} "
            f"(evidence={len(tool_run.evidence)}, artifacts={len(tool_run.artifacts)}, "
            f"duration_ms={tool_run.duration_ms:.1f})"
        )
        return tool_run

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

    def recover(self, task: Task, tool_name: str, tool_input: Dict[str, Any],
                last_result: str) -> Optional[ToolRunResult]:
        """실패 복구: RecoveryManager를 사용해 단계별로 시도"""
        print(f"[Executor] 복구 시도... (재시도 횟수: {self._retry_count})")
        
        recovery_result = self.recovery_manager.recover(
            task=task,
            tool_name=tool_name,
            tool_input=tool_input,
            last_result=last_result,
            retry_count=self._retry_count,
            verify_callback=lambda candidate: self.build_tool_run_result(
                task, tool_name, tool_input, candidate
            ),
        )

        self._retry_count = recovery_result.retry_count
        
        if recovery_result.success:
            print(f"[Executor] 복구 성공! {recovery_result.message}")
            return recovery_result.result
        print(f"[Executor] 복구 실패... {recovery_result.message}")
        return None

    @staticmethod
    def _unsupported_capability_message(goal: str) -> Optional[str]:
        """Return only product-level blocks that Registry cannot represent.

        Capability availability belongs to PluginRegistry and runtime
        diagnostics. Feature-specific denylists here become stale and can make
        implemented tools unreachable. Missing credentials or runtimes are
        therefore reported by the selected tool with evidence.
        """
        return None

    def finalize(self) -> str:
        """최종 종료 처리: 최종 답변 생성"""
        if self.terminal_error:
            return self.terminal_error
        return self.generate_response()

    def _respond_conversationally(
        self, message: str, history: List[Dict[str, str]], *, semantic_decision=None,
    ) -> str:
        """Answer ordinary conversation without exposing or invoking tools."""
        scope = analyze_utterance_scope(message, history)
        # Preserve the validated semantic decision across the presentation
        # boundary. Korean imperatives also request conversation ("인사해줘");
        # lexical fallback must not override a grounded no-tool classification.
        grounded_conversation = bool(
            semantic_decision is not None
            and semantic_decision.raw_text == message
            and semantic_decision.is_grounded_conversation
        )
        execution_requested = not grounded_conversation and not scope.discussion and bool(
            self._EXECUTION_REQUEST_PATTERN.search(scope.routing_text)
            or self._GENERIC_ACTION_REQUEST_PATTERN.search(scope.routing_text)
        )
        if execution_requested:
            # Reaching this method means no Registry execution path accepted the
            # request.  The structural absence of ToolRunResult evidence is
            # decisive, so never ask a conversational model to narrate success.
            return (
                "실제 작업을 실행하지 않았습니다. 도구 실행 증거가 없습니다. "
                "따라서 완료로 보고하지 않겠습니다. 완료했다고 안내하지 않겠습니다. "
                "지원되는 실행 경로와 필요한 연결 상태 확인이 필요합니다."
            )
        custom_voice, address, conversation_style = self._selected_voice_preferences()
        selected_profile = next(
            (
                item for item in load_custom_voice_profiles()
                if str(item.get("id")) == custom_voice
            ),
            {},
        )
        from core.assistant_settings import get_assistant_settings
        runtime_settings = get_assistant_settings()
        assistant_name = runtime_settings.assistant_name or str(selected_profile.get("assistant_name", "")).strip()
        conversation_service = getattr(self, "conversation_service", None)
        if conversation_service is None:
            conversation_service = ConversationService(self.llm)
            self.conversation_service = conversation_service
        memory_context = build_relevant_knowledge_context(
            message, self._workspace_scope(), limit=6
        )
        rag_context = ""
        context_manager = getattr(self, "context_manager", None)
        if context_manager is not None:
            rag_context = context_manager.get_rag_context(message)
        grounded_context = "\n".join(
            part for part in (memory_context, rag_context) if part
        )
        response = conversation_service.respond(
            message, history, assistant_name=assistant_name, voice_name=custom_voice,
            address=address, style=conversation_style, memory_context=grounded_context,
        )
        # The conversation classification selects a channel; it is not proof
        # that an external action happened. Also guard compatible/custom
        # services here, before recording the reply as memory usage.
        response = guard_conversation_response(response, message)
        if context_manager is not None:
            context_manager.mark_response_usage(response)
        return response

    def _selected_voice_preferences(self) -> tuple[str, str, str]:
        settings = getattr(getattr(self, "tool_executor", None), "tts_settings", None)
        voice_id = getattr(settings, "selected_custom_voice", "")
        address = getattr(settings, "selected_address", "보스")
        profile = next(
            (
                item for item in load_custom_voice_profiles()
                if str(item.get("id")) == voice_id
            ),
            None,
        )
        style = str((profile or {}).get("conversation_style", "")).strip()
        from core.assistant_settings import get_assistant_settings
        configured_style = get_assistant_settings().get("response_style")
        if configured_style:
            style = " ".join(filter(None, [style, configured_style]))
        response_language = get_assistant_settings().get("response_language")
        if response_language:
            style = " ".join(filter(None, [style, f"응답 언어는 {response_language}"]))
        return voice_id, address, style

    def generate_response(self) -> str:
        """최종 답변 생성"""
        voice_id, address, conversation_style = self._selected_voice_preferences()
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
                    f"체감온도는 {weather.get('apparent_temperature_c')}°C입니다, {address}. "
                    f"오늘 최저 {weather.get('today_min_c')}°C, 최고 {weather.get('today_max_c')}°C이며, "
                    f"습도는 {weather.get('humidity_percent')}%입니다.{precision}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        style_instruction = (
            f"\n현재 선택된 음성은 '{voice_id}'입니다. 다음 음성별 대화 스타일을 "
            f"상황에 맞게 적용하세요: {conversation_style}"
            if conversation_style else ""
        )
        from core.assistant_settings import get_assistant_settings
        assistant_name = get_assistant_settings().assistant_name
        system_prompt = f"""당신은 {assistant_name}입니다.
전체 Context를 보고, 최종 답변을 한국어로 작성하세요!
사용자 호칭은 반드시 '{address}'로 사용하고 다른 호칭으로 바꾸지 마세요.
외부의 현재 사실(날씨, 일정, 메일, 웹 정보 등)은 성공한 Tool Observation에 있는 값만 사용하세요.
Tool이 실패했거나 관측값이 없으면 절대 수치를 추측하지 말고 확인하지 못했다고 답하세요.
RAG 문서에서 가져온 주장은 Context의 [근거 ID]를 문장 뒤에 표시하고, 만료되어 재검증이 필요한 웹 정보는 사용하지 마세요.
location_precision이 city이면 동 단위 관측이 아니라 도시 기준 근사값임을 명시하세요.
최종 Goal에 적힌 '후속 질문의 핵심 요구'에 먼저 직접 답하고, Tool 수치와 이름을 바꾸거나 생략하지 마세요.
{style_instruction}"""
        verified_results = json.dumps(successful_observations, ensure_ascii=False, indent=2)
        user_prompt = (
            f"Context:\n{context}\n\n"
            f"검증된 Tool 결과 원문:\n{verified_results}\n\n"
            f"최종 Goal:\n{self.goal}\n\n최종 답변은?"
        )

        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])
            context_manager = getattr(self, "context_manager", None)
            if context_manager is not None:
                context_manager.mark_response_usage(response)
            return response
        except Exception as e:
            return f"죄송해요, {address}! 최종 답변 생성 중 오류가 발생했어요: {e}"

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
