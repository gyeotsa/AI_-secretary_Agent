"""Isolated semantic-to-executor regressions; no real messaging or user stores.

The model and plan coordinator are deterministic doubles, while semantic
validation, Registry contracts, dialogue persistence, result presentation and
the filesystem read plugin are real. All filesystem effects stay in tmp_path.
"""

from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from core.dialogue_state import DialogueStateStore
from core.executor import Executor
from core.intent_router import IntentRouter
from core.plan_runtime import PlanDAG, PlanRunResult, PlanStep, StepStatus
from core.plugin import BasePlugin, IntentSchema, PluginRegistry, SlotSchema, ToolCancelledError, ToolSchema
from core.semantic_request import SemanticDecision, SemanticRequestInterpreter
from core.tool_result import Evidence, ToolRunResult
from core.turn_context import TurnExecutionContext
from plugins.filesystem import FilesystemPlugin


SESSION = "semantic-executor-flow"
SEND_TOOL = "semantic_test_send"
READ_TOOL = "semantic_test_read"
WRITE_TOOL = "semantic_test_write"


def _forbidden(*_args, **_kwargs):
    raise AssertionError("semantic direct routing must not invoke a legacy planner/resolver")


class _SafeSurface(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "semantic_executor_test"
        self.calls = []

    def get_tools(self):
        return [
            ToolSchema(SEND_TOOL, "테스트 메시지 전송", {
                "type": "object", "properties": {
                    key: {"type": "string"} for key in ("provider", "recipient", "message")
                }, "required": ["provider", "recipient", "message"],
            }, side_effect="external_send"),
            ToolSchema(READ_TOOL, "테스트 파일 조회", {
                "type": "object", "properties": {"filename": {"type": "string"}},
                "required": ["filename"],
            }, side_effect="read"),
            ToolSchema(WRITE_TOOL, "테스트 파일 수정", {
                "type": "object", "properties": {
                    key: {"type": "string"} for key in ("filename", "instruction")
                }, "required": ["filename", "instruction"],
            }, side_effect="change"),
        ]

    def get_intents(self):
        return [
            IntentSchema("messaging.send", "테스트 전송", SEND_TOOL, ["카톡"], [
                SlotSchema("provider", "메신저", "어떤 메신저인가요?"),
                SlotSchema("recipient", "수신자", "누구에게 보낼까요?"),
                SlotSchema("message", "본문", "어떤 내용을 보낼까요?"),
            ], request_type="external_send"),
            IntentSchema("test.read", "테스트 조회", READ_TOOL, ["읽기"], [
                SlotSchema("filename", "파일명", "어떤 파일인가요?"),
            ], request_type="query"),
            IntentSchema("test.write", "테스트 수정", WRITE_TOOL, ["수정"], [
                SlotSchema("filename", "파일명", "어떤 파일인가요?"),
                SlotSchema("instruction", "수정 내용", "어떻게 수정할까요?"),
            ], request_type="change"),
        ]

    def execute_tool(self, tool_name, tool_input):
        # Even a broken approval route cannot cause an external effect here.
        assert tool_name != SEND_TOOL, "a semantic test must never send a real message"
        self.calls.append((tool_name, deepcopy(tool_input)))
        return ToolRunResult.successful(
            tool_name=tool_name, raw_output="isolated test result",
            evidence=[Evidence("test", "in-memory test plugin result")],
        )


class _Decisions:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.calls = []

    def interpret(self, raw_text, **kwargs):
        self.calls.append({"raw_text": raw_text, **deepcopy(kwargs)})
        assert self.decisions, "unexpected semantic interpretation"
        fields = self.decisions.pop(0)
        return SemanticDecision(raw_text=raw_text, **fields)


class _ScriptedModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def chat(self, messages):
        self.prompts.append(json.loads(messages[-1]["content"]))
        assert self.outputs, "literal continuation must not make another model call"
        return json.dumps(self.outputs.pop(0), ensure_ascii=False)


def _decision(intent="test.read", *, relation="new", **changes):
    tool, operation = {
        "test.read": (READ_TOOL, "read"),
        "test.write": (WRITE_TOOL, "change"),
        "messaging.send": (SEND_TOOL, "external_send"),
        "filesystem.read_file": ("filesystem_read_file", "read"),
    }[intent]
    return {
        "relation": relation, "operation": operation, "intent_name": intent,
        "tool_names": (tool,), "slots": {}, "confidence": 0.99,
        "grounded": True, "source": "test", **changes,
    }


def _model_output(**changes):
    return {
        "relation": "new", "operation": "external_send",
        "tool_names": [SEND_TOOL],
        "slots": {"provider": "kakaotalk", "recipient": "김하이"},
        "confidence": 0.99, "needs_clarification": False,
        "clarification_question": "", "control_scope": "current", **changes,
    }


@pytest.fixture
def make_executor(tmp_path, monkeypatch):
    workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path), is_set=lambda: True)
    monkeypatch.setattr("plugins.filesystem.get_workspace_manager", lambda: workspace)
    monkeypatch.setattr("core.executor.record_runtime_event", lambda *_a, **_k: None)

    def create(*decisions, model=None):
        executor = Executor.__new__(Executor)
        executor.dialogue_state = DialogueStateStore(str(tmp_path / "semantic-dialogue.db"))
        executor.workspace_manager = workspace
        registry = PluginRegistry()
        surface = _SafeSurface()
        registry.register_plugin(surface)
        registry.register_plugin(FilesystemPlugin())
        executor.intent_router = IntentRouter(registry)
        executor.semantic_interpreter = (
            SemanticRequestInterpreter(model, registry) if model is not None else _Decisions(*decisions)
        )
        executor.learning_runtime = SimpleNamespace(
            begin=lambda *_a, **_k: "isolated-trace", finish=lambda *_a, **_k: None,
            event=lambda *_a, **_k: None,
        )
        executor.quality_metrics = SimpleNamespace(record=lambda *_a, **_k: None)
        executor.context_manager = None
        executor.response_realizer = None
        executor.current_agent_task_id = ""
        executor._task_controls = {}
        executor._approval_inflight = set()
        executor._control_condition = threading.Condition()
        executor.context_resolver = SimpleNamespace(resolve=_forbidden)
        executor.planner = SimpleNamespace(decompose_goal=_forbidden, build_plan=_forbidden)
        executor.plan_calls = []
        executor.conversation_calls = []
        executor.test_surface = surface

        def conversation(goal, history):
            executor.conversation_calls.append((goal, deepcopy(history)))
            return "테스트 대화 응답입니다. 작업은 실행하지 않았습니다."

        executor._respond_conversationally = conversation

        def run_plan(plan, approved_step_ids=None):
            executor.plan_calls.append(plan)
            assert len(plan.steps) == 1
            step = plan.steps[0]
            if step.requires_approval:
                assert approved_step_ids is None, "these tests do not authorize external sends"
                step.status = StepStatus.AWAITING_APPROVAL
                assert executor.dialogue_state.transition_task(
                    executor.current_agent_task_id, "awaiting_approval"
                )
                return PlanRunResult(plan, "awaiting_approval", {}, [step.id])
            result = registry.execute_tool(step.tool_name, step.tool_input)
            assert isinstance(result, ToolRunResult)
            step.status = StepStatus.COMPLETED if result.succeeded else StepStatus.FAILED
            return PlanRunResult(plan, "completed" if result.succeeded else "failed", {step.id: result}, [])

        executor.execute_plan_dag = run_plan
        return executor

    return create


