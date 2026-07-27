import json
import pytest

from core.executor import Executor
from core.scratchpad import Task
from core.tool_result import Evidence
from core.tool_result import ToolRunResult, ToolRunStatus
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
