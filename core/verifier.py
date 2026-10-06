
import hashlib
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Iterable, Mapping
from dataclasses import dataclass

import pygetwindow

from core.tool_result import ToolRunResult, ToolRunStatus


@dataclass
class VerificationResult:
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None
    verified: bool = True


class ToolVerifier:
    """
    도구 실행 결과를 검증하는 클래스!
    각 도구별로 맞춤형 검증 로직을 제공합니다.
    """

    def __init__(self):
        self._verifiers: Dict[str, Callable] = {
            "write_file": self._verify_write_file,
            "read_file": self._verify_read_file,
            "create_directory": self._verify_create_directory,
            "filesystem_create_project": self._verify_created_path_result,
            "filesystem_create_file": self._verify_created_path_result,
            "filesystem_write_file": self._verify_created_path_result,
            "delete_directory": self._verify_delete_directory,
            "create_excel_file": self._verify_create_excel_file,
            "write_excel_cell": self._verify_write_excel_cell,
            "add_document": self._verify_add_document,
            "run_command": self._verify_run_command,
            "calendar_create_event": self._verify_calendar_create_event,
            "naver_calendar_read_view": self._verify_naver_calendar_view,
            "excel_create_workbook": self._verify_create_excel_file,
            "excel_write_cells": self._verify_write_excel_cells,
            "word_create_document": self._verify_word_document,
            "powerpoint_create_presentation": self._verify_powerpoint_presentation,
            "pdf_create_document": self._verify_pdf_document,
            "hwpx_create_document": self._verify_hwpx_document,
            "browser_web_search": self._verify_web_search,
            "windows_launch_app": self._verify_windows_launch_app,
            "windows_close_app": self._verify_windows_close_app,
            "windows_add_app_aliases": self._verify_windows_add_app_aliases,
        }

    @staticmethod
    def _raw_output(result: Any) -> str:
        if isinstance(result, ToolRunResult):
            return result.raw_output
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, default=str)
        return str(result)

    def _verify_naver_calendar_view(self, tool_input, result):
        from core.naver_calendar import NaverCalendarService
        payload = self._payload(result)
        evidence = self._evidence_data(result, "calendar_visible_view")
        try:
            NaverCalendarService.validate_view(payload)
        except (ValueError, TypeError):
            return VerificationResult(False, "네이버 캘린더 조회 범위/계정 표시 증거가 유효하지 않습니다.")
        if (evidence.get("scope") != "visible_view" or evidence.get("complete_account") is not False
                or evidence.get("payload_sha256") != hashlib.sha256(self._raw_output(result).encode()).hexdigest()
                or evidence.get("account_fingerprint") != hashlib.sha256(payload["account"].encode()).hexdigest()):
            return VerificationResult(False, "캘린더 조회 본문과 실행 증거가 일치하지 않습니다.")
        return VerificationResult(True, "현재 캘린더 표시 화면만 읽었음을 확인했습니다. 전체 일정 동기화는 아닙니다.",
                                  {"scope":"visible_view", "complete_account":False})

    @staticmethod
    def _payload(result: Any) -> Dict[str, Any]:
        if isinstance(result, Mapping):
            return dict(result)
        text = ToolVerifier._raw_output(result)
        try:
            payload = json.loads(text)
            return dict(payload) if isinstance(payload, Mapping) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}

    @staticmethod
    def _evidence(result: Any) -> list[Dict[str, Any]]:
        if not isinstance(result, ToolRunResult):
            return []
        records = []
        for item in result.evidence:
            records.append({
                "kind": str(getattr(item, "kind", "")),
                "summary": str(getattr(item, "summary", "")),
                "data": dict(getattr(item, "data", {}) or {}),
            })
        return records

    @classmethod
    def _evidence_data(cls, result: Any, *kinds: str) -> Dict[str, Any]:
        expected = {kind.casefold() for kind in kinds}
        merged: Dict[str, Any] = {}
        for item in cls._evidence(result):
            if not expected or item["kind"].casefold() in expected:
                merged.update(item["data"])
        return merged

    @staticmethod
    def _contract(tool_input: Mapping[str, Any]) -> Dict[str, Any]:
        """Return optional caller-captured before/after and postcondition evidence."""
        contract: Dict[str, Any] = {}
        for key in ("_verification", "verification"):
            value = tool_input.get(key)
            if isinstance(value, Mapping):
                contract.update(value)
        for key in (
            "before_sha256", "after_sha256", "expected_sha256",
            "before_mtime_ns", "after_mtime_ns", "before_exists",
            "expected_exit_code", "expected_stdout_contains", "expected_output_regex",
            "expected_paths_exist", "expected_paths_absent",
            "expected_file_sha256", "expected_content_contains",
        ):
            if key in tool_input:
                contract[key] = tool_input[key]
        return contract

    @staticmethod
    def _file_snapshot(path: Path) -> Dict[str, Any]:
        stat = path.stat()
        return {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    @classmethod
    def _path_from(cls, tool_input: Mapping[str, Any], result: Any) -> Optional[Path]:
        value = tool_input.get("path") or tool_input.get("file_path")
        if not value:
            value = cls._payload(result).get("path")
        if not value and isinstance(result, ToolRunResult):
            for artifact in result.artifacts:
                if str(getattr(artifact, "kind", "")).casefold() in {
                    "file", "document", "spreadsheet", "presentation", "calendar", "pdf"
                }:
                    value = getattr(artifact, "uri", "")
                    if value:
                        break
        return Path(str(value)).expanduser().resolve() if value else None

    @classmethod
    def _validate_file_snapshot(
        cls,
        path: Path,
        tool_input: Mapping[str, Any],
        result: Any,
        *,
        require_changed: bool = False,
    ) -> tuple[Optional[VerificationResult], Dict[str, Any]]:
        if not path.is_file():
            return VerificationResult(False, f"결과 파일이 생성되지 않았습니다: {path}"), {}
        snapshot = cls._file_snapshot(path)
        payload = cls._payload(result)
        evidence = cls._evidence_data(result)
        contract = cls._contract(tool_input)

        claimed_path = payload.get("path") or evidence.get("path")
        if claimed_path:
            try:
                if Path(str(claimed_path)).expanduser().resolve() != path:
                    return VerificationResult(
                        False,
                        "도구가 주장한 산출물 경로와 실제 검증 대상이 다릅니다.",
                        {"claimed_path": str(claimed_path), "verified_path": str(path)},
                    ), snapshot
            except (OSError, ValueError):
                return VerificationResult(False, "도구 결과의 산출물 경로가 유효하지 않습니다."), snapshot

        expected_after = (
            contract.get("after_sha256") or contract.get("expected_sha256")
            or payload.get("after_sha256") or payload.get("sha256")
            or evidence.get("after_sha256") or evidence.get("file_sha256")
            or evidence.get("sha256")
        )
        if expected_after and str(expected_after).casefold() != snapshot["sha256"].casefold():
            return VerificationResult(
                False,
                "저장 결과 해시가 실행 증거와 일치하지 않습니다.",
                {"expected_sha256": str(expected_after), "actual_sha256": snapshot["sha256"]},
            ), snapshot

        before_hash = contract.get("before_sha256") or payload.get("before_sha256") or evidence.get("before_sha256")
        if require_changed and before_hash and str(before_hash).casefold() == snapshot["sha256"].casefold():
            return VerificationResult(False, "파일 내용 해시가 실행 전과 같아 실제 변경이 없습니다."), snapshot

        before_mtime = contract.get("before_mtime_ns")
        if require_changed and before_mtime is not None:
            try:
                if snapshot["mtime_ns"] <= int(before_mtime):
                    return VerificationResult(False, "파일 수정 시각이 실행 전보다 갱신되지 않았습니다."), snapshot
            except (TypeError, ValueError):
                return VerificationResult(False, "실행 전 수정 시각 증거가 유효하지 않습니다."), snapshot
        return None, snapshot

    def _verify_windows_launch_app(
        self, tool_input: Dict[str, Any], result: Any
    ) -> VerificationResult:
        """PID가 있는 직접 실행만 실제 프로세스 존재 여부로 검증한다."""
        evidence = self._evidence_data(result, "process_state", "process_execution")
        match = re.search(r"\bPID:\s*(\d+)", self._raw_output(result))
        evidence_pid = evidence.get("pid")
        pid = int(evidence_pid) if str(evidence_pid or "").isdigit() else (
            int(match.group(1)) if match else 0
        )
        if not pid:
            return VerificationResult(
                False,
                "실행 요청은 반환됐지만 확인 가능한 프로세스 ID가 없습니다.",
                {"result": self._raw_output(result)[:200]},
                verified=False,
            )
        try:
            os.kill(pid, 0)
        except OSError as exc:
            return VerificationResult(
                False,
                f"실행된 프로세스를 확인하지 못했습니다: PID {pid}",
                {"pid": pid, "error": str(exc)},
            )
        return VerificationResult(
            True,
            f"실행된 프로세스를 확인했습니다: PID {pid}",
            {"pid": pid, "method": "process_exists"},
        )

    def _verify_windows_close_app(
        self, tool_input: Dict[str, Any], result: Any
    ) -> VerificationResult:
        """종료를 요청한 창이 현재 창 목록에서 사라졌는지 확인한다."""
        prefix = "앱 종료 요청 성공:"
        raw = self._raw_output(result)
        title = raw.split(prefix, 1)[1].strip() if prefix in raw else ""
        if not title:
            return VerificationResult(
                False,
                "종료 결과에서 대상 창 제목을 확인할 수 없습니다.",
                verified=False,
            )
        remaining = [
            window.title for window in pygetwindow.getAllWindows()
            if window.title and window.title.casefold() == title.casefold()
        ]
        if remaining:
            return VerificationResult(
                False,
                f"종료 요청 후에도 창이 남아 있습니다: {title}",
                {"window_title": title},
            )
        return VerificationResult(
            True,
            f"대상 창이 닫힌 것을 확인했습니다: {title}",
            {"window_title": title, "method": "window_absent"},
        )

    def _verify_windows_add_app_aliases(
        self, tool_input: Dict[str, Any], result: Any
    ) -> VerificationResult:
        """요청한 별칭이 사용자 별칭 저장소에 실제 보존됐는지 확인한다."""
        aliases = [
            str(item).strip().casefold()
            for item in tool_input.get("aliases") or []
            if str(item).strip()
        ]
        alias_path = Path(__file__).resolve().parent.parent / "data" / "user_app_aliases.json"
        try:
            saved = json.loads(alias_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return VerificationResult(
                False,
                f"사용자 별칭 저장소를 확인하지 못했습니다: {exc}",
            )
        missing = [alias for alias in aliases if not str(saved.get(alias, "")).strip()]
        if missing:
            return VerificationResult(
                False,
                f"저장되지 않은 앱 별칭이 있습니다: {', '.join(missing)}",
                {"missing_aliases": missing},
            )
        return VerificationResult(
            True,
            f"앱 별칭 {len(aliases)}개 저장을 확인했습니다.",
            {"aliases": aliases, "method": "alias_store"},
        )

    def _verify_web_search(
        self, tool_input: Dict[str, Any], result: Any
    ) -> VerificationResult:
        """웹 검색이 실제 URL이 포함된 구조화 결과를 반환했는지 확인한다."""
        try:
            payload = self._payload(result)
            results = payload.get("results", [])
            if not results and isinstance(result, ToolRunResult):
                evidence = self._evidence_data(result, "web_sources")
                urls = evidence.get("urls") or []
                if urls and int(evidence.get("source_count", 0)) == len(urls):
                    results = [{"title": "verified source", "url": url} for url in urls]
            if not results:
                return VerificationResult(False, "웹 검색 결과가 비어 있습니다.")
            valid = [
                item for item in results
                if str(item.get("url", "")).startswith(("https://", "http://"))
                and item.get("title")
            ]
            if not valid:
                return VerificationResult(False, "출처 URL이 있는 검색 결과가 없습니다.")
            return VerificationResult(
                True, f"실제 웹 검색 결과 {len(valid)}건과 출처 URL을 확인했습니다."
            )
        except (json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
            return VerificationResult(False, f"웹 검색 결과 검증 오류: {exc}")

    def _verify_created_path_result(
        self, tool_input: Dict[str, Any], result: Any
    ) -> VerificationResult:
        """파일시스템 생성 도구의 구조화 결과와 실제 경로를 함께 검증한다."""
        try:
            payload = self._payload(result)
            path = payload.get("path", "")
            expected_type = payload.get("type")
            if payload.get("status") not in {"created", "written"} or not path:
                return VerificationResult(
                    False,
                    "생성·수정 상태와 경로가 담긴 구조화 증거가 없습니다.",
                    {"result": self._raw_output(result)[:200]},
                    verified=False,
                )
            target = Path(str(path)).expanduser().resolve()
            exists = target.is_dir() if expected_type == "directory" else target.is_file()
            if not exists:
                return VerificationResult(False, f"생성 결과 경로가 존재하지 않습니다: {path}")
            details: Dict[str, Any] = {
                "path": str(target), "type": expected_type,
                "method": "filesystem_postcondition",
            }
            if expected_type != "directory":
                failure, snapshot = self._validate_file_snapshot(
                    target, tool_input, result,
                    require_changed=payload.get("status") == "written",
                )
                if failure:
                    return failure
                details.update(snapshot)
                expected_content = tool_input.get("content")
                if expected_content is not None:
                    expected_bytes = str(expected_content).encode("utf-8")
                    if target.read_bytes() != expected_bytes:
                        return VerificationResult(False, "생성된 파일 내용이 요청과 일치하지 않습니다.")
            if payload.get("status") == "written":
                if not payload.get("changed"):
                    return VerificationResult(False, "파일 내용이 실제로 변경되지 않았습니다.")
                before = payload.get("before_sha256")
                after = payload.get("after_sha256")
                if not before or not after or before == after:
                    return VerificationResult(False, "파일 변경 해시 검증에 실패했습니다.")
                if str(after).casefold() != details["sha256"].casefold():
                    return VerificationResult(False, "파일 수정 결과의 실제 해시가 반환 증거와 다릅니다.")
            return VerificationResult(True, f"실제 생성 경로와 사후조건 확인 성공: {path}", details)
        except (json.JSONDecodeError, TypeError, AttributeError, OSError, ValueError) as exc:
            return VerificationResult(False, f"생성 결과 검증 오류: {exc}")

    def verify(self, tool_name: str, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """
        도구 실행 결과를 검증합니다!
        
        Args:
            tool_name: 실행된 도구 이름
            tool_input: 도구에 전달된 입력
            result: 도구 실행 결과 문자열
        
        Returns:
            VerificationResult: 검증 결과
        """
        # Typed result status is authoritative, but even typed success must carry evidence
        # and pass the tool-specific postcondition below.
        if isinstance(result, ToolRunResult):
            if result.tool_name != tool_name:
                return VerificationResult(
                    False,
                    f"Tool 결과 이름이 요청과 다릅니다: {result.tool_name}",
                    {"requested_tool": tool_name, "result_tool": result.tool_name},
                )
            if result.status in {ToolRunStatus.FAILED, ToolRunStatus.CANCELLED}:
                return VerificationResult(
                    False,
                    result.error or result.raw_output or "도구 실행이 실패했습니다.",
                    {"status": result.status.value},
                )
            if result.status in {ToolRunStatus.PARTIAL, ToolRunStatus.UNVERIFIED}:
                return VerificationResult(
                    False,
                    "도구가 부분 성공 또는 미검증 결과를 반환했습니다.",
                    {"status": result.status.value},
                    verified=False,
                )
            if not result.evidence:
                return VerificationResult(
                    False,
                    "구조화 성공 결과에 검증 증거가 없습니다.",
                    {"status": result.status.value},
                    verified=False,
                )

        raw_result = self._raw_output(result)
        payload = self._payload(result)
        payload_status = str(payload.get("status", "")).casefold()
        if payload_status in {"failed", "failure", "error", "cancelled"} or payload.get("error"):
            return VerificationResult(
                False,
                f"구조화 도구 결과가 실패를 보고했습니다: {payload.get('error') or payload_status}",
                {"payload": payload},
            )
        # 구조화된 오류 접두사만 공통 실패로 봅니다. 정상 출력에 포함된 'error' 단어는 허용합니다.
        normalized = raw_result.lstrip().casefold()
        if (normalized.startswith(("오류:", "error:", "툴 파라미터 오류:", "명령어 실행 오류:"))
                or (normalized.startswith("경로 '") and "허용되지 않습니다" in normalized)
                or normalized.startswith(("권한이 거부", "권한 거부"))):
            return VerificationResult(
                success=False,
                message=f"도구 실행 결과에 오류가 포함되어 있습니다: {raw_result[:100]}",
                details={"error": raw_result}
            )

        # 2. 도구별 맞춤 검증
        if tool_name in self._verifiers:
            return self._verifiers[tool_name](tool_input, result)

        # 3. 전용 검증기가 없는 실행은 성공으로 추정하지 않는다.
        return VerificationResult(
            success=False,
            message=f"도구 {tool_name}에는 실행 결과 검증기가 등록되지 않았습니다.",
            details={"result": raw_result[:100], "tool_name": tool_name},
            verified=False,
        )

    @staticmethod
    def _normalized_text(value: Any) -> str:
        """문서 재열기 결과 비교용으로 줄바꿈·연속 공백만 정규화한다."""
        return " ".join(str(value or "").replace("\r", "\n").split())

    @classmethod
    def _requested_document_texts(cls, tool_input: Mapping[str, Any]) -> list[str]:
        requested: list[str] = []
        if tool_input.get("title") is not None:
            requested.append(str(tool_input["title"]))
        requested.extend(str(item) for item in (tool_input.get("paragraphs") or []))
        return [item for item in requested if item != ""]

    def _verify_write_file(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """요청 바이트와 디스크 바이트, 선택적 실행 전/후 증거를 비교한다."""
        try:
            path = self._path_from(tool_input, result)
            if path is None:
                return VerificationResult(False, "파일 경로가 누락되었습니다.")
            if "content" not in tool_input:
                return VerificationResult(False, "검증할 요청 파일 내용이 없습니다.", verified=False)
            failure, snapshot = self._validate_file_snapshot(
                path, tool_input, result, require_changed=True
            )
            if failure:
                return failure
            expected = str(tool_input["content"]).encode("utf-8")
            actual = path.read_bytes()
            if actual != expected:
                return VerificationResult(
                    False,
                    "파일의 실제 바이트가 요청 내용과 일치하지 않습니다.",
                    {"expected_sha256": hashlib.sha256(expected).hexdigest(), **snapshot},
                )
            return VerificationResult(
                True, "파일 내용·크기·해시 사후조건을 확인했습니다.",
                {**snapshot, "method": "byte_exact_postcondition"},
            )
        except (OSError, TypeError, ValueError) as exc:
            return VerificationResult(False, f"파일 검증 중 오류가 발생했습니다: {exc}")

    def _verify_created_file(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """이전 호출자용 보수적 포맷 재열기 디스패처."""
        path = self._path_from(tool_input, result)
        suffix = path.suffix.casefold() if path else ""
        dispatch = {
            ".xlsx": self._verify_create_excel_file,
            ".docx": self._verify_word_document,
            ".pptx": self._verify_powerpoint_presentation,
            ".pdf": self._verify_pdf_document,
            ".hwpx": self._verify_hwpx_document,
            ".ics": self._verify_calendar_create_event,
        }
        verifier = dispatch.get(suffix)
        if verifier is None:
            return VerificationResult(
                False,
                "파일 존재만으로는 생성 성공을 증명할 수 없습니다.",
                {"path": str(path) if path else ""},
                verified=False,
            )
        return verifier(tool_input, result)

    def _verify_read_file(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """반환 텍스트가 검증 시점의 실제 파일 내용과 정확히 같은지 확인한다."""
        try:
            path = self._path_from(tool_input, result)
            if path is None or not path.is_file():
                return VerificationResult(False, f"파일이 존재하지 않습니다: {path}")
            actual = path.read_text(encoding="utf-8")
            returned = self._raw_output(result)
            if returned != actual:
                return VerificationResult(
                    False, "반환된 내용이 검증 시점의 파일 내용과 다릅니다.",
                    {
                        "actual_sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest(),
                        "returned_sha256": hashlib.sha256(returned.encode("utf-8")).hexdigest(),
                    },
                )
            snapshot = self._file_snapshot(path)
            return VerificationResult(
                True, "파일을 다시 읽어 반환 내용과 해시를 확인했습니다.",
                {**snapshot, "method": "readback_exact"},
            )
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            return VerificationResult(False, f"파일 검증 중 오류가 발생했습니다: {exc}")

    def _verify_create_directory(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """디렉터리 존재와 생성 실행 증거를 함께 요구한다."""
        try:
            value = tool_input.get("dir_path") or tool_input.get("path") or self._payload(result).get("path")
            if not value:
                return VerificationResult(False, "디렉터리 경로가 누락되었습니다.")
            path = Path(str(value)).expanduser().resolve()
            if not path.is_dir():
                return VerificationResult(False, f"디렉터리가 생성되지 않았습니다: {path}")
            payload = self._payload(result)
            evidence = self._evidence_data(result, "directory_exists", "filesystem_state")
            contract = self._contract(tool_input)
            proved_creation = (
                evidence.get("is_directory") is True
                or str(payload.get("status", "")).casefold() == "created"
                or contract.get("before_exists") is False
            )
            if not proved_creation:
                return VerificationResult(
                    False, "폴더는 존재하지만 이번 실행에서 생성됐다는 증거가 없습니다.",
                    {"path": str(path)}, verified=False,
                )
            return VerificationResult(
                True, "생성 증거와 실제 디렉터리 상태를 확인했습니다.",
                {"path": str(path), "is_directory": True, "method": "directory_postcondition"},
            )
        except (OSError, TypeError, ValueError) as exc:
            return VerificationResult(False, f"디렉터리 검증 중 오류가 발생했습니다: {exc}")

    def _verify_delete_directory(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """실행 전 존재 또는 구조화 삭제 증거와 실행 후 부재를 함께 확인한다."""
        try:
            value = tool_input.get("dir_path") or tool_input.get("path") or self._payload(result).get("path")
            if not value:
                return VerificationResult(False, "디렉터리 경로가 누락되었습니다.")
            path = Path(str(value)).expanduser().resolve()
            if path.exists():
                return VerificationResult(False, f"디렉터리가 삭제되지 않았습니다: {path}")
            payload = self._payload(result)
            evidence = self._evidence_data(result, "directory_absent")
            contract = self._contract(tool_input)
            proved_deletion = bool(evidence) or str(payload.get("status", "")).casefold() == "deleted" \
                or contract.get("before_exists") is True
            if not proved_deletion:
                return VerificationResult(
                    False, "경로는 없지만 이번 실행에서 삭제됐다는 증거가 없습니다.",
                    {"path": str(path)}, verified=False,
                )
            return VerificationResult(
                True, "삭제 실행 증거와 경로 부재 사후조건을 확인했습니다.",
                {"path": str(path), "exists": False, "method": "absence_postcondition"},
            )
        except (OSError, TypeError, ValueError) as exc:
            return VerificationResult(False, f"디렉터리 검증 중 오류가 발생했습니다: {exc}")

    def _verify_create_excel_file(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """XLSX를 실제로 다시 열고 시트·행·셀 값을 요청과 비교한다."""
        path = self._path_from(tool_input, result)
        if path is None:
            return VerificationResult(False, "엑셀 파일 경로가 누락되었습니다.")
        try:
            from openpyxl import load_workbook
            failure, snapshot = self._validate_file_snapshot(path, tool_input, result)
            if failure:
                return failure
            workbook = load_workbook(path, read_only=True, data_only=False)
            try:
                sheet_name = str(tool_input.get("sheet") or "Sheet1")
                if sheet_name not in workbook.sheetnames:
                    return VerificationResult(False, f"요청한 시트가 없습니다: {sheet_name}")
                sheet = workbook[sheet_name]
                rows = tool_input.get("rows")
                if rows is None:
                    rows = tool_input.get("data")
                for row_index, expected_row in enumerate(rows or [], 1):
                    for column_index, expected in enumerate(list(expected_row), 1):
                        actual = sheet.cell(row=row_index, column=column_index).value
                        if actual != expected:
                            return VerificationResult(
                                False, "다시 연 Excel 셀 값이 요청과 다릅니다.",
                                {"row": row_index, "column": column_index,
                                 "expected": expected, "actual": actual},
                            )
                details = {
                    **snapshot, "sheet": sheet_name,
                    "rows": sheet.max_row, "columns": sheet.max_column,
                    "sheet_names": list(workbook.sheetnames), "method": "openpyxl_reopen",
                }
            finally:
                workbook.close()
            return VerificationResult(True, "Excel 통합 문서를 다시 열어 구조와 값을 확인했습니다.", details)
        except Exception as exc:
            return VerificationResult(False, f"Excel 재열기 검증에 실패했습니다: {exc}")

    def _verify_write_excel_cell(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """이전 단일 셀 도구의 저장 값을 재조회한다."""
        cell = tool_input.get("cell") or tool_input.get("address")
        if not cell or "value" not in tool_input:
            return VerificationResult(False, "검증할 셀 주소 또는 값이 없습니다.", verified=False)
        copied = dict(tool_input)
        copied["cells"] = {str(cell): tool_input.get("value")}
        return self._verify_write_excel_cells(copied, result)

    def _verify_write_excel_cells(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """수정된 XLSX를 다시 열어 요청한 모든 셀 값을 확인한다."""
        path = self._path_from(tool_input, result)
        cells = tool_input.get("cells")
        if path is None or not isinstance(cells, Mapping) or not cells:
            return VerificationResult(False, "검증할 Excel 경로 또는 셀 값이 없습니다.", verified=False)
        try:
            from openpyxl import load_workbook
            failure, snapshot = self._validate_file_snapshot(
                path, tool_input, result, require_changed=True
            )
            if failure:
                return failure
            workbook = load_workbook(path, read_only=True, data_only=False)
            try:
                sheet_name = str(tool_input.get("sheet") or "Sheet1")
                if sheet_name not in workbook.sheetnames:
                    return VerificationResult(False, f"요청한 시트가 없습니다: {sheet_name}")
                sheet = workbook[sheet_name]
                mismatches = {
                    str(address): {"expected": expected, "actual": sheet[str(address)].value}
                    for address, expected in cells.items()
                    if sheet[str(address)].value != expected
                }
                if mismatches:
                    return VerificationResult(False, "Excel 셀 사후조건이 일치하지 않습니다.", {"mismatches": mismatches})
            finally:
                workbook.close()
            return VerificationResult(
                True, "Excel 파일을 다시 열어 요청한 모든 셀 값을 확인했습니다.",
                {**snapshot, "sheet": sheet_name, "cells": dict(cells), "method": "openpyxl_cell_readback"},
            )
        except Exception as exc:
            return VerificationResult(False, f"Excel 셀 재조회 검증에 실패했습니다: {exc}")

    def _verify_word_document(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """DOCX ZIP 무결성과 재열기 텍스트를 확인한다."""
        path = self._path_from(tool_input, result)
        if path is None:
            return VerificationResult(False, "Word 파일 경로가 누락되었습니다.")
        try:
            from docx import Document
            failure, snapshot = self._validate_file_snapshot(path, tool_input, result)
            if failure:
                return failure
            with zipfile.ZipFile(path) as package:
                bad_member = package.testzip()
                if bad_member:
                    return VerificationResult(False, f"DOCX 패키지 구성원이 손상되었습니다: {bad_member}")
            document = Document(path)
            paragraphs = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    paragraphs.extend(cell.text for cell in row.cells)
            normalized = self._normalized_text("\n".join(paragraphs))
            missing = [text for text in self._requested_document_texts(tool_input)
                       if self._normalized_text(text) not in normalized]
            if missing:
                return VerificationResult(False, "DOCX 재열기 결과에 요청 내용이 없습니다.", {"missing": missing})
            return VerificationResult(
                True, "Word 문서를 다시 열어 패키지 구조와 요청 텍스트를 확인했습니다.",
                {**snapshot, "paragraphs": len(paragraphs), "method": "python_docx_reopen"},
            )
        except Exception as exc:
            return VerificationResult(False, f"Word 재열기 검증에 실패했습니다: {exc}")

    def _verify_powerpoint_presentation(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """PPTX를 다시 열어 슬라이드 수와 요청 텍스트를 확인한다."""
        path = self._path_from(tool_input, result)
        if path is None:
            return VerificationResult(False, "PowerPoint 파일 경로가 누락되었습니다.")
        try:
            from pptx import Presentation
            failure, snapshot = self._validate_file_snapshot(path, tool_input, result)
            if failure:
                return failure
            with zipfile.ZipFile(path) as package:
                bad_member = package.testzip()
                if bad_member:
                    return VerificationResult(False, f"PPTX 패키지 구성원이 손상되었습니다: {bad_member}")
            presentation = Presentation(path)
            extracted: list[str] = []
            for slide in presentation.slides:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        extracted.append(shape.text)
                    if getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            extracted.extend(cell.text for cell in row.cells)
            requested = ([str(tool_input["title"])] if tool_input.get("title") is not None else [])
            for item in tool_input.get("slides") or []:
                if item.get("title") is not None:
                    requested.append(str(item["title"]))
                requested.extend(str(value) for value in (item.get("bullets") or []))
            normalized = self._normalized_text("\n".join(extracted))
            missing = [text for text in requested if text and self._normalized_text(text) not in normalized]
            if missing:
                return VerificationResult(False, "PPTX 재열기 결과에 요청 내용이 없습니다.", {"missing": missing})
            expected_slides = len(tool_input.get("slides") or []) + (1 if tool_input.get("title") else 0)
            if expected_slides and len(presentation.slides) != expected_slides:
                return VerificationResult(
                    False, "PPTX 슬라이드 수가 요청과 다릅니다.",
                    {"expected": expected_slides, "actual": len(presentation.slides)},
                )
            return VerificationResult(
                True, "PowerPoint를 다시 열어 슬라이드 구조와 요청 텍스트를 확인했습니다.",
                {**snapshot, "slides": len(presentation.slides), "method": "python_pptx_reopen"},
            )
        except Exception as exc:
            return VerificationResult(False, f"PowerPoint 재열기 검증에 실패했습니다: {exc}")

    def _verify_pdf_document(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """PDF 파서를 통해 페이지와 추출 텍스트를 재확인한다."""
        path = self._path_from(tool_input, result)
        if path is None:
            return VerificationResult(False, "PDF 파일 경로가 누락되었습니다.")
        try:
            import fitz
            failure, snapshot = self._validate_file_snapshot(path, tool_input, result)
            if failure:
                return failure
            with fitz.open(path) as document:
                page_count = document.page_count
                extracted = "\n".join(page.get_text() for page in document)
            if page_count < 1:
                return VerificationResult(False, "PDF에 페이지가 없습니다.")
            normalized = self._normalized_text(extracted)
            missing = [text for text in self._requested_document_texts(tool_input)
                       if self._normalized_text(text) not in normalized]
            if missing:
                return VerificationResult(False, "PDF 재열기 결과에 요청 텍스트가 없습니다.", {"missing": missing})
            return VerificationResult(
                True, "PDF를 다시 열어 페이지와 요청 텍스트를 확인했습니다.",
                {**snapshot, "pages": page_count, "text_chars": len(extracted), "method": "pymupdf_reopen"},
            )
        except Exception as exc:
            return VerificationResult(False, f"PDF 재열기 검증에 실패했습니다: {exc}")

    def _verify_hwpx_document(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """HWPX 패키지를 다시 열어 내보낸 텍스트와 요청을 비교한다."""
        path = self._path_from(tool_input, result)
        if path is None:
            return VerificationResult(False, "HWPX 파일 경로가 누락되었습니다.")
        try:
            from hwpx import HwpxDocument
            failure, snapshot = self._validate_file_snapshot(path, tool_input, result)
            if failure:
                return failure
            with zipfile.ZipFile(path) as package:
                bad_member = package.testzip()
                if bad_member:
                    return VerificationResult(False, f"HWPX 패키지 구성원이 손상되었습니다: {bad_member}")
            document = HwpxDocument.open(path)
            extracted = document.export_text()
            normalized = self._normalized_text(extracted)
            missing = [text for text in self._requested_document_texts(tool_input)
                       if self._normalized_text(text) not in normalized]
            if missing:
                return VerificationResult(False, "HWPX 재열기 결과에 요청 텍스트가 없습니다.", {"missing": missing})
            return VerificationResult(
                True, "HWPX를 다시 열어 패키지와 요청 텍스트를 확인했습니다.",
                {**snapshot, "text_chars": len(extracted), "method": "hwpx_reopen"},
            )
        except Exception as exc:
            return VerificationResult(False, f"HWPX 재열기 검증에 실패했습니다: {exc}")

    @staticmethod
    def _unescape_ical(value: str) -> str:
        return (value.replace("\\n", "\n").replace("\\,", ",")
                .replace("\\;", ";").replace("\\\\", "\\"))

    @staticmethod
    def _expected_ical_datetime(value: Any) -> tuple[str, str]:
        text = str(value)
        parsed = datetime.fromisoformat(text)
        if "T" not in text:
            return ";VALUE=DATE", parsed.strftime("%Y%m%d")
        return "", parsed.strftime("%Y%m%dT%H%M%S")

    def _verify_calendar_create_event(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        path = self._path_from(tool_input, result)
        if path is None:
            return VerificationResult(False, "캘린더 파일 경로가 누락되었습니다.")
        try:
            failure, snapshot = self._validate_file_snapshot(path, tool_input, result)
            if failure:
                return failure
            raw = path.read_text(encoding="utf-8")
            unfolded = re.sub(r"\r?\n[ \t]", "", raw)
            required = (
                "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:",
                "DTSTAMP:", "DTSTART", "DTEND", "SUMMARY:", "END:VEVENT", "END:VCALENDAR",
            )
            missing_markers = [marker for marker in required if marker not in unfolded]
            if missing_markers:
                return VerificationResult(False, "iCalendar 필수 구조가 누락되었습니다.", {"missing": missing_markers})

            lines = unfolded.replace("\r\n", "\n").split("\n")
            properties: Dict[str, list[str]] = {}
            for line in lines:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                properties.setdefault(key, []).append(value)
            if tool_input.get("title") is not None:
                summary = self._unescape_ical((properties.get("SUMMARY") or [""])[0])
                if summary != str(tool_input["title"]):
                    return VerificationResult(False, "iCalendar 제목이 요청과 다릅니다.", {"actual": summary})
            for field in ("start", "end"):
                if tool_input.get(field) is None:
                    continue
                suffix, expected = self._expected_ical_datetime(tool_input[field])
                key = f"DT{field.upper()}{suffix}"
                actual = (properties.get(key) or [None])[0]
                if actual != expected:
                    return VerificationResult(
                        False, f"iCalendar {field} 값이 요청과 다릅니다.",
                        {"expected": expected, "actual": actual, "property": key},
                    )
            if tool_input.get("description") is not None:
                description = self._unescape_ical((properties.get("DESCRIPTION") or [""])[0])
                if description != str(tool_input.get("description", "")):
                    return VerificationResult(False, "iCalendar 설명이 요청과 다릅니다.")
            if tool_input.get("start") and tool_input.get("end"):
                if datetime.fromisoformat(str(tool_input["end"])) <= datetime.fromisoformat(str(tool_input["start"])):
                    return VerificationResult(False, "iCalendar 종료 시각이 시작 시각보다 빠르거나 같습니다.")
            return VerificationResult(
                True, "iCalendar를 다시 읽어 구조·제목·날짜 사후조건을 확인했습니다.",
                {**snapshot, "method": "icalendar_reparse"},
            )
        except Exception as exc:
            return VerificationResult(False, f"캘린더 파일 재검증 중 오류가 발생했습니다: {exc}")

    def _verify_add_document(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """현재 RAG 저장소를 재조회하고 원본 해시·Chunk를 확인한다."""
        try:
            value = tool_input.get("file_path") or tool_input.get("path")
            if not value:
                return VerificationResult(False, "RAG 원본 문서 경로가 누락되었습니다.")
            path = Path(str(value)).expanduser().resolve()
            if not path.is_file():
                return VerificationResult(False, f"RAG 원본 문서가 없습니다: {path}")
            text = path.read_text(encoding="utf-8")
            expected_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if not text.strip():
                return VerificationResult(False, "RAG 원본 문서가 비어 있습니다.")

            from core import rag as rag_module
            manager = getattr(rag_module, "_rag_manager", None)
            if manager is None:
                return VerificationResult(
                    False, "현재 RAG 저장소 인스턴스가 없어 저장 결과를 재조회할 수 없습니다.",
                    verified=False,
                )
            namespace = str(tool_input.get("namespace") or getattr(manager, "namespace", "global"))
            matches = []
            for key, record in dict(getattr(manager, "documents", {}) or {}).items():
                if not isinstance(record, Mapping):
                    continue
                source = record.get("source")
                try:
                    same_source = bool(source) and Path(str(source)).expanduser().resolve() == path
                except (OSError, ValueError):
                    same_source = False
                if same_source and str(record.get("namespace", namespace)) == namespace:
                    matches.append((str(key), record))
            if not matches:
                return VerificationResult(False, "RAG 저장소 재조회에서 원본 문서를 찾지 못했습니다.")
            key, record = matches[-1]
            chunks = list(record.get("chunks") or [])
            if not chunks or any(not str(chunk).strip() for chunk in chunks):
                return VerificationResult(False, "RAG 문서에 유효한 Chunk가 없습니다.")
            if str(record.get("content_sha256", "")).casefold() != expected_hash.casefold():
                return VerificationResult(
                    False, "RAG 저장 문서 해시가 현재 원본과 일치하지 않습니다.",
                    {"expected_sha256": expected_hash, "actual_sha256": record.get("content_sha256")},
                )
            return VerificationResult(
                True, "RAG 저장소에서 원본 해시와 Chunk를 재조회했습니다.",
                {"document_key": key, "namespace": namespace, "chunks": len(chunks),
                 "content_sha256": expected_hash, "method": "rag_catalog_requery"},
            )
        except Exception as exc:
            return VerificationResult(False, f"RAG 문서 재조회 검증 중 오류가 발생했습니다: {exc}")

    @staticmethod
    def _as_values(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (str, bytes, Path)):
            return [value]
        if isinstance(value, Iterable):
            return list(value)
        return [value]

    def _verify_command_postconditions(self, contract: Mapping[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
        details: Dict[str, Any] = {"paths_exist": [], "paths_absent": [], "files": {}}
        for value in self._as_values(contract.get("expected_paths_exist")):
            path = Path(str(value)).expanduser().resolve()
            if not path.exists():
                return f"명령 사후조건 경로가 존재하지 않습니다: {path}", details
            details["paths_exist"].append(str(path))
        for value in self._as_values(contract.get("expected_paths_absent")):
            path = Path(str(value)).expanduser().resolve()
            if path.exists():
                return f"명령 사후조건 경로가 여전히 존재합니다: {path}", details
            details["paths_absent"].append(str(path))
        expected_hashes = contract.get("expected_file_sha256") or {}
        if not isinstance(expected_hashes, Mapping):
            return "expected_file_sha256 계약은 경로-해시 매핑이어야 합니다.", details
        for raw_path, expected in expected_hashes.items():
            path = Path(str(raw_path)).expanduser().resolve()
            if not path.is_file():
                return f"해시 검증 대상 파일이 없습니다: {path}", details
            snapshot = self._file_snapshot(path)
            if snapshot["sha256"].casefold() != str(expected).casefold():
                return f"명령 사후조건 파일 해시가 다릅니다: {path}", details
            details["files"][str(path)] = snapshot
        content_contract = contract.get("expected_content_contains") or {}
        if not isinstance(content_contract, Mapping):
            return "expected_content_contains 계약은 경로-문구 매핑이어야 합니다.", details
        for raw_path, expected_values in content_contract.items():
            path = Path(str(raw_path)).expanduser().resolve()
            if not path.is_file():
                return f"내용 검증 대상 파일이 없습니다: {path}", details
            content = path.read_text(encoding="utf-8")
            missing = [str(value) for value in self._as_values(expected_values) if str(value) not in content]
            if missing:
                return f"명령 사후조건 파일에 요청 문구가 없습니다: {path}", details
            details["files"].setdefault(str(path), self._file_snapshot(path))
        return None, details

    def _verify_run_command(self, tool_input: Dict[str, Any], result: Any) -> VerificationResult:
        """구조화 종료 코드와 명시적 파일/출력 사후조건을 검증한다."""
        try:
            raw = self._raw_output(result)
            contract = self._contract(tool_input)
            payload = self._payload(result)
            evidence = self._evidence_data(result, "process_execution")
            candidates: list[int] = []
            for value in (evidence.get("returncode"), payload.get("returncode"), payload.get("exit_code")):
                if value is not None:
                    candidates.append(int(value))
            parsed_codes = [int(value) for value in re.findall(r"(?m)^종료 코드:\s*(-?\d+)\s*$", raw)]
            if parsed_codes:
                candidates.append(parsed_codes[-1])
            if not candidates:
                return VerificationResult(
                    False, "확인 가능한 프로세스 종료 코드가 없습니다.",
                    {"result": raw[:200]}, verified=False,
                )
            if len(set(candidates)) != 1:
                return VerificationResult(False, "도구 결과의 종료 코드 증거가 서로 충돌합니다.", {"codes": candidates})
            actual_code = candidates[0]
            expected_code = int(contract.get("expected_exit_code", 0))
            if actual_code != expected_code:
                return VerificationResult(
                    False, f"명령 종료 코드가 예상과 다릅니다: {actual_code}",
                    {"expected_exit_code": expected_code, "actual_exit_code": actual_code},
                )
            missing_output = [str(value) for value in self._as_values(contract.get("expected_stdout_contains"))
                              if str(value) not in raw]
            if missing_output:
                return VerificationResult(False, "명령 출력에 예상 문구가 없습니다.", {"missing": missing_output})
            pattern = contract.get("expected_output_regex")
            if pattern and re.search(str(pattern), raw, re.MULTILINE) is None:
                return VerificationResult(False, "명령 출력이 예상 정규식과 일치하지 않습니다.", {"pattern": str(pattern)})
            post_error, post_details = self._verify_command_postconditions(contract)
            if post_error:
                return VerificationResult(False, post_error, post_details)
            return VerificationResult(
                True, "프로세스 종료 코드와 명시된 사후조건을 확인했습니다.",
                {"returncode": actual_code, "postconditions": post_details,
                 "method": "process_exit_and_postconditions"},
            )
        except (OSError, UnicodeError, TypeError, ValueError, re.error) as exc:
            return VerificationResult(False, f"명령어 검증 중 오류가 발생했습니다: {exc}")


# Singleton instance
_verifier = None


def get_tool_verifier() -> ToolVerifier:
    """ToolVerifier 싱글톤 인스턴스 반환"""
    global _verifier
    if _verifier is None:
        _verifier = ToolVerifier()
    return _verifier
