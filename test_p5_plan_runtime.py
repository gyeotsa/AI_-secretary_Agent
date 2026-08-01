import time

import pytest

from core.plan_runtime import (
    PlanCoordinator, PlanDAG, PlanExecutionStore, PlanStep, StepStatus,
)
from core.recovery import RecoveryManager
from core.scratchpad import Task
from core.dialogue_state import DialogueStateStore
from core.tool_result import Artifact, Evidence, ToolRunResult


def success(step, artifact=None):
    return ToolRunResult.successful(
        tool_name=step.tool_name or step.id,
        raw_output="ok",
        evidence=[Evidence("test", "verified")],
        artifacts=[artifact] if artifact else [],
    )


def test_plan_dag_rejects_cycles_and_missing_dependencies():
    with pytest.raises(ValueError, match="순환"):
        PlanDAG("cycle", [PlanStep("a", dependencies=["b"]), PlanStep("b", dependencies=["a"])])
    with pytest.raises(ValueError, match="존재하지 않는"):
        PlanDAG("missing", [PlanStep("a", dependencies=["unknown"])])


def test_independent_steps_run_in_parallel_and_dependency_waits(tmp_path):
    timeline = {}
    plan = PlanDAG("parallel", [
        PlanStep("a", tool_name="a"),
        PlanStep("b", tool_name="b"),
        PlanStep("c", tool_name="c", dependencies=["a", "b"]),
    ])

    def execute(step, strategy):
        timeline[f"{step.id}_start"] = time.perf_counter()
        if step.id in {"a", "b"}:
            time.sleep(0.12)
        timeline[f"{step.id}_end"] = time.perf_counter()
        return success(step)

    coordinator = PlanCoordinator(PlanExecutionStore(str(tmp_path / "plan.db")))
    started = time.perf_counter()
    result = coordinator.run(plan, execute, lambda _step, candidate: candidate)
    elapsed = time.perf_counter() - started
    assert result.status == "completed"
    assert timeline["a_start"] < timeline["b_end"] and timeline["b_start"] < timeline["a_end"]
    assert timeline["c_start"] >= max(timeline["a_end"], timeline["b_end"])


def test_recovery_reuses_verifier_and_blocks_duplicate_failure(tmp_path):
    plan = PlanDAG("retry", [PlanStep("a", tool_name="demo", retry_budget=5)])
    calls = {"execute": 0, "verify": 0}

    def execute(step, strategy):
        calls["execute"] += 1
        return ToolRunResult.failed(tool_name="demo", error="Timeout after 123 seconds")

    def verify(step, candidate):
        calls["verify"] += 1
        return candidate

    coordinator = PlanCoordinator(
        PlanExecutionStore(str(tmp_path / "retry.db")), duplicate_failure_limit=2
    )
    result = coordinator.run(plan, execute, verify)
    assert result.status == "failed"
    assert calls == {"execute": 2, "verify": 2}
    assert plan.steps[0].attempts == 2
    assert plan.steps[0].last_error_signature


def test_recovered_attempt_is_verified_and_expected_artifact_checked(tmp_path):
    step = PlanStep("a", tool_name="write", retry_budget=1,
                    expected_artifacts=[{"kind": "file", "uri": "out.txt"}])
    plan = PlanDAG("artifact", [step])

    def execute(current, strategy):
        artifact = Artifact("file", "wrong.txt" if current.attempts == 1 else "out.txt")
        return success(current, artifact)

    verified = []
    result = PlanCoordinator(PlanExecutionStore(str(tmp_path / "artifact.db"))).run(
        plan, execute, lambda current, candidate: verified.append(current.attempts) or candidate
    )
    assert result.status == "completed"
    assert verified == [1, 2]


def test_human_approval_is_explicit_pause_point(tmp_path):
    called = []
    plan = PlanDAG("approval", [PlanStep(
        "send", tool_name="mail_send", requires_approval=True,
        approval_reason="외부 수신자에게 메일을 보냅니다.",
    )])
    result = PlanCoordinator(PlanExecutionStore(str(tmp_path / "approval.db"))).run(
        plan, lambda step, strategy: called.append(step.id) or success(step),
        lambda step, candidate: candidate,
    )
    assert result.status == "awaiting_approval"
    assert result.awaiting_approval == ["send"]
    assert called == []
    assert plan.steps[0].status == StepStatus.AWAITING_APPROVAL

    resumed = PlanCoordinator(PlanExecutionStore(str(tmp_path / "approval.db"))).resume_approved(
        plan, ["send"], lambda step, strategy: called.append(step.id) or success(step),
        lambda step, candidate: candidate,
    )
    assert resumed.status == "completed"
    assert called == ["send"]


def test_observation_can_replace_plan_with_new_revision(tmp_path):
    original = PlanDAG("replan", [PlanStep("old", tool_name="old", retry_budget=0)])

    def execute(step, strategy):
        if step.id == "old":
            return ToolRunResult.failed(tool_name="old", error="validation mismatch")
        return success(step)

    def replan(plan, failed_step, result):
        return PlanDAG(plan.goal, [PlanStep("new", tool_name="new")])

    outcome = PlanCoordinator(PlanExecutionStore(str(tmp_path / "replan.db"))).run(
        original, execute, lambda step, candidate: candidate, replan=replan
    )
    assert outcome.status == "completed"
    assert outcome.plan.revision == 2
    assert outcome.plan.steps[0].id == "new"


def test_legacy_recovery_uses_budget_signature_and_same_verifier(tmp_path):
    manager = RecoveryManager.__new__(RecoveryManager)
    manager.max_retries = 5
    manager.coordinator = PlanCoordinator(
        PlanExecutionStore(str(tmp_path / "legacy.db")), duplicate_failure_limit=2
    )

    class FailingTools:
        calls = 0

        def execute_tool(self, name, value):
            self.calls += 1
            return ToolRunResult.failed(tool_name=name, error="Timeout 999")

    manager.tool_executor = FailingTools()
    verified = []
    result = manager.recover(
        Task("task", "demo"), "demo", {}, "initial", 0,
        verify_callback=lambda candidate: verified.append(candidate) or candidate,
    )
    assert result.success is False
    assert result.repeated_failure_blocked is True
    assert result.retry_count == 2
    assert len(verified) == 2


def test_plan_and_approval_checkpoint_survive_restart(tmp_path):
    plan_db = tmp_path / "plans.db"
    store = PlanExecutionStore(str(plan_db))
    plan = PlanDAG("persist", [PlanStep(
        "publish", tool_name="publish", requires_approval=True,
        approval_reason="외부 공개 변경",
    )])
    coordinator = PlanCoordinator(store)
    outcome = coordinator.run(plan, lambda step, strategy: success(step),
                              lambda step, candidate: candidate)
    assert outcome.status == "awaiting_approval"
    restored = PlanExecutionStore(str(plan_db)).load_plan(plan.plan_id)
    assert restored is not None
    assert restored.steps[0].status == StepStatus.AWAITING_APPROVAL
    assert restored.steps[0].approval_reason == "외부 공개 변경"

    state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    task = state.create_task("session", "publish")
    state.transition_task(task.task_id, "running")
    state.update_task(task.task_id, plan_id=plan.plan_id, plan=plan.to_dict()["steps"])
    assert state.transition_task(task.task_id, "awaiting_approval")
    loaded = DialogueStateStore(str(tmp_path / "dialogue.db")).get_task("session", task.task_id)
    assert loaded.plan_id == plan.plan_id
    assert loaded.status == "awaiting_approval"
