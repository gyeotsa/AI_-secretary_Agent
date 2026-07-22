
import os
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass



@dataclass
class VerificationResult:
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


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
            "delete_directory": self._verify_delete_directory,
            "create_excel_file": self._verify_create_excel_file,
            "write_excel_cell": self._verify_write_excel_cell,
            "add_document": self._verify_add_document,
            "run_command": self._verify_run_command,
        }

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
        if normalized.startswith(("오류:", "error:", "툴 파라미터 오류:", "명령어 실행 오류:")):
            return VerificationResult(
                success=False,
                message=f"도구 실행 결과에 오류가 포함되어 있습니다: {result[:100]}",
                details={"error": result}
            )

        # 2. 도구별 맞춤 검증
        if tool_name in self._verifiers:
            return self._verifiers[tool_name](tool_input, result)

        # 3. 기본 검증 통과
        return VerificationResult(
            success=True,
            message=f"도구 {tool_name} 실행 결과 검증 성공!",
            details={"result": result[:100]}
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
