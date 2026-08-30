from datetime import datetime, timedelta, timezone
import json

import pytest

from core.acceptance_runtime import AcceptanceRuntime
from core.quality_metrics import AcceptanceScenarioEvaluator, QualityMetricStore


def test_live_acceptance_cannot_pass_without_operator_and_complete_evidence(tmp_path):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    report = tmp_path / "device.json"
    report.write_text("{}", encoding="utf-8")
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"audio")
    with pytest.raises(ValueError, match="사용자"):
        runtime.record("speaker_tts", "passed", evidence=[
            {"kind": "device_report", "value": str(report)},
            {"kind": "audio", "value": str(audio)},
        ])
    with pytest.raises(ValueError, match="필수 증거"):
        runtime.record("speaker_tts", "passed", operator="tester", evidence=[
            {"kind": "device_report", "value": str(report)},
        ])


def test_acceptance_is_persistent_sanitized_and_fail_closed(tmp_path):
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    report = tmp_path / "device.json"
    report.write_text("Bearer secret-token output ok", encoding="utf-8")
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"audio")
    runtime.record("speaker_tts", "passed", operator="tester", evidence=[
        {"kind": "device_report", "value": str(report)},
        {"kind": "audio", "value": str(audio)},
    ])
    runtime.record("oauth_roundtrip", "blocked", blockers=["시험 계정이 필요합니다."])
    snapshot = AcceptanceRuntime(str(path)).snapshot()
    rows = {item["key"]: item for item in snapshot["scenarios"]}
    assert rows["speaker_tts"]["status"] == "passed"
    assert rows["oauth_roundtrip"]["status"] == "blocked"
    assert snapshot["all_passed"] is False
    assert "secret-token" not in path.read_text(encoding="utf-8")


def test_expired_live_evidence_does_not_count_as_complete(tmp_path):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    report = tmp_path / "eval.json"
    report.write_text("{}", encoding="utf-8")
    runtime.record("conversation_human_eval", "passed", operator="tester", evidence=[
        {"kind": "evaluation_report", "value": str(report)},
    ])
    future = datetime.now(timezone.utc) + timedelta(days=15)
    snapshot = runtime.snapshot(now=future)
    row = next(item for item in snapshot["scenarios"] if item["key"] == "conversation_human_eval")
    assert row["status"] == "expired"
    assert snapshot["all_passed"] is False


def test_blocked_requires_actionable_reason(tmp_path):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    with pytest.raises(ValueError, match="차단 사유"):
        runtime.record("kakao_delivery", "blocked")


def test_corrupt_or_unknown_records_never_become_passed(tmp_path):
    path = tmp_path / "acceptance.json"
    path.write_text(json.dumps({"results": {
        "speaker_tts": {"status": "magic"},
        "unknown": {"status": "passed"},
    }}), encoding="utf-8")
    snapshot = AcceptanceRuntime(str(path)).snapshot()
    assert all(item["status"] == "not_run" for item in snapshot["scenarios"])


def test_local_evidence_must_exist_and_be_nonempty(tmp_path):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    with pytest.raises(ValueError, match="증거 파일"):
        runtime.record("wall_clock_soak", "passed", operator="tester", evidence=[
            {"kind": "soak_report", "value": str(tmp_path / "missing.json")},
        ])


def test_quality_gate_rejects_lucky_single_sample(tmp_path):
    store = QualityMetricStore(str(tmp_path / "quality.db"))
    store.record("task_success", 1.0, success=True)
    result = AcceptanceScenarioEvaluator(store).evaluate()
    row = next(item for item in result["scenarios"] if item["key"] == "task_success")
    assert row["status"] == "insufficient"
    assert row["sample_count"] == 1
    assert result["all_passed"] is False