def _pending_send(executor, original="김하이에게 카카오톡 메시지를 보내줘"):
    workspace = executor._workspace_scope()
    task = executor.dialogue_state.create_task(SESSION, original, workspace_path=workspace)
    slots = {"provider": "kakaotalk", "recipient": "김하이"}
    executor.dialogue_state.save_intent_state(task.task_id, SESSION, "messaging.send", slots, original)
    executor.dialogue_state.create(SESSION, original, "어떤 내용을 보낼까요?", [], task.task_id, workspace)
    executor.dialogue_state.transition_task(task.task_id, "awaiting_user")
    return task


def test_semantic_clarification_reuses_persisted_task_and_original_goal(make_executor):
    original = "카카오톡 메시지를 보내줘"
    first_executor = make_executor(_decision("messaging.send", slots={"provider": "kakaotalk"}))
    first = first_executor.execute_turn(original, SESSION)
    assert first.status == "awaiting_user"

    second_executor = make_executor(
        _decision("messaging.send", relation="continue", slots={"provider": "kakaotalk", "recipient": "김하이"}),
        _decision("messaging.send", relation="continue", slots={
            "provider": "kakaotalk", "recipient": "김하이", "message": "내일  학교에서 만나",
        }),
    )
    second = second_executor.execute_turn("김하이", SESSION)
    third = second_executor.execute_turn('"내일  학교에서 만나"', SESSION)

    assert second.status == "awaiting_user"
    assert third.status == "awaiting_approval"
    assert first.task_id == second.task_id == third.task_id
    stored = second_executor.dialogue_state.get_task(SESSION, first.task_id)
    assert stored.goal == original
    assert stored.slots["message"] == "내일  학교에서 만나"
    assert second_executor.plan_calls[0].goal == original
    assert len(second_executor.dialogue_state.list_tasks(SESSION)) == 1
    assert second_executor.dialogue_state.get(SESSION, first.task_id, second_executor._workspace_scope()) is None
    assert second_executor.test_surface.calls == []


