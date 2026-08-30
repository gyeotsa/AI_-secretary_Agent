from pathlib import Path

from core.evaluation_runtime import ApplicationEvaluator
from core.learning_runtime import EvaluationCase, LearningRuntime
from core.tool_result import Evidence, ToolRunResult
from core.quality_metrics import AcceptanceScenarioEvaluator, QualityMetricStore


def test_execution_contract_accepts_evidence_backed_success():
    result = ToolRunResult.successful(
        tool_name="filesystem_create_file",
        raw_output="파일 생성을 완료했습니다.",
        evidence=[Evidence("file_content", "저장 후 내용을 다시 읽었습니다.")],
    )
    passed, details = ApplicationEvaluator.check(result, {
        "required_paths": ["tool_name", "status", "evidence.0.kind"],
        "path_equals": {"tool_name": "filesystem_create_file", "status": "succeeded"},
        "path_in": {"evidence.0.kind": ["file_content", "file_hash"]},
        "evidence_required_when_succeeded": True,
    })
    assert passed, details


def test_execution_contract_rejects_false_completion_without_evidence():
    passed, details = ApplicationEvaluator.check({
        "tool_name": "desktop_send_message",
        "status": "succeeded",
        "response": "카카오톡 전송을 완료했습니다.",
        "evidence": [],
    }, {"evidence_required_when_succeeded": True})
    assert not passed
    assert "증거" in details


def test_execution_contract_rejects_success_claim_on_failed_payload():
    passed, details = ApplicationEvaluator.check({
        "status": "failed", "response": "작업을 완료했습니다.", "evidence": [],
    }, {"required_paths": ["status"]})
    assert not passed
    assert "증거" in details


def test_execution_contract_validates_nested_paths_and_allowed_values():
    passed, details = ApplicationEvaluator.check({
        "status": "partial", "verification": {"status": "unverified"},
        "evidence": [{"kind": "tool_error"}],
    }, {
        "required_paths": ["verification.status"],
        "path_in": {"status": ["partial", "failed"],
                    "verification.status": ["unverified"]},
    })
    assert passed, details


def test_application_evaluator_runs_persistent_contract_cases(tmp_path: Path):
    runtime = LearningRuntime(str(tmp_path / "learning.db"))
    runtime.upsert_case(EvaluationCase(
        "send.evidence", "execution_contract", "민수에게 카톡 보내줘",
        {"path_equals": {"status": "succeeded"},
         "required_paths": ["evidence.0.kind"],
         "evidence_required_when_succeeded": True},
    ))
    evaluator = ApplicationEvaluator(runtime)
    results = evaluator.run(lambda _prompt: {
        "status": "succeeded", "response": "전송을 완료했습니다.",
        "evidence": [{"kind": "outgoing_message_evidence"}],
    }, "execution_contract")
    assert len(results) == 1 and results[0].passed
    assert evaluator.gate(results, minimum_pass_rate=1.0)["passed"]


def test_specialist_quality_metrics_need_real_sample_volume(tmp_path: Path):
    store = QualityMetricStore(str(tmp_path / "quality.db"))
    store.record("specialist_artifact_quality", 1.0, success=True)
    store.record("mockup_visual_approval", 1.0, success=True)
    scenarios = {item["key"]: item for item in AcceptanceScenarioEvaluator(store).evaluate()["scenarios"]}
    assert scenarios["specialist_artifact_quality"]["status"] == "insufficient"
    assert scenarios["mockup_visual_approval"]["status"] == "insufficient"
