from typing import List, Optional, Dict, Any
from core.llm import get_llm_client
from core.scratchpad import get_scratchpad, Task, Observation
from core.planner import get_planner, DecomposedTask
from core.tools import get_tool_executor
from core.reflection import get_reflection


class Executor:
    """
    작업을 실행하는 Executor 클래스
    - Planner에서 분해한 작업을 순서대로 실행
    - 실행 결과를 Scratchpad에 기록
    - 실패시 분석 후 재계획
    - 목표 달성 확인
    """

    def __init__(self):
        self.llm = get_llm_client()
        self.scratchpad = get_scratchpad()
        self.planner = get_planner()
        self.tool_executor = get_tool_executor()
        self.reflection = get_reflection()
        self.max_iterations = 10  # 최대 반복 횟수

    def execute_goal(self, goal: str, context: str = "") -> str:
        """
        사용자의 목표를 실행하는 메인 메서드
        
        Args:
            goal: 사용자의 원래 요청
            context: 추가 컨텍스트
            
        Returns:
            최종 답변
        """
        # 1. Planner로 작업 분해
        print(f"[Executor] 목표 실행 시작: {goal}")
        decomposed_tasks = self.planner.decompose_goal(goal, context)
        if not decomposed_tasks:
            return "죄송합니다, 목표를 분해할 수 없습니다."

        # 2. 작업 실행 루프
        for iteration in range(self.max_iterations):
            print(f"\n[Executor] 반복 {iteration + 1}/{self.max_iterations}")

            # 현재 진행할 작업 찾기
            current_task = self._get_next_task()
            if not current_task:
                print("[Executor] 모든 작업 완료!")
                break

            # 작업 실행
            print(f"[Executor] 작업 실행: {current_task.description}")
            self.scratchpad.set_current_task(current_task.id)
            task_result = self._execute_task(current_task)

            # 결과 확인
            if task_result["success"]:
                self.scratchpad.complete_task(current_task.id)
                print(f"[Executor] 작업 성공: {task_result['result']}")
            else:
                self.scratchpad.fail_task(current_task.id, task_result["error"])
                print(f"[Executor] 작업 실패: {task_result['error']}")
                # 실패 분석 및 재계획
                should_replan = self._reflect_and_replan(task_result["error"])
                if should_replan:
                    print("[Executor] 재계획 중...")
                    decomposed_tasks = self.planner.decompose_goal(goal, self.scratchpad.get_context())

        # 3. 최종 답변 생성
        final_answer = self._generate_final_answer(goal)
        return final_answer

    def _get_next_task(self) -> Optional[Task]:
        """다음으로 실행할 작업을 찾습니다."""
        # 대기 중인 작업 중 의존성이 충족된 것을 찾기
        pending_tasks = self.scratchpad.get_pending_tasks()
        # 우선순위 높은 순서로 정렬
        pending_tasks.sort(key=lambda t: t.priority)
        if pending_tasks:
            return pending_tasks[0]
        return None

    def _execute_task(self, task: Task) -> Dict[str, Any]:
        """
        하나의 작업을 실행합니다.
        
        Args:
            task: 실행할 작업
            
        Returns:
            성공/실패 결과
        """
        # 작업 설명에서 필요한 Tool 추론 (간단한 구현)
        # 실제로는 DecomposedTask의 required_tools 사용하거나, LLM으로 추론
        # 여기서는 간단히 텍스트 답변으로 처리하거나, Tool 호출
        try:
            # 1. 작업 설명 분석
            task_desc = task.description.lower()
            
            # 2. Tool이 필요한지 확인
            # 간단한 키워드 기반으로 Tool 선택 (실제로는 LLM으로 추론하는 게 좋음)
            tool_name = None
            tool_input = {}
            
            if "파일 읽어" in task_desc or "read_file" in task_desc:
                tool_name = "read_file"
                # TODO: 실제 파일 경로 추출 로직 필요
                tool_input = {"file_path": "temp.txt"}  # 예시
            elif "웹 검색" in task_desc or "web_search" in task_desc:
                tool_name = "web_search"
                # TODO: 실제 검색어 추출 로직 필요
                tool_input = {"query": "example"}  # 예시
            # 필요한 Tool 더 추가...

            if tool_name:
                # Tool 실행
                print(f"[Executor] Tool 호출: {tool_name}, 입력: {tool_input}")
                result = self.tool_executor.execute_tool(tool_name, tool_input)
                # Observation 기록
                self.scratchpad.add_observation(tool_name, tool_input, str(result), True)
                return {"success": True, "result": str(result)}
            else:
                # Tool이 필요 없는 경우, 간단히 텍스트로 처리
                simple_answer = f"작업 완료: {task.description}"
                self.scratchpad.add_observation("text_response", {}, simple_answer, True)
                return {"success": True, "result": simple_answer}

        except Exception as e:
            error_msg = str(e)
            self.scratchpad.add_observation("error", {}, error_msg, False)
            return {"success": False, "error": error_msg}

    def _reflect_and_replan(self, error: str) -> bool:
        """
        실패를 분석하고 재계획이 필요한지 확인합니다.
        
        Args:
            error: 실패 메시지
            
        Returns:
            재계획 필요 여부
        """
        # 현재 작업 가져오기
        current_task = self.scratchpad.current_task
        if not current_task:
            return False

        # Reflection으로 실패 분석
        analysis = self.reflection.analyze_failure(error, current_task.description)
        
        # 분석 결과 출력
        print(f"[Executor] 실패 원인: {analysis['cause']}")
        print(f"[Executor] 복구 전략: {analysis['recovery_strategy']}")
        
        # 재계획 필요 여부 반환
        return analysis["should_replan"]

    def _generate_final_answer(self, goal: str) -> str:
        """
        최종 답변을 생성합니다.
        
        Args:
            goal: 원래 목표
            
        Returns:
            최종 답변
        """
        # Scratchpad의 컨텍스트를 기반으로 LLM으로 최종 답변 생성
        context = self.scratchpad.get_context()
        
        system_prompt = """당신은 AI 어시스턴트입니다. 사용자의 요청에 대한 최종 답변을 한국어로 작성해야 합니다.
        
        규칙:
        1. Scratchpad의 내용을 기반으로 답변하세요.
        2. 간결하고 명확하게 답변하세요.
        3. "보스"라는 호칭을 사용하세요.
        """

        user_prompt = f"사용자 요청: {goal}\n\nScratchpad 내용:\n{context}\n\n최종 답변을 작성해주세요."

        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])
            return response
        except Exception as e:
            return f"죄송합니다, 최종 답변 생성 중 오류가 발생했습니다: {e}"


# Singleton instance
_executor = None


def get_executor() -> Executor:
    """Executor 싱글톤 인스턴스 반환"""
    global _executor
    if _executor is None:
        _executor = Executor()
    return _executor
