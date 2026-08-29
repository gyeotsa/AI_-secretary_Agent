"""Focused, side-effect-free regression tests for Executor approval safety."""

from types import SimpleNamespace
import threading

import pytest

from core.dialogue_state import DialogueStateStore
from core.executor import Executor
from core.intent_router import IntentResolution
from core.plan_runtime import PlanDAG, PlanRunResult, PlanStep, StepStatus
from core.tool_result import Evidence, ToolRunResult


SESSION = "approval-safety"
INTENT_NAME = "messaging.send"
TOOL_NAME = "fake_external_send"


class _FakeRegistry:
    def __init__(self):
        self.schema = SimpleNamespace(
            name=INTENT_NAME,
            tool_name=TOOL_NAME,
            slots=[
                SimpleNamespace(name="provider", required=True),
                SimpleNamespace(name="recipient", required=True),
                SimpleNamespace(name="message", required=True),
            ],
            request_type="external_send",
            domain="messaging",
            action="send",
            freshness="static",
            requires_sources=False,
        )
        self.capability = SimpleNamespace(
            side_effect="external_send",
            max_retries=0,
            verification_required=True,
        )

    def get_all_intents(self):
        return [(None, self.schema)]

    def get_capability(self, tool_name):
        return self.capability if tool_name == TOOL_NAME else None

    def validate_tool_call(self, tool_name, tool_input, request_type=""):
        if tool_name != TOOL_NAME:
            return [f"unknown tool: {tool_name}"]
        if request_type and request_type != "external_send":
            return [f"wrong request type: {request_type}"]
        return [
            f"missing: {name}"
            for name in ("provider", "recipient", "message")
            if not str(tool_input.get(name, "")).strip()
        ]

    @staticmethod
    def present_result(tool_name, result):
        assert tool_name == TOOL_NAME
        return f"검증된 전송 결과: {result}"


class _FakeRouter:
    def __init__(self):
        self.registry = _FakeRegistry()

    @staticmethod
    def resolve(_goal, *_args, **_kwargs):
        # Stored Registry intent/slots, not reparsing an old incomplete goal, are
        # intentionally required by these tests.
        return IntentResolution()


def _executor(tmp_path):
    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue-state.db"))
    executor.intent_router = _FakeRouter()
    executor.workspace_manager = None
    executor.current_agent_task_id = ""
    executor.response_realizer = None
    executor.context_manager = None
    executor._control_condition = threading.Condition()
    executor._task_controls = {}
    executor._approval_inflight = set()
    return executor


def _resolution(recipient="형택", message="테스트 메시지"):
    return IntentResolution(
        matched=True,
        intent_name=INTENT_NAME,
        tool_name=TOOL_NAME,
        slots={
            "provider": "kakaotalk",
            "recipient": recipient,
            "message": message,
        },
        explicit=True,
        execution_requested=True,
        confidence=0.99,
        domain="messaging",
        action="send",
        request_type="external_send",
    )


def _create_approval(executor, recipient="형택", message="테스트 메시지", *, goal=None):
    resolution = _resolution(recipient, message)
    task = executor.dialogue_state.create_task(
        SESSION,
        goal or f"{recipient}에게 {message} 보내줘",
        workspace_path="",
    )
    plan = PlanDAG(goal=task.goal, steps=[PlanStep(
        id=f"intent-{task.task_id}",
        description=INTENT_NAME,
        tool_name=TOOL_NAME,
        tool_input=dict(resolution.slots),
        requires_approval=True,
        approval_reason="외부 전송",
        retry_budget=0,
        status=StepStatus.AWAITING_APPROVAL,
    )])
    assert executor.dialogue_state.transition_task(
        task.task_id,
        "running",
        intent_name=INTENT_NAME,
        slots=resolution.slots,
        context_confidence=resolution.confidence,
        plan=plan.to_dict()["steps"],
        plan_id=plan.plan_id,
    )
    assert executor.dialogue_state.transition_task(task.task_id, "awaiting_approval")
    return executor.dialogue_state.get_task(SESSION, task.task_id, "")