def test_real_interpreter_literal_reply_preserves_body_across_restart(make_executor):
    model = _ScriptedModel(_model_output())
    first_executor = make_executor(model=model)
    first = first_executor.execute_turn("김하이에게 카카오톡 메시지를 보내줘", SESSION)
    assert first.status == "awaiting_user"

    restarted_model = _ScriptedModel()
    restarted = make_executor(model=restarted_model)
    body = "취소 라는 단어도 그대로\nx = [1,  2]  # 공백 유지"
    outcome = restarted.execute_turn(f'"{body}"라고 보내줘', SESSION)

    assert outcome.status == "awaiting_approval"
    assert outcome.task_id == first.task_id
    assert restarted_model.prompts == []
    step = restarted.plan_calls[0].steps[0]
    assert step.tool_input == {"provider": "kakaotalk", "recipient": "김하이", "message": body}
    assert restarted.test_surface.calls == []


def test_new_read_topic_does_not_consume_pending_message(make_executor):
    executor = make_executor(_decision(slots={"filename": "status.txt"}))
    pending = _pending_send(executor)
    before = executor.dialogue_state.get_intent_state(pending.task_id)

    outcome = executor.execute_turn("status.txt 파일을 읽어줘", SESSION)

    assert outcome.status == "completed"
    assert outcome.task_id != pending.task_id
    assert executor.dialogue_state.get_intent_state(pending.task_id) == before
    assert executor.dialogue_state.get_task(SESSION, pending.task_id).status == "awaiting_user"
    assert executor.dialogue_state.get(SESSION, pending.task_id, executor._workspace_scope()) is not None
    assert executor.test_surface.calls == [(READ_TOOL, {"filename": "status.txt"})]


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "awaiting_approval", "interrupted"])
def test_recent_referents_exclude_uncompleted_tasks(make_executor, status):
    executor = make_executor({"relation": "conversation", "operation": "conversation", "grounded": True})
    scope = executor._workspace_scope()
    completed = executor.dialogue_state.create_task(SESSION, "완료한 파일", workspace_path=scope)
    executor.dialogue_state.update_task(
        completed.task_id, status="completed", intent_name="test.read", slots={"filename": "confirmed.txt"}
    )
    newer = executor.dialogue_state.create_task(SESSION, "아직 완료하지 않은 파일", workspace_path=scope)
    executor.dialogue_state.update_task(
        newer.task_id, status=status, intent_name="test.read", slots={"filename": "unconfirmed.txt"}
    )

    executor.execute_turn("그 파일에 대해 이야기하자", SESSION)

    recent = executor.semantic_interpreter.calls[0]["pending"]["recent_completed"]
    assert recent["task_id"] == completed.task_id
    assert recent["slots"] == {"filename": "confirmed.txt"}
    assert executor.plan_calls == []


def test_legacy_recent_entry_without_completed_task_is_not_a_referent(make_executor):
    executor = make_executor({"relation": "conversation", "operation": "conversation", "grounded": True})
    executor.dialogue_state.save_recent_intent(SESSION, "test.read", {"filename": "legacy.txt"}, "기존 요청")

    executor.execute_turn("그 파일에 대해 이야기하자", SESSION)

    assert executor.semantic_interpreter.calls[0]["pending"] == {}
    assert executor.plan_calls == []


