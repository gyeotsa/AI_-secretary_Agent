import json
import threading
from types import SimpleNamespace
import pytest

import core.scheduler as scheduler_module
from core.executor import Executor
from core.scratchpad import Task
from core.tool_result import Evidence
from core.tool_result import ToolRunResult, ToolRunStatus
from core.tools import ToolExecutor
from core.verifier import ToolVerifier, VerificationResult


def test_verified_file_result_contains_evidence_and_artifact():
    raw = json.dumps({
        "status": "created",
        "type": "file",
        "path": r"C:\workspace\report.txt",
    })
    result = ToolRunResult.from_verification(
        tool_name="filesystem_create_file",
        raw_output=raw,
        verification=VerificationResult(
            True,
            "실제 파일 확인",
            {"method": "path_exists"},
        ),
        duration_ms=12.5,
    )
    assert result.status == ToolRunStatus.SUCCEEDED
    assert result.succeeded
    assert result.evidence[0].data["method"] == "path_exists"
    assert result.artifacts[0].uri == r"C:\workspace\report.txt"
    assert result.duration_ms == 12.5


def test_failed_verification_cannot_claim_success():
    result = ToolRunResult.from_verification(
        tool_name="filesystem_write_file",
        raw_output="오류: 저장 실패",
        verification=VerificationResult(False, "파일이 변경되지 않았습니다."),
    )
    assert result.status == ToolRunStatus.FAILED
    assert not result.succeeded
    assert result.error == "파일이 변경되지 않았습니다."


def test_web_search_result_exposes_source_artifacts():
    raw = json.dumps({
        "query": "latest",
        "results": [
            {"title": "Official", "url": "https://example.com/product"},
            {"title": "News", "url": "https://example.org/news"},
        ],
    })
    result = ToolRunResult.from_verification(
        tool_name="browser_web_search",
        raw_output=raw,
        verification=VerificationResult(True, "출처 2건 확인"),
    )
    assert [artifact.kind for artifact in result.artifacts] == ["url", "url"]
    assert result.to_dict()["status"] == "succeeded"


def test_unknown_tool_is_unverified_instead_of_success():
    verification = ToolVerifier().verify(
        "plugin_without_verifier",
        {},
        "작업을 완료했습니다.",
    )
    result = ToolRunResult.from_verification(
        tool_name="plugin_without_verifier",
        raw_output="작업을 완료했습니다.",
        verification=verification,
    )
    assert not verification.verified
    assert not verification.success
    assert result.status == ToolRunStatus.UNVERIFIED
    assert not result.succeeded
    assert result.error is None


def test_successful_result_requires_evidence():
    with pytest.raises(ValueError, match="검증 증거"):
        ToolRunResult.successful(
            tool_name="filesystem_create_file",
            raw_output="{}",
            evidence=[],
        )


def test_executor_preserves_direct_typed_result_and_rejects_name_mismatch():
    executor = Executor.__new__(Executor)
    direct = ToolRunResult.successful(
        tool_name="filesystem_create_file",
        raw_output='{"status":"created"}',
        evidence=[Evidence("filesystem_state", "파일 확인")],
    )
    preserved = executor.build_tool_run_result(
        Task("task-1", "파일 생성"),
        "filesystem_create_file",
        {},
        direct,
        9.5,
    )
    assert preserved is direct
    assert preserved.duration_ms == 9.5

    mismatch = executor.build_tool_run_result(
        Task("task-2", "파일 생성"),
        "filesystem_write_file",
        {},
        direct,
    )
    assert mismatch.status == ToolRunStatus.FAILED
    assert "일치하지 않습니다" in mismatch.error


def test_executor_downgrades_direct_success_without_evidence():
    executor = Executor.__new__(Executor)
    unsupported = ToolRunResult(
        tool_name="unsafe_plugin",
        status=ToolRunStatus.SUCCEEDED,
        raw_output="완료",
    )
    result = executor.build_tool_run_result(
        Task("task-3", "미검증 작업"),
        "unsafe_plugin",
        {},
        unsupported,
    )
    assert result.status == ToolRunStatus.UNVERIFIED
    assert not result.succeeded