def _install_successful_plan_runner(executor):
    calls = []

    def execute_plan_dag(plan, approved_step_ids=None):
        step = plan.steps[0]
        if approved_step_ids is not None:
            assert approved_step_ids == [step.id]
        calls.append(dict(step.tool_input))
        step.status = StepStatus.COMPLETED
        result = ToolRunResult.successful(
            tool_name=TOOL_NAME,
            raw_output="sent-once",
            evidence=[Evidence("test", "verified external send")],
        )
        return PlanRunResult(plan, "completed", {step.id: result}, [])

    executor.execute_plan_dag = execute_plan_dag
    return calls


def test_conversation_action_fallback_never_calls_llm_or_claims_completion(tmp_path):
    executor = _executor(tmp_path)

    class _ForbiddenConversation:
        @staticmethod
        def respond(*_args, **_kwargs):
            raise AssertionError("an execution request must not reach conversational LLM")

    executor.conversation_service = _ForbiddenConversation()
    response = executor._respond_conversationally(
        "형택에게 카카오톡 메시지를 보내줘", []
    )

    assert "도구 실행 증거가 없습니다" in response
    assert "완료했다고 안내하지" in response


def test_delivery_capability_phrase_never_reaches_conversation_llm(tmp_path):
    executor = _executor(tmp_path)

    class _ForbiddenConversation:
        @staticmethod
        def respond(*_args, **_kwargs):
            raise AssertionError("a delivery request must not reach conversational LLM")

    executor.conversation_service = _ForbiddenConversation()
    response = executor._respond_conversationally(
        "형택이에게 카톡으로 전달해줄 수 있어?", []
    )

    assert "도구 실행 증거가 없습니다" in response
    assert "완료했다고 안내하지" in response


def test_resumed_slot_fill_cancels_only_equivalent_recipient_variant(tmp_path):
    executor = _executor(tmp_path)
    equivalent = _create_approval(executor, "형택", "같은 본문")
    distinct = _create_approval(executor, "형택이", "같은 본문")

    current = executor.dialogue_state.create_task(
        SESSION, "카카오톡 메시지를 보내줘", workspace_path=""
    )
    current_resolution = _resolution("형택", "같은 본문")
    assert executor.dialogue_state.update_task(
        current.task_id,
        intent_name=INTENT_NAME,
        slots=current_resolution.slots,
        context_confidence=current_resolution.confidence,
    )
    assert executor.dialogue_state.transition_task(current.task_id, "awaiting_user")

    def await_approval(plan, approved_step_ids=None):
        assert approved_step_ids is None
        step = plan.steps[0]
        step.status = StepStatus.AWAITING_APPROVAL
        assert executor.dialogue_state.transition_task(
            executor.current_agent_task_id, "awaiting_approval"
        )
        return PlanRunResult(plan, "awaiting_approval", {}, [step.id])

    executor.execute_plan_dag = await_approval
    outcome = executor._execute_resolved_intent(
        current_resolution,
        current.goal,
        SESSION,
        current.task_id,
    )

    assert outcome.status == "awaiting_approval"
    assert executor.dialogue_state.get_task(SESSION, equivalent.task_id, "").status == "cancelled"
    assert executor.dialogue_state.get_task(SESSION, distinct.task_id, "").status == "awaiting_approval"
    assert executor.dialogue_state.get_task(SESSION, current.task_id, "").status == "awaiting_approval"


def test_contextual_approval_coalesces_stale_equivalents_and_executes_once(tmp_path):
    executor = _executor(tmp_path)
    first = _create_approval(executor, "형택", "동일 본문")
    second = _create_approval(executor, "형택", "동일 본문")
    calls = _install_successful_plan_runner(executor)

    outcome = executor.handle_control_command("승인", SESSION)
    statuses = {
        first.task_id: executor.dialogue_state.get_task(SESSION, first.task_id, "").status,
        second.task_id: executor.dialogue_state.get_task(SESSION, second.task_id, "").status,
    }

    assert outcome.status == "completed"
    assert len(calls) == 1
    assert sorted(statuses.values()) == ["cancelled", "completed"]


