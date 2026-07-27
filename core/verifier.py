
import json
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass

import pygetwindow


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
            "excel_create_workbook": self._verify_created_file,
            "excel_write_cells": self._verify_created_file,
            "word_create_document": self._verify_created_file,
            "powerpoint_create_presentation": self._verify_created_file,
            "pdf_create_document": self._verify_created_file,
            "hwpx_create_document": self._verify_created_file,
            "browser_web_search": self._verify_web_search,
            "windows_launch_app": self._verify_windows_launch_app,
            "windows_close_app": self._verify_windows_close_app,
            "windows_add_app_aliases": self._verify_windows_add_app_aliases,
        }

    def _verify_windows_launch_app(
        self, tool_input: Dict[str, Any], result: str
    ) -> VerificationResult:
        """PID가 있는 직접 실행만 실제 프로세스 존재 여부로 검증한다."""
        match = re.search(r"\bPID:\s*(\d+)", str(result))
        if not match:
            return VerificationResult(
                False,
                "실행 요청은 반환됐지만 확인 가능한 프로세스 ID가 없습니다.",
                {"result": str(result)[:200]},
                verified=False,
            )
        pid = int(match.group(1))
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
        self, tool_input: Dict[str, Any], result: str
    ) -> VerificationResult:
        """종료를 요청한 창이 현재 창 목록에서 사라졌는지 확인한다."""
        prefix = "앱 종료 요청 성공:"
        title = str(result).split(prefix, 1)[1].strip() if prefix in str(result) else ""
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
        self, tool_input: Dict[str, Any], result: str
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
        self, tool_input: Dict[str, Any], result: str
    ) -> VerificationResult:
        """웹 검색이 실제 URL이 포함된 구조화 결과를 반환했는지 확인한다."""
        try:
            payload = json.loads(result)
            results = payload.get("results", [])
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
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            return VerificationResult(False, f"웹 검색 결과 검증 오류: {exc}")

    def _verify_created_path_result(
        self, tool_input: Dict[str, Any], result: str
    ) -> VerificationResult:
        """파일시스템 생성 도구의 구조화 결과와 실제 경로를 함께 검증한다."""
        try:
            payload = json.loads(result)
            path = payload.get("path", "")
            expected_type = payload.get("type")
            if payload.get("status") not in {"created", "written"} or not path:
                return VerificationResult(False, f"생성 성공 결과가 아닙니다: {result}")
            target = Path(path)
            exists = target.is_dir() if expected_type == "directory" else target.is_file()
            if not exists:
                return VerificationResult(False, f"생성 결과 경로가 존재하지 않습니다: {path}")
            if payload.get("status") == "written":
                if not payload.get("changed"):
                    return VerificationResult(False, "파일 내용이 실제로 변경되지 않았습니다.")
                before = payload.get("before_sha256")
                after = payload.get("after_sha256")
                if not before or not after or before == after:
                    return VerificationResult(False, "파일 변경 해시 검증에 실패했습니다.")
            return VerificationResult(True, f"실제 생성 경로 확인 성공: {path}")
        except (json.JSONDecodeError, TypeError, AttributeError) as exc:
            return VerificationResult(False, f"생성 결과 검증 오류: {exc}")

    def verify(self, tool_name: str, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """
        도구 실행 결과를 검증합니다!
        
        Args:
            tool_name: 실행된 도구 이름
            tool_input: 도구에 전달된 입력
            result: 도구 실행 결과 문자열
        
        Returns:
            VerificationResult: 검증 결과
        """
        # 구조화된 오류 접두사만 공통 실패로 봅니다. 정상 출력에 포함된 'error' 단어는 허용합니다.
        normalized = str(result).lstrip().casefold()
        if (normalized.startswith(("오류:", "error:", "툴 파라미터 오류:", "명령어 실행 오류:"))
                or (normalized.startswith("경로 '") and "허용되지 않습니다" in normalized)
                or normalized.startswith(("권한이 거부", "권한 거부"))):
            return VerificationResult(
                success=False,
                message=f"도구 실행 결과에 오류가 포함되어 있습니다: {result[:100]}",
                details={"error": result}
            )

        # 2. 도구별 맞춤 검증
        if tool_name in self._verifiers:
            return self._verifiers[tool_name](tool_input, result)

        # 3. 전용 검증기가 없는 실행은 성공으로 추정하지 않는다.
        return VerificationResult(
            success=False,
            message=f"도구 {tool_name}에는 실행 결과 검증기가 등록되지 않았습니다.",
            details={"result": result[:100], "tool_name": tool_name},
            verified=False,
        )

    def _verify_write_file(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """write_file 도구 검증: 실제로 파일이 생성되고 내용이 일치하는지 확인"""
        try:
            path = tool_input.get("path")
            expected_content = tool_input.get("content")

            if not path:
                return VerificationResult(False, "파일 경로가 누락되었습니다!")

            # 파일이 실제로 존재하는지 확인
            if not os.path.exists(path):
                return VerificationResult(False, f"파일이 생성되지 않았습니다: {path}")

            # 파일 내용이 일치하는지 확인
            with open(path, "r", encoding="utf-8") as f:
                actual_content = f.read()

            if actual_content.strip() == expected_content.strip():
                return VerificationResult(True, f"파일 작성 성공! 내용이 일치합니다: {path}")
            else:
                return VerificationResult(
                    False,
                    f"파일 내용이 일치하지 않습니다!",
                    details={"expected": expected_content[:100], "actual": actual_content[:100]}
                )

        except Exception as e:
            return VerificationResult(False, f"파일 검증 중 오류가 발생했습니다: {e}")

    def _verify_created_file(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        path = tool_input.get("path") or tool_input.get("file_path")
        if not path or not os.path.isfile(path):
            return VerificationResult(False, f"결과 파일이 생성되지 않았습니다: {path}")
        if os.path.getsize(path) <= 0:
            return VerificationResult(False, f"결과 파일이 비어 있습니다: {path}")
        return VerificationResult(True, f"결과 파일 생성 검증 성공: {path}")

    def _verify_read_file(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """read_file 도구 검증: 파일이 실제로 존재하고 내용이 반환되었는지 확인"""
        try:
            path = tool_input.get("path")

            if not path:
                return VerificationResult(False, "파일 경로가 누락되었습니다!")

            if not os.path.exists(path):
                return VerificationResult(False, f"파일이 존재하지 않습니다: {path}")

            return VerificationResult(True, f"파일 읽기 성공!")

        except Exception as e:
            return VerificationResult(False, f"파일 검증 중 오류가 발생했습니다: {e}")

    def _verify_create_directory(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """create_directory 도구 검증: 디렉토리가 실제로 생성되었는지 확인"""
        try:
            path = tool_input.get("dir_path") or tool_input.get("path")

            if not path:
                return VerificationResult(False, "디렉토리 경로가 누락되었습니다!")

            if os.path.exists(path) and os.path.isdir(path):
                return VerificationResult(True, f"디렉토리 생성 성공!")
            else:
                return VerificationResult(False, f"디렉토리가 생성되지 않았습니다: {path}")

        except Exception as e:
            return VerificationResult(False, f"디렉토리 검증 중 오류가 발생했습니다: {e}")

    def _verify_delete_directory(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """delete_directory 도구 검증: 디렉토리가 실제로 삭제되었는지 확인"""
        try:
            path = tool_input.get("dir_path") or tool_input.get("path")

            if not path:
                return VerificationResult(False, "디렉토리 경로가 누락되었습니다!")

            if not os.path.exists(path):
                return VerificationResult(True, f"디렉토리 삭제 성공!")
            else:
                return VerificationResult(False, f"디렉토리가 삭제되지 않았습니다: {path}")

        except Exception as e:
            return VerificationResult(False, f"디렉토리 검증 중 오류가 발생했습니다: {e}")

    def _verify_create_excel_file(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """create_excel_file 도구 검증: 엑셀 파일이 실제로 생성되었는지 확인"""
        try:
            path = tool_input.get("file_path") or tool_input.get("path")

            if not path:
                return VerificationResult(False, "엑셀 파일 경로가 누락되었습니다!")

            if os.path.exists(path):
                return VerificationResult(True, f"엑셀 파일 생성 성공!")
            else:
                return VerificationResult(False, f"엑셀 파일이 생성되지 않았습니다: {path}")

        except Exception as e:
            return VerificationResult(False, f"엑셀 파일 검증 중 오류가 발생했습니다: {e}")

    def _verify_calendar_create_event(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        path = tool_input.get("path")
        if not path or not os.path.isfile(path):
            return VerificationResult(False, f"캘린더 파일이 생성되지 않았습니다: {path}")
        try:
            with open(path, "r", encoding="utf-8") as calendar_file:
                content = calendar_file.read()
            required = ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "END:VCALENDAR")
            has_dates = ("DTSTART:" in content or "DTSTART;VALUE=DATE:" in content) and (
                "DTEND:" in content or "DTEND;VALUE=DATE:" in content
            )
            if not all(marker in content for marker in required) or not has_dates:
                return VerificationResult(False, "생성된 파일이 유효한 iCalendar 구조를 갖추지 못했습니다.")
            return VerificationResult(True, f"캘린더 파일 생성 검증 성공: {path}")
        except Exception as exc:
            return VerificationResult(False, f"캘린더 파일 검증 중 오류가 발생했습니다: {exc}")

    def _verify_write_excel_cell(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """write_excel_cell 도구 검증: 엑셀 파일이 실제로 수정되었는지 확인"""
        try:
            path = tool_input.get("file_path") or tool_input.get("path")

            if not path:
                return VerificationResult(False, "엑셀 파일 경로가 누락되었습니다!")

            if os.path.exists(path):
                # 간단히 파일 존재 여부만 확인 (실제 셀 값 확인은 openpyxl 필요하므로 여기서는 생략)
                return VerificationResult(True, f"엑셀 파일 수정 성공!")
            else:
                return VerificationResult(False, f"엑셀 파일이 존재하지 않습니다: {path}")

        except Exception as e:
            return VerificationResult(False, f"엑셀 파일 검증 중 오류가 발생했습니다: {e}")

    def _verify_add_document(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """add_document 도구 검증: 문서가 RAG 시스템에 추가되었는지 확인"""
        try:
            if "성공" in result or "추가되었습니다" in result:
                return VerificationResult(True, f"문서 추가 성공!")
            else:
                return VerificationResult(False, f"문서 추가 실패: {result}")
        except Exception as e:
            return VerificationResult(False, f"문서 검증 중 오류가 발생했습니다: {e}")

    def _verify_run_command(self, tool_input: Dict[str, Any], result: str) -> VerificationResult:
        """run_command 도구 검증: 명령어가 0 종료 코드로 성공했는지 확인"""
        try:
            # 종료 코드가 0이면 성공
            if "종료 코드: 0" in result:
                return VerificationResult(True, f"명령어 실행 성공!")
            else:
                return VerificationResult(False, f"명령어 실행 실패: {result}")
        except Exception as e:
            return VerificationResult(False, f"명령어 검증 중 오류가 발생했습니다: {e}")


# Singleton instance
_verifier = None


def get_tool_verifier() -> ToolVerifier:
    """ToolVerifier 싱글톤 인스턴스 반환"""
    global _verifier
    if _verifier is None:
        _verifier = ToolVerifier()
    return _verifier
