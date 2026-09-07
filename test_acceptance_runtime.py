from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os

import pytest

from core.acceptance_runtime import AcceptanceRuntime, AcceptanceScenario
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


def _row(runtime, key="packaged_runtime", **kwargs):
    return next(item for item in runtime.snapshot(**kwargs)["scenarios"] if item["key"] == key)


def _record_runtime(tmp_path):
    path = tmp_path / "acceptance.json"
    report = tmp_path / "runtime.json"
    report.write_text('{"status":"healthy"}', encoding="utf-8")
    runtime = AcceptanceRuntime(str(path))
    runtime.record("packaged_runtime", "passed", evidence=[
        {"kind": "runtime_report", "value": str(report)},
    ])
    return runtime, path, report


def _remote_receipt(*, kind="remote_receipt", value="receipt-1",
                    operation="message_delivery", method="recipient_readback",
                    expected=None, observed=None, verified_at=None):
    expected = expected or sha256(b"expected-message").hexdigest()
    observed = observed or expected
    return {
        "kind": kind,
        "value": value,
        "verification": {
            "provider": "kakao",
            "operation": operation,
            "target": "recipient-fixture",
            "remote_id": value,
            "status": "verified",
            "method": method,
            "verified_at": verified_at or datetime.now(timezone.utc).isoformat(),
            "expected_sha256": expected,
            "observed_sha256": observed,
        },
    }