def test_legacy_file_mutations_return_verified_typed_results(tmp_path):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._resolve_and_validate_path = lambda path: (True, "", str(path))

    target = tmp_path / "nested" / "note.txt"
    written = executor.write_file(str(target), "안녕")
    assert written.status == ToolRunStatus.SUCCEEDED
    assert written.evidence[0].kind == "file_content"
    assert target.read_text(encoding="utf-8") == "안녕"

    directory = tmp_path / "folder"
    created = executor.create_directory(str(directory))
    assert created.status == ToolRunStatus.SUCCEEDED
    assert directory.is_dir()

    deleted = executor.delete_directory(str(directory))
    assert deleted.status == ToolRunStatus.SUCCEEDED
    assert deleted.evidence[0].kind == "directory_absent"
    assert not directory.exists()


def test_run_command_uses_exit_code_as_success_boundary(monkeypatch):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.safety = SimpleNamespace(
        validate_command=lambda command: (True, "", ["git", "--version"])
    )

    monkeypatch.setattr(
        "core.tools.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="git version 1.0\n", stderr="", returncode=0
        ),
    )
    succeeded = executor.run_command("git --version")
    assert succeeded.status == ToolRunStatus.SUCCEEDED
    assert succeeded.evidence[0].data["returncode"] == 0

    monkeypatch.setattr(
        "core.tools.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="", stderr="fatal", returncode=2
        ),
    )
    failed = executor.run_command("git --version")
    assert failed.status == ToolRunStatus.FAILED
    assert failed.evidence[0].data["returncode"] == 2


def test_device_queries_and_camera_capture_return_direct_evidence(tmp_path):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._hardware_manager = SimpleNamespace(
        list_input_devices=lambda: [{
            "index": 3,
            "name": "Test microphone",
            "default_samplerate": 48000.0,
            "max_input_channels": 1,
        }]
    )
    image_path = tmp_path / "capture.jpg"
    image_path.write_bytes(b"jpeg-bytes")
    executor._multimodal_manager = SimpleNamespace(
        list_camera_devices=lambda: [{
            "index": 1, "backend": "TEST", "width": 640, "height": 480,
        }],
        capture_camera_frame_details=lambda save_path: {
            "path": str(image_path),
            "camera_index": 1,
            "backend": "TEST",
            "width": 640,
            "height": 480,
            "size": image_path.stat().st_size,
        },
    )

    microphones = executor.list_audio_input_devices()
    cameras = executor.list_camera_devices()
    capture = executor.capture_camera(str(image_path))

    assert microphones.status == ToolRunStatus.SUCCEEDED
    assert microphones.evidence[0].data["count"] == 1
    assert cameras.status == ToolRunStatus.SUCCEEDED
    assert cameras.evidence[0].data["count"] == 1
    assert capture.status == ToolRunStatus.SUCCEEDED
    assert capture.artifacts[0].uri == str(image_path)


def test_automation_tools_verify_database_and_engine_state(monkeypatch, tmp_path):
    engine = scheduler_module.AutomationEngine.__new__(scheduler_module.AutomationEngine)
    engine.data_dir = str(tmp_path)
    engine.scheduler_db_path = str(tmp_path / "scheduler.db")
    engine.scheduled_jobs = []
    engine.scheduler = scheduler_module.schedule.Scheduler()
    engine.running = False
    engine.scheduler_thread = None
    engine.stop_event = threading.Event()
    engine.job_results = {}
    engine.result_callback = None
    engine._init_db()
    monkeypatch.setattr(scheduler_module, "get_automation_engine", lambda: engine)
    executor = ToolExecutor.__new__(ToolExecutor)

    added = executor.add_automation_job("테스트", "every_minutes", "5", "상태 확인")
    assert added.status == ToolRunStatus.SUCCEEDED
    job_id = added.evidence[0].data["id"]

    listed = executor.list_automation_jobs()
    assert listed.status == ToolRunStatus.SUCCEEDED
    assert listed.evidence[0].data["job_ids"] == [job_id]

    toggled = executor.toggle_automation_job(job_id, False)
    assert toggled.status == ToolRunStatus.SUCCEEDED
    assert toggled.evidence[0].data["enabled"] is False

    missing = executor.toggle_automation_job(9999, True)
    assert missing.status == ToolRunStatus.FAILED

    history = executor.get_job_history(job_id)
    assert history.status == ToolRunStatus.SUCCEEDED
    assert history.evidence[0].data["count"] == 0

    started = executor.start_automation_engine()
    assert started.status == ToolRunStatus.SUCCEEDED
    stopped = executor.stop_automation_engine()
    assert stopped.status == ToolRunStatus.SUCCEEDED

    deleted = executor.delete_automation_job(job_id)
    assert deleted.status == ToolRunStatus.SUCCEEDED
    assert engine.get_job_record(job_id) is None
