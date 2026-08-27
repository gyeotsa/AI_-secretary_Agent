from typing import Optional, Dict, Any
from core.llm import get_llm_client
from core.scratchpad import Scratchpad, get_scratchpad


class Reflection:
    """
    실패를 분석하고 복구 전략을 제안하는 Reflection 클래스
    - 실패 원인 분석 (LLM 사용)
    - 복구 전략 제안
    - 재계획 필요 여부 반환
    """

    def __init__(self, *, llm=None, scratchpad: Optional[Scratchpad] = None):
        self.llm = llm or get_llm_client("reasoning")
        self.scratchpad = scratchpad or get_scratchpad()

    def analyze_failure(self, error: str, task_description: str) -> Dict[str, Any]:
        """
        실패를 분석합니다.
        
        Args:
            error: 실패 메시지
            task_description: 실패한 작업 설명
            
        Returns:
            분석 결과 (원인, 복구 전략, 재계획 필요 여부)
        """
        print(f"[Reflection] 실패 분석 시작: {task_description}")
        print(f"[Reflection] 오류 메시지: {error}")

        # LLM에 전달할 시스템 프롬프트
        system_prompt = """당신은 AI 어시스턴트의 Reflection 모듈입니다. 작업 실패의 원인을 분석하고 복구 전략을 제안해야 합니다.

규칙:
1. 실패 원인을 명확히 분석하세요.
2. 가능한 복구 전략을 제안하세요.
3. 재계획이 필요한지 여부를 반환하세요. (true/false)
4. 한국어로 답변하세요.

응답 형식 (JSON만 반환하세요!):
{
    "cause": "실패 원인",
    "recovery_strategy": "복구 전략 설명",
    "should_replan": true 또는 false,
    "suggested_fix": "수정 제안 (있으면)"
}
"""

        # 사용자 프롬프트
        user_prompt = f"실패한 작업: {task_description}\n오류 메시지: {error}\n\nScratchpad 내용:\n{self.scratchpad.get_context()}\n\n위 실패를 분석해주세요."

        try:
            # LLM 호출
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])

            # JSON 파싱
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                response = response.split("```")[1].strip()

            import json
            result = json.loads(response)

            print(f"[Reflection] 분석 결과: {result}")
            return result

        except Exception as e:
            print(f"[Reflection] 분석 오류: {e}")
            import traceback
            traceback.print_exc()
            # 기본 반환값
            return {
                "cause": "분석 오류",
                "recovery_strategy": "다시 시도해보세요",
                "should_replan": False,
                "suggested_fix": None
            }

    def suggest_retry_params(self, original_tool_name: str, original_input: Dict[str, Any], analysis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        재시도할 Tool의 파라미터를 제안합니다.
        
        Args:
            original_tool_name: 원래 Tool 이름
            original_input: 원래 입력
            analysis: 분석 결과
            
        Returns:
            제안된 새로운 입력 (없으면 None)
        """
        suggested_fix = analysis.get("suggested_fix")
        if not suggested_fix:
            return None

        # 간단한 구현: suggested_fix에서 파라미터 추출 (실제로는 LLM으로 더 정교히)
        # 여기서는 그냥 원래 입력 반환 (기본값)
        return original_input


# Singleton instance
_reflection = None


def get_reflection() -> Reflection:
    """Reflection 싱글톤 인스턴스 반환"""
    global _reflection
    if _reflection is None:
        _reflection = Reflection()
    return _reflection
