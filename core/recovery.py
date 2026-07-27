
import os
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from core.scratchpad import Task
from core.tools import get_tool_executor, get_tool_names
from core.tool_result import ToolRunResult


@dataclass
class RecoveryResult:
    success: bool
    result: Optional[ToolRunResult] = None
    message: str = ""
    retry_count: int = 0


class RecoveryManager:
    """
    도구 실행 실패 시 복구를 관리하는 클래스!
    Retry → Parameter 수정 → Alternative Tool → Planner 재호출 → Human Approval → Abort 순서로 시도합니다.
    """

    def __init__(self):
        self.tool_executor = get_tool_executor()
        self.max_retries = 2

        # 대체 도구 매핑: 원래 도구 → 대체 도구 목록
        self._alternative_tools: Dict[str, list] = {
            "write_file": ["create_excel_file"],
            "create_excel_file": ["write_file"],
            "list_directory": ["read_file"],
        }

    def recover(
        self,
        task: Task,
        tool_name: str,
        tool_input: Dict[str, Any],
        last_result: str,
        retry_count: int = 0
    ) -> RecoveryResult:
        """
        실패 복구를 순차적으로 시도합니다!
        
        Args:
            task: 실패한 Task
            tool_name: 실패한 도구 이름
            tool_input: 도구 입력
            last_result: 마지막 실행 결과
            retry_count: 현재 재시도 횟수
        
        Returns:
            RecoveryResult: 복구 결과
        """
        print(f"[RecoveryManager] 복구 시작: {tool_name}, 재시도 횟수: {retry_count}")

        # 1. Retry
        if retry_count < self.max_retries:
            print(f"[RecoveryManager] 1단계: Retry 시도...")
            result = self._try_retry(tool_name, tool_input)
            if result.success:
                return RecoveryResult(
                    success=True,
                    result=result.result,
                    message=f"Retry 성공! (재시도 {retry_count + 1}회)",
                    retry_count=retry_count + 1
                )

        # 2. Parameter 수정
        print(f"[RecoveryManager] 2단계: Parameter 수정 시도...")
        result = self._try_modify_parameters(tool_name, tool_input, last_result)
        if result.success:
            return RecoveryResult(
                success=True,
                result=result.result,
                message="파라미터 수정으로 복구 성공!",
                retry_count=retry_count + 1
            )

        # 3. Alternative Tool
        print(f"[RecoveryManager] 3단계: Alternative Tool 시도...")
        result = self._try_alternative_tool(task, tool_name, tool_input)
        if result.success:
            return RecoveryResult(
                success=True,
                result=result.result,
                message=f"대체 도구로 복구 성공!",
                retry_count=retry_count + 1
            )

        # 4. Fallback: 간단한 응답으로 대체
        print(f"[RecoveryManager] 4단계: Fallback 처리...")
        return RecoveryResult(
            success=False,
            result=ToolRunResult.failed(
                tool_name=tool_name,
                error=f"Task '{task.description}'은(는) 실패했습니다. 원인: {last_result[:100]}",
            ),
            message="Fallback 처리로 대체합니다.",
            retry_count=retry_count + 1
        )

    def _try_retry(self, tool_name: str, tool_input: Dict[str, Any]) -> RecoveryResult:
        """단순 재시도"""
        try:
            result = self.tool_executor.execute_tool(tool_name, tool_input)
            if result.succeeded:
                return RecoveryResult(success=True, result=result)
            return RecoveryResult(success=False, message=f"Retry 실패: {result}")
        except Exception as e:
            return RecoveryResult(success=False, message=f"Retry 오류: {e}")

    def _try_modify_parameters(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        last_result: str
    ) -> RecoveryResult:
        """파라미터 수정 시도 (예: 경로 정리, 인코딩 변경 등)"""
        try:
            modified_input = tool_input.copy()

            # 파일 도구의 경우 경로 정리
            if tool_name in ["write_file", "read_file", "create_directory", "delete_directory"]:
                path_key = "path" if "path" in modified_input else "dir_path" if "dir_path" in modified_input else "file_path"
                if path_key in modified_input:
                    path = modified_input[path_key]
                    # 경로 정리: ~ 확장, 절대경로로 변환, 공백 제거
                    modified_input[path_key] = os.path.abspath(os.path.expanduser(path.strip()))
                    print(f"[RecoveryManager] 경로 정리: {path} → {modified_input[path_key]}")

            # 수정된 파라미터로 재시도
            result = self.tool_executor.execute_tool(tool_name, modified_input)
            if result.succeeded:
                return RecoveryResult(success=True, result=result)
            return RecoveryResult(success=False, message=f"파라미터 수정 실패: {result}")

        except Exception as e:
            return RecoveryResult(success=False, message=f"파라미터 수정 오류: {e}")

    def _try_alternative_tool(
        self,
        task: Task,
        original_tool: str,
        original_input: Dict[str, Any]
    ) -> RecoveryResult:
        """대체 도구 시도"""
        try:
            alternatives = self._alternative_tools.get(original_tool, [])
            for alt_tool in alternatives:
                if alt_tool not in get_tool_names():
                    continue

                print(f"[RecoveryManager] 대체 도구 시도: {alt_tool}")

                # 대체 도구에 맞는 입력으로 변환 (간단한 케이스만 처리)
                alt_input = original_input.copy()

                # 실제로 대체 도구 실행
                result = self.tool_executor.execute_tool(alt_tool, alt_input)
                if result.succeeded:
                    return RecoveryResult(success=True, result=result)

            return RecoveryResult(success=False, message="적합한 대체 도구가 없습니다.")

        except Exception as e:
            return RecoveryResult(success=False, message=f"대체 도구 오류: {e}")


# Singleton instance
_recovery_manager = None


def get_recovery_manager() -> RecoveryManager:
    """RecoveryManager 싱글톤 인스턴스 반환"""
    global _recovery_manager
    if _recovery_manager is None:
        _recovery_manager = RecoveryManager()
    return _recovery_manager