@pytest.mark.parametrize(
    ("first_recipient", "first_message", "second_recipient", "second_message"),
    [
        ("형택", "같은 본문", "민수", "같은 본문"),
        ("형택", "같은 본문", "형택이", "같은 본문"),
        ("형택", "첫 본문", "형택이", "둘째 본문"),
    ],
)
def test_contextual_approval_never_merges_distinct_send_tasks(
    tmp_path, first_recipient, first_message, second_recipient, second_message,
):
    executor = _executor(tmp_path)
    first = _create_approval(executor, first_recipient, first_message)
    second = _create_approval(executor, second_recipient, second_message)
    calls = _install_successful_plan_runner(executor)

    outcome = executor.handle_control_command("승인", SESSION)

    assert outcome.status == "awaiting_approval"
    assert "여러 개" in outcome.response
    assert calls == []
    assert executor.dialogue_state.get_task(SESSION, first.task_id, "").status == "awaiting_approval"
    assert executor.dialogue_state.get_task(SESSION, second.task_id, "").status == "awaiting_approval"


def test_exact_bare_id_executes_only_selected_task_once(tmp_path):
    executor = _executor(tmp_path)
    selected = _create_approval(executor, "형택", "첫 본문")
    other = _create_approval(executor, "민수", "둘째 본문")
    calls = _install_successful_plan_runner(executor)

    first_outcome = executor._execute_turn_impl(selected.task_id, SESSION)
    repeated_outcome = executor._execute_turn_impl(selected.task_id, SESSION)

    assert first_outcome.status == "completed"
    assert repeated_outcome.status == "completed"
    assert len(calls) == 1
    assert executor.dialogue_state.get_task(SESSION, selected.task_id, "").status == "completed"
    assert executor.dialogue_state.get_task(SESSION, other.task_id, "").status == "awaiting_approval"


def test_bare_id_for_non_awaiting_task_never_calls_llm_or_tool(tmp_path):
    executor = _executor(tmp_path)
    queued = executor.dialogue_state.create_task(
        SESSION, "아직 실행하지 않을 작업", workspace_path=""
    )

    def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("non-awaiting ID must not execute a tool")

    class _ForbiddenConversation:
        @staticmethod
        def respond(*_args, **_kwargs):
            raise AssertionError("bare task ID must not reach conversational LLM")

    executor.execute_plan_dag = forbidden_execute
    executor.conversation_service = _ForbiddenConversation()
    outcome = executor._execute_turn_impl(queued.task_id, SESSION)

    assert outcome.status == "queued"
    assert "queued 상태" in outcome.response
    assert executor.dialogue_state.get_task(SESSION, queued.task_id, "").status == "queued"


def test_awaiting_approval_can_be_revised_without_executing_old_send(tmp_path):
    executor = _executor(tmp_path)
    task = _create_approval(executor, "형택", "기존 본문")

    def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("수정 전 외부 전송 계획을 실행하면 안 됩니다")

    executor.execute_plan_dag = forbidden_execute
    outcome = executor.handle_control_command(
        f"{task.task_id} 수정: 수신자를 민수로 바꿔줘", SESSION,
    )

    assert outcome.status == "cancelled"
    assert "수신자를 민수로 바꿔줘" in outcome.next_goal
    assert "기존 본문" in outcome.next_goal
    stored = executor.dialogue_state.get_task(SESSION, task.task_id, "")
    assert stored.status == "cancelled"


def test_cancel_never_claims_success_after_approved_external_send_has_started(tmp_path):
    executor = _executor(tmp_path)
    task = _create_approval(executor, "형택", "경합 본문")
    started = threading.Event()
    release = threading.Event()
    results = {}

    def blocking_plan_runner(plan, approved_step_ids=None):
        step = plan.steps[0]
        started.set()
        assert release.wait(timeout=5)
        step.status = StepStatus.COMPLETED
        result = ToolRunResult.successful(
            tool_name=TOOL_NAME,
            raw_output="sent-once",
            evidence=[Evidence("test", "verified external send")],
        )
        return PlanRunResult(plan, "completed", {step.id: result}, [])

    executor.execute_plan_dag = blocking_plan_runner
    worker = threading.Thread(
        target=lambda: results.setdefault(
            "approval", executor.handle_control_command(f"{task.task_id} 승인", SESSION)
        )
    )
    worker.start()
    assert started.wait(timeout=5)

    cancel = executor.handle_control_command(f"{task.task_id} 취소", SESSION)
    release.set()
    worker.join(timeout=5)

    assert cancel.status == "running"
    assert "취소 완료로 표시할 수 없습니다" in cancel.response
    assert results["approval"].status == "completed"
    assert executor.dialogue_state.get_task(SESSION, task.task_id, "").status == "completed"