@pytest.mark.parametrize("raw", [
    "[]", "null", "7", '"text"', "{", "{}", '{"results":null}',
    '{"results":[]}', '{"results":1}', '{"version":999,"results":{}}',
    '{"version":true,"results":{}}', '{"results":{},"results":{}}',
    '{"results":{"kakao_delivery":{"status":"blocked","status":"passed"}}}',
])
def test_malformed_ledger_never_crashes_or_is_overwritten(tmp_path, raw):
    path = tmp_path / "acceptance.json"
    path.write_text(raw, encoding="utf-8")
    runtime = AcceptanceRuntime(str(path))
    snapshot = runtime.snapshot()
    assert snapshot["counts"]["passed"] == 0
    assert snapshot["all_passed"] is False
    assert snapshot["completion_percent"] == 0
    assert snapshot["load_error"]
    with pytest.raises(ValueError, match="원장"):
        runtime.record("kakao_delivery", "blocked", blockers=["계정 로그인 필요"])
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(("field", "value"), [
    ("environment", [1]), ("environment", "not-object"),
    ("evidence", {"kind": "runtime_report"}), ("evidence", [None]),
    ("blockers", "test"), ("blockers", [None]), ("operator", None),
    ("operator", 123), ("notes", ["text"]), ("executed_at", ["today"]),
    ("key", "kakao_delivery"),
])
def test_bad_record_fields_do_not_discard_healthy_siblings(tmp_path, field, value):
    _, path, _ = _record_runtime(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"]["packaged_runtime"][field] = value
    payload["results"]["kakao_delivery"] = {
        "status": "blocked", "blockers": ["로그인이 필요합니다."],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    runtime = AcceptanceRuntime(str(path))
    assert _row(runtime)["status"] == "failed"
    assert _row(runtime, "kakao_delivery")["status"] == "blocked"
    assert runtime.snapshot()["counts"]["passed"] == 0


@pytest.mark.parametrize("executed_at", [
    "", "not-a-time", "2026-01-02T03:04:05", "9999-12-31T23:59:59+00:00",
])
def test_loaded_pass_requires_valid_nonfuture_timezone_timestamp(tmp_path, executed_at):
    _, path, _ = _record_runtime(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"]["packaged_runtime"]["executed_at"] = executed_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    row = _row(AcceptanceRuntime(str(path)))
    assert row["status"] == "failed"
    assert "시각" in row["reason"]


def test_loaded_live_pass_rechecks_operator_and_required_evidence(tmp_path):
    path = tmp_path / "acceptance.json"
    payload = {"results": {"kakao_delivery": {
        "status": "passed", "executed_at": datetime.now(timezone.utc).isoformat(),
        "operator": "", "evidence": [{"kind": "remote_receipt", "value": "test-fixture"}],
    }}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert "사용자" in _row(AcceptanceRuntime(str(path)), "kakao_delivery")["reason"]
    payload["results"]["kakao_delivery"].update(operator="fixture-tester", evidence=[])
    path.write_text(json.dumps(payload), encoding="utf-8")
    row = _row(AcceptanceRuntime(str(path)), "kakao_delivery")
    assert row["status"] == "failed"
    assert "증거" in row["reason"]


@pytest.mark.parametrize("mutation", ["delete", "truncate", "same_size_rewrite"])
def test_evidence_deletion_or_change_invalidates_in_memory_and_reloaded_pass(tmp_path, mutation):
    runtime, path, report = _record_runtime(tmp_path)
    evidence = _row(runtime)["evidence"][0]
    assert evidence["fingerprint"]["sha256"] == sha256(report.read_bytes()).hexdigest()
    assert evidence["fingerprint"]["size_bytes"] == report.stat().st_size
    if mutation == "delete":
        report.unlink()
    elif mutation == "truncate":
        report.write_bytes(b"")
    else:
        previous = report.stat()
        report.write_bytes(b"x" * previous.st_size)
        # A content hash, not just size/mtime, protects the approved evidence.
        os.utime(report, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    for candidate in (runtime, AcceptanceRuntime(str(path))):
        assert _row(candidate)["status"] == "failed"
        assert candidate.snapshot()["counts"]["passed"] == 0
        assert candidate.snapshot()["all_passed"] is False


def test_legacy_unsealed_local_evidence_requires_fresh_attestation(tmp_path):
    runtime, path, report = _record_runtime(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 1
    payload["results"]["packaged_runtime"]["evidence"][0].pop("fingerprint")
    path.write_text(json.dumps(payload), encoding="utf-8")
    restored = AcceptanceRuntime(str(path))
    assert _row(restored)["status"] == "failed"
    assert "다시 수락 확인" in _row(restored)["reason"]
    assert "fingerprint" not in path.read_text(encoding="utf-8")
    restored.record("packaged_runtime", "passed", evidence=[
        {"kind": "runtime_report", "value": str(report)},
    ])
    assert _row(restored)["status"] == "passed"


@pytest.mark.parametrize("value", [None, False, 123, [], {}, "", "  "])
def test_remote_evidence_requires_a_real_nonempty_string(tmp_path, value):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    with pytest.raises(ValueError, match="kind와 value"):
        runtime.record("kakao_delivery", "passed", operator="fixture-tester", evidence=[
            {"kind": "remote_receipt", "value": value},
        ])
    assert runtime.snapshot()["counts"]["passed"] == 0


def test_opaque_remote_success_string_cannot_count_as_acceptance(tmp_path):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    with pytest.raises(ValueError, match="구조화된 verification"):
        runtime.record("kakao_delivery", "passed", operator="fixture-tester", evidence=[
            {"kind": "remote_receipt", "value": "anything"},
        ])
    assert runtime.snapshot()["completion_percent"] == 0


@pytest.mark.parametrize(("mutation", "message"), [
    ({"operation": "mail_delivery"}, "작업 종류"),
    ({"remote_id": "different"}, "증거 ID"),
    ({"status": "sent"}, "verified 상태"),
    ({"method": "claimed_by_agent"}, "확인 방법"),
    ({"expected_sha256": "0" * 64}, "payload"),
    ({"verified_at": "2099-01-01T00:00:00+00:00"}, "확인 시각"),
])
def test_remote_receipt_must_bind_expected_payload_to_attributable_readback(
        tmp_path, mutation, message):
    receipt = _remote_receipt()
    receipt["verification"].update(mutation)
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    with pytest.raises(ValueError, match=message):
        runtime.record("kakao_delivery", "passed", operator="fixture-tester",
                       evidence=[receipt])
    assert runtime.snapshot()["counts"]["passed"] == 0


def test_structured_remote_receipt_is_revalidated_after_persistence(tmp_path):
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    result = runtime.record("kakao_delivery", "passed", operator="fixture-tester",
                            evidence=[_remote_receipt()])
    assert result.status == "passed"
    assert _row(AcceptanceRuntime(str(path)), "kakao_delivery")["status"] == "passed"

    payload = json.loads(path.read_text(encoding="utf-8"))
    verification = payload["results"]["kakao_delivery"]["evidence"][0]["verification"]
    verification["observed_sha256"] = "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    restored = AcceptanceRuntime(str(path))
    assert _row(restored, "kakao_delivery")["status"] == "failed"
    assert restored.snapshot()["completion_percent"] == 0


def _freeze_acceptance_clock(monkeypatch, value):
    clock = {"now": value}

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = clock["now"]
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr("core.acceptance_runtime.datetime", FrozenDateTime)
    return clock


def test_expired_remote_receipt_cannot_replace_existing_acceptance(tmp_path, monkeypatch):
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    _freeze_acceptance_clock(monkeypatch, now)
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    runtime.record("kakao_delivery", "blocked", blockers=["실제 수신 확인 필요"])
    before = path.read_bytes()
    receipt = _remote_receipt(verified_at=(now - timedelta(days=15)).isoformat())
    with pytest.raises(ValueError, match="재검증 기한"):
        runtime.record("kakao_delivery", "passed", operator="fixture-tester", evidence=[receipt])
    assert path.read_bytes() == before
    assert _row(runtime, "kakao_delivery")["status"] == "blocked"
    assert _row(AcceptanceRuntime(str(path)), "kakao_delivery")["status"] == "blocked"


def test_rerecording_recent_receipt_preserves_original_expiry(tmp_path, monkeypatch):
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    clock = _freeze_acceptance_clock(monkeypatch, now)
    verified = now - timedelta(days=13)
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    receipt = _remote_receipt(verified_at=verified.isoformat())
    runtime.record("kakao_delivery", "passed", operator="fixture-tester", evidence=[receipt])
    clock["now"] += timedelta(hours=12)
    second = runtime.record("kakao_delivery", "passed", operator="fixture-tester", evidence=[receipt])
    assert datetime.fromisoformat(second.executed_at) == clock["now"]
    saved = path.read_bytes()
    for candidate in (runtime, AcceptanceRuntime(str(path))):
        assert _row(candidate, "kakao_delivery")["status"] == "passed"
        boundary = verified + timedelta(days=14)
        assert _row(candidate, "kakao_delivery", now=boundary)["status"] == "passed"
        snapshot = candidate.snapshot(now=boundary + timedelta(microseconds=1))
        row = next(item for item in snapshot["scenarios"] if item["key"] == "kakao_delivery")
        assert row["status"] == "expired"
        assert row["evidence"][0]["verification"]["verified_at"] == verified.isoformat()
        assert snapshot["counts"]["passed"] == 0
    # Expiry is a derived view; retained historical evidence is not rewritten.
    assert path.read_bytes() == saved


@pytest.mark.parametrize("reverse", [False, True])
def test_remote_expiry_uses_oldest_required_receipt(tmp_path, monkeypatch, reverse):
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    _freeze_acceptance_clock(monkeypatch, now)
    evidence = [
        _remote_receipt(kind="remote_id", operation="mail_delivery", value="mail-1",
                        verified_at=(now - timedelta(days=13)).isoformat()),
        _remote_receipt(operation="mail_delivery", value="delivery-1", verified_at=now.isoformat()),
    ]
    if reverse:
        evidence.reverse()
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    runtime.record("mail_delivery", "passed", operator="fixture-tester", evidence=evidence)
    for candidate in (runtime, AcceptanceRuntime(str(path))):
        assert _row(candidate, "mail_delivery", now=now + timedelta(days=2))["status"] == "expired"


def test_remote_expiry_uses_scenario_window_and_timezone_boundary(tmp_path, monkeypatch):
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
    clock = _freeze_acceptance_clock(monkeypatch, now)
    scenarios = [AcceptanceScenario("oauth_roundtrip", "fixture", "external", True, ("remote_id",), 3)]
    verified = (now - timedelta(days=3)).astimezone(timezone(timedelta(hours=9)))
    receipt = _remote_receipt(kind="remote_id", operation="oauth_roundtrip", method="oauth_roundtrip",
                              verified_at=verified.isoformat())
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path), scenarios)
    runtime.record("oauth_roundtrip", "passed", operator="fixture-tester", evidence=[receipt])
    assert runtime.snapshot()["all_passed"] is True
    before = path.read_bytes()
    clock["now"] += timedelta(microseconds=1)
    with pytest.raises(ValueError, match="재검증 기한"):
        runtime.record("oauth_roundtrip", "passed", operator="fixture-tester", evidence=[receipt])
    restored = AcceptanceRuntime(str(path), scenarios)
    assert _row(restored, "oauth_roundtrip")["status"] == "expired"
    assert restored.snapshot()["all_passed"] is False
    assert path.read_bytes() == before


def test_extra_evidence_cannot_omit_its_kind(tmp_path):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    with pytest.raises(ValueError, match="kind와 value"):
        runtime.record("kakao_delivery", "passed", operator="fixture-tester", evidence=[
            {"kind": "remote_receipt", "value": "test-fixture"}, {"value": "untyped"},
        ])


@pytest.mark.parametrize("previous_status", [None, "blocked", "passed"])
def test_atomic_write_failure_preserves_memory_disk_and_removes_temporary_file(
        tmp_path, monkeypatch, previous_status):
    path = tmp_path / "acceptance.json"
    report = tmp_path / "runtime.json"
    report.write_text("{}", encoding="utf-8")
    runtime = AcceptanceRuntime(str(path))
    if previous_status == "blocked":
        runtime.record("packaged_runtime", "blocked", blockers=["검증 환경 없음"])
    elif previous_status == "passed":
        runtime.record("packaged_runtime", "passed", evidence=[
            {"kind": "runtime_report", "value": str(report)},
        ])
    original_disk = path.read_bytes() if path.exists() else None
    original_row = _row(runtime)

    def fail_replace(*args):
        raise PermissionError("fixture atomic replacement failure")

    monkeypatch.setattr("core.acceptance_runtime.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        runtime.record("packaged_runtime", "failed", notes="fixture attempt")
    assert _row(runtime) == original_row
    assert (path.read_bytes() if path.exists() else None) == original_disk
    assert _row(AcceptanceRuntime(str(path))) == original_row
    assert list(tmp_path.glob("acceptance.json*.tmp")) == []


def test_returned_results_and_caller_objects_cannot_mutate_accepted_state(tmp_path):
    report = tmp_path / "runtime.json"
    report.write_text("{}", encoding="utf-8")
    evidence = [{"kind": "runtime_report", "value": str(report)}]
    environment = {"nested": {"host": "fixture"}}
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    result = runtime.record("packaged_runtime", "passed", evidence=evidence, environment=environment)
    evidence[0]["value"] = "changed"
    environment["nested"]["host"] = "changed"
    result.evidence.clear()
    result.environment["nested"]["host"] = "changed-again"
    result.status = "failed"
    row = _row(runtime)
    assert row["status"] == "passed"
    assert row["environment"]["nested"]["host"] == "fixture"
    assert row["evidence"][0]["value"] == str(report.resolve())
    row["evidence"].clear()
    assert _row(runtime)["status"] == "passed"


def test_relative_evidence_paths_are_stable_across_working_directory_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "runtime.json"
    report.write_text("{}", encoding="utf-8")
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    result = runtime.record("packaged_runtime", "passed", evidence=[
        {"kind": "runtime_report", "value": "runtime.json"},
    ])
    assert result.evidence[0]["value"] == str(report.resolve())
    monkeypatch.chdir(tmp_path.parent)
    assert _row(runtime)["status"] == "passed"
    assert _row(AcceptanceRuntime(str(path)))["status"] == "passed"


def test_in_memory_and_persisted_records_are_equally_redacted(tmp_path):
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    result = runtime.record("kakao_delivery", "blocked", blockers=["로그인 필요"],
                            environment={"api_key": "private-value"},
                            notes="Bearer do-not-retain")
    assert "private-value" not in str(result)
    assert "do-not-retain" not in str(result)
    assert _row(runtime, "kakao_delivery") == _row(AcceptanceRuntime(str(path)), "kakao_delivery")


@pytest.mark.parametrize("bad_value", [{1, 2}, float("nan"), float("inf")])
def test_non_json_data_never_publishes_partial_state(tmp_path, bad_value):
    path = tmp_path / "acceptance.json"
    runtime = AcceptanceRuntime(str(path))
    with pytest.raises(ValueError, match="JSON"):
        runtime.record("packaged_runtime", "blocked", blockers=["검증 필요"],
                       environment={"bad": bad_value})
    assert _row(runtime)["status"] == "not_run"
    assert not path.exists()


def test_pass_cannot_keep_unresolved_blockers_and_loaded_blocked_requires_reason(tmp_path):
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"))
    with pytest.raises(ValueError, match="차단 사유"):
        runtime.record("kakao_delivery", "passed", operator="fixture-tester", blockers=["미수신"],
                       evidence=[{"kind": "remote_receipt", "value": "test-fixture"}])
    path = tmp_path / "loaded.json"
    path.write_text(json.dumps({"results": {"kakao_delivery": {"status": "blocked"}}}), encoding="utf-8")
    assert _row(AcceptanceRuntime(str(path)), "kakao_delivery")["status"] == "failed"


def test_one_valid_scenario_completes_only_its_explicit_gate(tmp_path):
    report = tmp_path / "runtime.json"
    report.write_text("{}", encoding="utf-8")
    scenario = AcceptanceScenario("runtime", "fixture", "operations", False, ("runtime_report",))
    runtime = AcceptanceRuntime(str(tmp_path / "acceptance.json"), scenarios=[scenario])
    runtime.record("runtime", "passed", evidence=[{"kind": "runtime_report", "value": str(report)}])
    snapshot = runtime.snapshot()
    assert snapshot["all_passed"] is True
    assert snapshot["completion_percent"] == 100
    assert snapshot["completion_scope"] == "acceptance_scenarios"
    assert snapshot["total_scenarios"] == 1
    assert AcceptanceRuntime(str(tmp_path / "empty.json"), scenarios=[]).snapshot()["all_passed"] is False


def test_naive_snapshot_clock_is_handled_without_datetime_type_error(tmp_path):
    runtime, _, _ = _record_runtime(tmp_path)
    snapshot = runtime.snapshot(now=(datetime.now(timezone.utc) + timedelta(days=31)).replace(tzinfo=None))
    assert next(row for row in snapshot["scenarios"] if row["key"] == "packaged_runtime")["status"] == "expired"


@pytest.mark.parametrize("fingerprint", [
    None, "abc", {}, {"sha256": "0" * 64, "size_bytes": 20},
    {"sha256": ["bad"], "size_bytes": True},
])
def test_loaded_malformed_fingerprints_never_count_as_passed(tmp_path, fingerprint):
    _, path, _ = _record_runtime(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["results"]["packaged_runtime"]["evidence"][0]["fingerprint"] = fingerprint
    path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = AcceptanceRuntime(str(path)).snapshot()
    assert snapshot["counts"]["passed"] == 0


def test_relative_ledger_path_is_fixed_at_runtime_creation(tmp_path, monkeypatch):
    original = tmp_path / "original"
    other = tmp_path / "other"
    original.mkdir()
    other.mkdir()
    monkeypatch.chdir(original)
    runtime = AcceptanceRuntime("acceptance.json")
    monkeypatch.chdir(other)
    runtime.record("kakao_delivery", "blocked", blockers=["로그인 필요"])
    assert (original / "acceptance.json").exists()
    assert not (other / "acceptance.json").exists()


def test_hex_fingerprints_are_not_mistaken_for_personal_phone_numbers(tmp_path, monkeypatch):
    digest = "f" * 20 + "01012345678" + "f" * 33
    monkeypatch.setattr(AcceptanceRuntime, "_file_fingerprint", staticmethod(
        lambda target: {"sha256": digest, "size_bytes": target.stat().st_size}))
    runtime, path, _ = _record_runtime(tmp_path)
    assert _row(runtime)["evidence"][0]["fingerprint"]["sha256"] == digest
    assert _row(AcceptanceRuntime(str(path)))["status"] == "passed"