@pytest.mark.parametrize("foreign_scope", ["session", "workspace"])
def test_completed_referents_stay_in_current_session_and_workspace(make_executor, foreign_scope):
    executor = make_executor({"relation": "conversation", "operation": "conversation", "grounded": True})
    scope = executor._workspace_scope()
    local = executor.dialogue_state.create_task(SESSION, "현재 범위의 완료 파일", workspace_path=scope)
    executor.dialogue_state.update_task(
        local.task_id, status="completed", intent_name="test.read", slots={"filename": "local.txt"}
    )
    foreign = executor.dialogue_state.create_task(
        "other-session" if foreign_scope == "session" else SESSION,
        "다른 범위의 완료 파일",
        workspace_path=scope if foreign_scope == "session" else scope + "-other",
    )
    executor.dialogue_state.update_task(
        foreign.task_id, status="completed", intent_name="test.read", slots={"filename": "foreign.txt"}
    )

    executor.execute_turn("그 파일에 대해 이야기하자", SESSION)

    recent = executor.semantic_interpreter.calls[0]["pending"]["recent_completed"]
    assert recent["task_id"] == local.task_id
    assert recent["slots"] == {"filename": "local.txt"}
    assert executor.plan_calls == []


def test_correction_reuses_pending_task_without_normalizing_recipient(make_executor):
    executor = make_executor(_decision("messaging.send", relation="correct", slots={
        "provider": "kakaotalk", "recipient": "김하", "message": "내일  만나",
    }))
    pending = _pending_send(executor)

    outcome = executor.execute_turn('김하이가 아니라 김하에게 "내일  만나"라고 보내줘', SESSION)

    assert outcome.status == "awaiting_approval"
    assert outcome.task_id == pending.task_id
    stored = executor.dialogue_state.get_task(SESSION, pending.task_id)
    assert stored.goal == pending.goal
    assert stored.slots == {"provider": "kakaotalk", "recipient": "김하", "message": "내일  만나"}
    assert executor.plan_calls[0].goal == pending.goal
    assert executor.test_surface.calls == []


def test_unresolved_clarification_keeps_task_identity_when_later_grounded(make_executor):
    original = "문서 안의 내용을 확인해줘"
    executor = make_executor(
        {"relation": "new", "needs_clarification": True,
         "clarification_question": "어떤 문서의 내용을 확인할까요?"},
        _decision(relation="continue", slots={"filename": "notes.txt"}),
    )

    first = executor.execute_turn(original, SESSION)
    second = executor.execute_turn("notes.txt", SESSION)

    assert first.status == "awaiting_user"
    assert second.status == "completed"
    assert first.task_id == second.task_id
    assert executor.dialogue_state.get_task(SESSION, first.task_id).goal == original
    assert executor.plan_calls[0].goal == original
    assert executor.semantic_interpreter.calls[1]["raw_text"] == "notes.txt"
    assert executor.semantic_interpreter.calls[1]["pending"]["original_request"] == original


@pytest.mark.parametrize("grounded", [True, False])
def test_uncertain_cancel_never_executes_or_cancels_pending_send(make_executor, grounded):
    executor = make_executor({
        "relation": "cancel", "operation": "control", "grounded": grounded,
        "tool_names": (SEND_TOOL,), "needs_clarification": True,
        "clarification_question": "어떤 작업을 취소할까요?",
    })
    pending = _pending_send(executor)

    outcome = executor.execute_turn("그중 예전에 이야기했던 건 그만둘까", SESSION)

    assert outcome.status == "awaiting_user"
    assert executor.dialogue_state.get_task(SESSION, pending.task_id).status == "awaiting_user"
    assert executor.dialogue_state.get_intent_state(pending.task_id)["slots"].get("message") is None
    assert executor.plan_calls == []
    assert executor.test_surface.calls == []


@pytest.mark.parametrize("raw", [
    "note.txt 파일을 수정하지 마",
    "내가 승인하면 note.txt 파일을 수정해줘",
])
def test_negated_or_conditional_write_has_no_effect_even_with_grounded_decision(make_executor, raw):
    executor = make_executor(_decision("test.write", slots={"filename": "note.txt", "instruction": "내용 교체"}))

    outcome = executor.execute_turn(raw, SESSION)

    assert outcome.status != "completed" or not outcome.tool_results
    assert executor.plan_calls == []
    assert executor.test_surface.calls == []


def test_real_semantic_read_returns_exact_source_and_does_not_write(make_executor, tmp_path):
    filename = "Agent 인수인계.txt"
    source = "첫 줄:  원문 유지\r\nx = [1,  2]\r\n'취소'는 파일 내용\r\n"
    target = tmp_path / filename
    original_bytes = source.encode("utf-8")
    target.write_bytes(original_bytes)
    model = _ScriptedModel(_model_output(
        operation="read", tool_names=["filesystem_read_file"], slots={"filename": filename},
    ))
    executor = make_executor(model=model)

    outcome = executor.execute_turn(f"{filename}에 작성되어 있는 내용을 읽어줘", SESSION)

    assert outcome.status == "completed"
    assert len(executor.plan_calls) == 1
    assert executor.plan_calls[0].steps[0].tool_name == "filesystem_read_file"
    assert outcome.tool_result.succeeded
    payload = json.loads(outcome.tool_result.raw_output)
    assert payload["content"] == source
    assert payload["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert outcome.tool_result.evidence[0].kind == "file_content"
    assert source in outcome.response
    assert target.read_bytes() == original_bytes
    assert executor.test_surface.calls == []
    assert model.prompts[0]["current_user_input"] == f"{filename}에 작성되어 있는 내용을 읽어줘"


def test_semantic_model_failure_leaves_existing_pending_request_untouched(make_executor):
    executor = make_executor({"reason": "semantic_interpretation_failed:RuntimeError"})
    pending = _pending_send(executor)

    outcome = executor.execute_turn("다른 파일도 확인해줘", SESSION)

    assert outcome.status == "failed"
    assert executor.dialogue_state.get_task(SESSION, pending.task_id).status == "awaiting_user"
    assert executor.dialogue_state.get_intent_state(pending.task_id)["slots"].get("message") is None
    assert executor.plan_calls == []


def _cancel_before_execution(*_args, **_kwargs):
    raise ToolCancelledError("isolated test cancellation before any effect")


def _generic_executor(make_executor):
    executor = make_executor(_decision(
        tool_names=(READ_TOOL, "filesystem_read_file"), slots={"filename": "notes.txt"},
    ))
    executor.initialize = lambda goal, _session_id: setattr(executor, "goal", goal)
    executor.build_context = lambda: ""
    executor.planning_service = SimpleNamespace(create=lambda goal, *_args: PlanDAG(goal, [
        PlanStep(id="test-read", tool_name=READ_TOOL, tool_input={"filename": "notes.txt"}),
        PlanStep(id="source-read", tool_name="filesystem_read_file", tool_input={"filename": "notes.txt"}),
    ]))
    return executor


@pytest.mark.parametrize("route", ["resolved", "generic_dag"])
def test_cancel_before_plan_effect_closes_persisted_running_task(make_executor, route):
    executor = (_generic_executor(make_executor) if route == "generic_dag" else
                make_executor(_decision(slots={"filename": "notes.txt"})))
    executor.execute_plan_dag = _cancel_before_execution

    outcome = executor.execute_turn("notes.txt 내용을 읽고 확인해줘", SESSION)

    tasks = executor.dialogue_state.list_tasks(SESSION)
    assert outcome.status == "cancelled"
    assert len(tasks) == 1
    assert tasks[0].status == "cancelled", "a cancelled turn must not leave a persisted running task"
    assert executor.current_agent_task_id == ""
    assert executor._task_controls == {}
    assert executor.test_surface.calls == []


def test_cancel_during_generic_planning_is_not_reported_as_provider_failure(make_executor):
    executor = _generic_executor(make_executor)
    executor.planning_service = SimpleNamespace(create=_cancel_before_execution)

    outcome = executor.execute_turn("notes.txt 내용을 읽고 확인해줘", SESSION)

    tasks = executor.dialogue_state.list_tasks(SESSION)
    assert outcome.status == "cancelled"
    assert len(tasks) == 1
    assert tasks[0].status == "cancelled"
    assert executor.current_agent_task_id == ""
    assert executor._task_controls == {}
    assert executor.test_surface.calls == []


def _verified_stub_run(plan, **_kwargs):
    """Represent verified completion, without invoking any actual send plugin."""
    step = plan.steps[0]
    step.status = StepStatus.COMPLETED
    result = ToolRunResult.successful(
        tool_name=step.tool_name, raw_output="verified isolated delivery result",
        evidence=[Evidence("test_delivery_receipt", "isolated verified receipt", {"receipt": "test-only"})],
    )
    return PlanRunResult(plan, "completed", {step.id: result}, [])


def test_cancel_during_presentation_keeps_verified_resolved_completion(make_executor, monkeypatch):
    executor = make_executor(_decision(slots={"filename": "notes.txt"}))
    executor.execute_plan_dag = _verified_stub_run
    monkeypatch.setattr(executor.intent_router.registry, "present_result", _cancel_before_execution)

    outcome = executor.execute_turn("notes.txt 내용을 읽어줘", SESSION)

    stored = executor.dialogue_state.list_tasks(SESSION)[0]
    assert stored.status == "completed"
    assert stored.evidence[0]["kind"] == "test_delivery_receipt"
    assert outcome.status == "completed", "presentation cancellation cannot undo a verified operation"
    assert outcome.task_id == stored.task_id
    assert outcome.tool_result.succeeded
    assert outcome.tool_result.evidence
    assert executor.test_surface.calls == []


def test_cancel_during_approved_send_presentation_does_not_overwrite_delivery_evidence(make_executor, monkeypatch):
    executor = make_executor(_decision("messaging.send", slots={
        "provider": "kakaotalk", "recipient": "김하이", "message": "isolated test only",
    }))
    pending = executor.execute_turn('김하이에게 카카오톡으로 "isolated test only"를 보내줘', SESSION)
    assert pending.status == "awaiting_approval"
    executor.execute_plan_dag = _verified_stub_run
    monkeypatch.setattr(executor.intent_router.registry, "present_result", _cancel_before_execution)

    outcome = executor.execute_turn("승인", SESSION)

    stored = executor.dialogue_state.get_task(SESSION, pending.task_id)
    assert stored.status == "completed", "post-delivery cancellation must not rewrite completed to failed"
    assert stored.verification_status == "completed"
    assert stored.evidence[0]["kind"] == "test_delivery_receipt"
    assert outcome.status == "completed"
    assert outcome.task_id == pending.task_id
    assert outcome.tool_result.succeeded
    assert executor.test_surface.calls == []


def test_late_turn_checkpoint_preserves_already_verified_completion(make_executor):
    executor = make_executor(_decision(slots={"filename": "notes.txt"}))
    context = TurnExecutionContext("isolated-late-cancel", SESSION, executor._workspace_scope())

    def complete_then_cancel(plan, **kwargs):
        run = _verified_stub_run(plan, **kwargs)
        context.cancel()
        return run

    executor.execute_plan_dag = complete_then_cancel

    outcome = executor.execute_turn("notes.txt 내용을 읽어줘", SESSION, turn_context=context)

    stored = executor.dialogue_state.list_tasks(SESSION)[0]
    assert stored.status == "completed"
    assert stored.evidence
    assert outcome.status == "completed", "a late checkpoint must retain the evidence-bearing outcome"
    assert outcome.task_id == stored.task_id
    assert outcome.tool_result.succeeded
    assert executor.test_surface.calls == []


@pytest.mark.parametrize("content", ["hello", None])
def test_semantic_fast_path_cannot_drop_explicit_file_body(make_executor, content):
    slots = {"filename": "memo.txt"}
    if content is not None:
        slots["content"] = content
    executor = make_executor(model=_ScriptedModel(_model_output(
        operation="change", tool_names=["filesystem_create_file"], slots=slots,
    )))
    reached_execution = []

    def prevent_any_effect(plan, **_kwargs):
        reached_execution.append(plan)
        raise ToolCancelledError("test stopped a lossy call before any tool effect")

    executor.execute_plan_dag = prevent_any_effect
    # A repair/planning route may stop safely, but must not execute the original
    # lossy fast-path call. No filesystem tool is invoked by this test.
    executor.initialize = lambda goal, _session_id: setattr(executor, "goal", goal)
    executor.build_context = lambda: ""
    executor.planning_service = SimpleNamespace(create=_cancel_before_execution)

    executor.execute_turn('memo.txt 파일을 만들어줘. 내용은 "hello world"', SESSION)

    assert reached_execution == [], "explicit body was shortened/dropped before a schema-valid tool call"
    assert executor.test_surface.calls == []


def test_semantic_compound_tools_are_mandatory_planning_requirements(make_executor):
    executor = make_executor(_decision(
        operation="change", tool_names=(READ_TOOL, WRITE_TOOL), slots={"filename": "notes.txt"},
    ))
    executor.initialize = lambda goal, _session_id: setattr(executor, "goal", goal)
    executor.build_context = lambda: ""
    planning_calls = []

    def capture_requirements(goal, context, allowed_tools, required_tools):
        planning_calls.append((list(allowed_tools), list(required_tools)))
        raise ToolCancelledError("test stops at the planner boundary without execution")

    executor.planning_service = SimpleNamespace(create=capture_requirements)

    outcome = executor.execute_turn("notes.txt 파일을 읽고 내용을 수정해줘", SESSION)

    assert outcome.status == "cancelled"
    assert planning_calls == [([READ_TOOL, WRITE_TOOL], [READ_TOOL, WRITE_TOOL])]
    assert executor.plan_calls == []
    assert executor.test_surface.calls == []
