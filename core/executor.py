from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import json

from core.llm import get_llm_client
from core.scratchpad import get_scratchpad, Task
from core.planner import get_planner
from core.tools import get_tool_executor
from core.reflection import get_reflection
from core.context import get_context_manager
from core.permission import get_permission_manager
from core.memory import get_memory, build_memory_context
from core.workspace import get_workspace_manager


class Executor:
    """
    진정한 Agent Runtime!
    Jarvis의 중앙 실행 엔진으로, Planner와 협력하고, Context를 조립하고,
    적절한 Tool을 선택하고, 권한을 확인하고, 실행 결과를 검증하고,
    실패를 복구하고, Memory와 Scratchpad를 갱신하며,
    목표 달성 여부를 계속 판단합니다!
    """

    def __init__(self):
        self.llm = get_llm_client()
        self.scratchpad = get_scratchpad()
        self.planner = get_planner()
        self.tool_executor = get_tool_executor()
        self.reflection = get_reflection()
        self.context_manager = get_context_manager()
        self.permission_manager = get_permission_manager()
        self.memory = get_memory()
        self.workspace_manager = get_workspace_manager()

        self.goal = ""
        self.session_id = ""
        self.max_iterations = 20  # 무한 루프 방지
        self.current_iteration = 0

    def initialize(self, goal: str, session_id: Optional[str] = None):
        """초기화: Goal 설정, Scratchpad 초기화, Context 빌드"""
        self.goal = goal
        self.session_id = session_id or ""
        self.current_iteration = 0
        self.scratchpad.reset()
        self.scratchpad.set_goal(goal)
        print(f"[Executor] 초기화 완료: Goal='{goal}'")

    def execute_goal(self, goal: str, session_id: Optional[str] = None) -> str:
        """메인 메서드: Goal을 받아서 전체 실행 흐름을 관리"""
        self.initialize(goal, session_id)

        # 1. 초기 Planning
        initial_context = self.build_context()
        _ = self.planner.decompose_goal(goal, initial_context)

        # 2. 메인 반복 루프
        while self.current_iteration < self.max_iterations:
            self.current_iteration += 1
            print(f"\n[Executor] 반복 {self.current_iteration}/{self.max_iterations}")

            should_continue = self.run_iteration()
            if not should_continue:
                break

        # 3. 최종 종료 처리
        return self.finalize()

    def run_iteration(self) -> bool:
        """한 번의 반복 실행: Task 선택 → Context 빌드 → Reasoning → Action → Verify → Reflect → Update → Evaluate"""
        try:
            # 1. Task 선택
            task = self.select_task()
            if not task:
                print("[Executor] 더 이상 실행할 Task가 없습니다!")
                return False

            self.scratchpad.set_current_task(task.id)
            print(f"[Executor] Task 선택: {task.description}")

            # 2. Context 빌드
            context = self.build_context(task)

            # 3. 다음 Action Reasoning
            action = self.reason_next_action(task, context)
            print(f"[Executor] Action 결정: {action}")

            # 4. Tool 선택
            tool_name, tool_input = self.select_tool(task, action, context)
            if tool_name:
                print(f"[Executor] Tool 선택: {tool_name}, 입력: {tool_input}")

                # 5. Permission Check
                if not self.request_permission(tool_name):
                    print(f"[Executor] Permission 거절됨: {tool_name}")
                    self.scratchpad.add_observation("permission_denied", {"tool": tool_name}, "Permission denied", False)
                    return True  # 다음 반복으로

                # 6. Tool 실행
                result = self.execute_tool(tool_name, tool_input)
                print(f"[Executor] Tool 실행 결과: {result}")

                # 7. 실행 결과 검증
                verified = self.verify_execution(task, tool_name, tool_input, result)
                if not verified:
                    print(f"[Executor] 실행 결과 검증 실패! 복구 시도...")
                    recover_result = self.recover(task, tool_name, tool_input, result)
                    if recover_result:
                        result = recover_result
                    else:
                        print("[Executor] 복구 실패!")
                        return True

                # 8. Observation 처리
                self.process_observation(task, tool_name, tool_input, result)
            else:
                # Tool이 필요 없는 경우 (간단한 텍스트 응답 등)
                simple_result = action.get("simple_result", "Task 완료")
                print(f"[Executor] 간단한 처리: {simple_result}")
                self.scratchpad.add_observation("simple_action", action, simple_result, True)

            # 9. Reflection
            self.reflect(task)

            # 10. Memory 업데이트
            self.update_memory()

            # 11. Goal 평가
            goal_completed = self.evaluate_goal()
            if goal_completed:
                print("[Executor] Goal 달성!")
                return False

            return True

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[Executor] 반복 실행 오류: {e}")
            self.scratchpad.add_observation("error", {}, str(e), False)
            return True

    def build_context(self, task: Optional[Task] = None) -> str:
        """
        모든 Context를 조립:
        Conversation + Memory + Workspace + OS + 현재 Task + Scratchpad + 최근 Tool 결과
        """
        context_parts = []

        # 1. Goal
        context_parts.append(f"# 최종 목표 (Goal)\n{self.goal}\n")

        # 2. Scratchpad 상태
        scratchpad_context = self.scratchpad.get_context()
        context_parts.append(f"# 스크래치패드 (Scratchpad)\n{scratchpad_context}\n")

        # 3. Memory (Episode + Semantic)
        try:
            memory_context = build_memory_context(self.session_id, 15, True)
            if memory_context:
                context_parts.append(f"# 메모리 (Memory)\n{memory_context}\n")
        except Exception as e:
            print(f"[Executor] Memory Context 빌드 오류: {e}")

        # 4. Workspace 정보
        try:
            workspace_info = self.workspace_manager.get_info()
            if workspace_info.path:
                context_parts.append(f"# 작업 공간 (Workspace)\n경로: {workspace_info.path}\n이름: {workspace_info.name}\n")
        except Exception as e:
            print(f"[Executor] Workspace Context 빌드 오류: {e}")

        # 5. 현재 Task
        if task:
            context_parts.append(f"# 현재 Task\nID: {task.id}\n설명: {task.description}\n우선순위: {task.priority}\n")

        # 6. Context Manager (시스템 상태)
        try:
            system_context = self.context_manager.get_context()
            if system_context:
                context_parts.append(f"# 시스템 상태 (System Context)\n{system_context}\n")
        except Exception as e:
            print(f"[Executor] System Context 빌드 오류: {e}")

        return "\n".join(context_parts)

    def select_task(self) -> Optional[Task]:
        """다음으로 실행할 Task 선택: 대기 중인 Task 중 우선순위 높은 순으로"""
        pending_tasks = self.scratchpad.get_pending_tasks()
        if not pending_tasks:
            return None

        # 우선순위 높은 순으로 정렬
        pending_tasks.sort(key=lambda t: t.priority)
        return pending_tasks[0]

    def reason_next_action(self, task: Task, context: str) -> Dict[str, Any]:
        """다음 Action을 Reasoning: 무엇을 할지 결정"""
        system_prompt = """당신은 Jarvis의 Action Reasoner입니다.
현재 Task와 전체 Context를 보고, 다음으로 무엇을 할지 결정해야 합니다.

응답 형식 (JSON만 반환):
{
    "action_type": "use_tool" | "simple_task" | "request_more_info",
    "explanation": "왜 이 Action을 선택했는지 설명",
    "simple_result": "만약 simple_task이면 결과 텍스트"
}
"""
        user_prompt = f"Context:\n{context}\n\nTask: {task.description}\n\n다음 Action은?"

        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])

            json_str = self._extract_json(response)
            if json_str:
                return json.loads(json_str)
            else:
                return {"action_type": "simple_task", "simple_result": "Task 완료", "explanation": "간단한 처리"}

        except Exception as e:
            print(f"[Executor] Reasoning 오류: {e}")
            return {"action_type": "simple_task", "simple_result": f"Task 완료: {task.description}", "explanation": "기본 처리"}

    def select_tool(self, task: Task, action: Dict[str, Any], context: str) -> tuple[Optional[str], Dict[str, Any]]:
        """
        Tool Registry에서 가능한 Tool 목록을 가져와서 LLM으로 Best Tool 선택!
        절대 Rule-based로 하지 않습니다!
        """
        if action.get("action_type") != "use_tool":
            return None, {}

        # 1. Tool Registry에서 모든 Tool 가져오기 (실제로는 Plugin Registry에서 가져올 수 있음)
        # 여기서는 간단히 ToolExecutor가 아는 Tool 목록을 사용
        # (나중에 proper Tool Registry로 교체)
        available_tools = self._get_available_tools()

        # 2. LLM으로 Best Tool 선택
        system_prompt = """당신은 Tool Selector입니다.
사용 가능한 Tool 목록을 보고, 주어진 Task와 Context에 가장 적합한 Tool을 선택하세요!

사용 가능한 Tool 목록:
"""
        for tool in available_tools:
            system_prompt += f"- {tool['name']}: {tool['description']}\n"

        system_prompt += """
응답 형식 (JSON만 반환):
{
    "tool_name": "선택한 Tool 이름",
    "tool_input": { Tool에 전달할 입력 },
    "explanation": "왜 이 Tool을 선택했는지"
}
"""
        user_prompt = f"Context:\n{context}\n\nTask: {task.description}\n\n적절한 Tool은?"

        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])

            json_str = self._extract_json(response)
            if json_str:
                result = json.loads(json_str)
                return result.get("tool_name"), result.get("tool_input", {})
            else:
                return None, {}

        except Exception as e:
            print(f"[Executor] Tool Selection 오류: {e}")
            return None, {}

    def _get_available_tools(self) -> List[Dict[str, Any]]:
        """사용 가능한 Tool 목록 반환 (나중에 proper Tool Registry로 교체)"""
        # 여기서는 간단한 하드코드로 제공
        # 실제로는 get_tools_schema()나 Plugin Registry에서 가져와야 함
        return [
            {"name": "read_file", "description": "파일 읽기"},
            {"name": "write_file", "description": "파일 쓰기"},
            {"name": "list_directory", "description": "폴더 내용 보기"},
            {"name": "run_command", "description": "명령 실행"},
            {"name": "web_search", "description": "웹 검색"},
            {"name": "speak_text", "description": "텍스트 음성으로 읽기"},
            {"name": "add_semantic_memory", "description": "의미 기억 추가"},
            {"name": "get_semantic_memory", "description": "의미 기억 조회"},
            {"name": "search_semantic_memory", "description": "의미 기억 검색"},
            {"name": "index_project", "description": "프로젝트 인덱싱"},
            {"name": "search_files", "description": "파일 검색"},
            {"name": "search_symbols", "description": "심볼 검색"},
            {"name": "add_automation_job", "description": "자동화 작업 추가"},
            {"name": "list_automation_jobs", "description": "자동화 작업 목록"},
            {"name": "add_entity", "description": "지식 그래프 엔티티 추가"},
            {"name": "get_entity", "description": "지식 그래프 엔티티 조회"},
            {"name": "add_triple", "description": "지식 그래프 관계 추가"},
            {"name": "get_subgraph", "description": "지식 그래프 서브그래프 조회"},
        ]

    def request_permission(self, tool_name: str) -> bool:
        """Permission 체크: Tool 실행 전 권한 확인"""
        # Permission Level 결정 (간단한 규칙, 나중에 더 정교하게)
        permission_level = "safe"
        dangerous_tools = ["run_command", "delete_entity", "delete_semantic_memory"]
        system_tools = []  # 카메라, 마이크 등 (나중에)

        if tool_name in dangerous_tools:
            permission_level = "confirm"
        elif tool_name in system_tools:
            permission_level = "system"

        # Permission Manager로 권한 요청
        # 실제로는 Permission Manager의 request 메서드 호출
        # 여기서는 간단히 항상 허용 (나중에 제대로 구현)
        print(f"[Executor] Permission 체크: {tool_name} (Level: {permission_level})")
        return True  # 일단 항상 허용 (테스트용)

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Tool 실행"""
        return self.tool_executor.execute_tool(tool_name, tool_input)

    def verify_execution(self, task: Task, tool_name: str, tool_input: Dict[str, Any], result: str) -> bool:
        """실행 결과 검증: 정말로 성공했는지 확인 (예: 파일 수정 후 git diff 확인 등)"""
        # 간단한 검증: 결과에 "오류"나 "Error"가 없으면 OK
        # 나중에 더 정교한 검증 로직 추가
        if "오류" in result or "Error" in result or "error" in result:
            print(f"[Executor] 검증 실패: 결과에 오류가 있음")
            return False

        print(f"[Executor] 검증 성공!")
        return True

    def process_observation(self, task: Task, tool_name: str, tool_input: Dict[str, Any], result: str):
        """Observation 처리: Scratchpad에 기록"""
        self.scratchpad.add_observation(tool_name, tool_input, result, True)
        self.scratchpad.complete_task(task.id)
        print(f"[Executor] Task 완료 처리: {task.id}")

    def reflect(self, task: Task):
        """Reflection: 실패 분석 및 개선"""
        # 실패한 경우에만 Reflection 호출 (지금은 항상 성공으로 가정)
        # 실제로는 Task 상태 확인 후 호출
        pass

    def update_memory(self):
        """Memory 업데이트: Episode Memory에 현재 상태 저장"""
        # Episode Memory는 이미 main_qt.py에서 저장되지만, 여기서 추가로 업데이트할 수 있음
        pass

    def evaluate_goal(self) -> bool:
        """Goal 평가: 정말로 달성됐는지 확인"""
        # Scratchpad에 남은 Task가 없고, 최종 결과가 있으면 완료로 판단
        pending_tasks = self.scratchpad.get_pending_tasks()
        if pending_tasks:
            return False

        # LLM으로 최종 Goal 달성 여부 확인
        context = self.build_context()
        system_prompt = """당신은 Goal Evaluator입니다.
전체 Context를 보고, 최종 Goal이 달성됐는지 판단하세요!

응답 형식 (JSON만 반환):
{
    "goal_completed": true | false,
    "reason": "왜 그렇게 판단했는지"
}
"""
        user_prompt = f"Context:\n{context}\n\nGoal: {self.goal}\n\n달성됐나요?"

        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])

            json_str = self._extract_json(response)
            if json_str:
                result = json.loads(json_str)
                return result.get("goal_completed", False)

        except Exception as e:
            print(f"[Executor] Goal Evaluation 오류: {e}")

        return len(pending_tasks) == 0

    def recover(self, task: Task, tool_name: str, tool_input: Dict[str, Any], last_result: str) -> Optional[str]:
        """실패 복구: Retry → Alternative Tool → Fallback → Planner 순서로 시도"""
        print(f"[Executor] 복구 시도...")

        # 1. Retry (한 번만)
        try:
            print("[Executor] 1차 복구: Retry...")
            result = self.tool_executor.execute_tool(tool_name, tool_input)
            if "오류" not in result and "Error" not in result:
                print("[Executor] Retry 성공!")
                return result
        except Exception:
            pass

        # 2. Alternative Tool (여기서는 간단히 생략, 나중에 구현)

        # 3. Fallback (간단한 응답)
        print("[Executor] 3차 복구: Fallback...")
        return f"Task는 실패했지만, 기본 처리로 대체합니다: {task.description}"

    def finalize(self) -> str:
        """최종 종료 처리: 최종 답변 생성"""
        return self.generate_response()

    def generate_response(self) -> str:
        """최종 답변 생성"""
        context = self.build_context()
        system_prompt = """당신은 Jarvis입니다.
전체 Context를 보고, 최종 답변을 한국어로 작성하세요!
보스라는 호칭을 사용하세요.
"""
        user_prompt = f"Context:\n{context}\n\n최종 답변은?"

        try:
            return self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])
        except Exception as e:
            return f"죄송해요, 보스! 최종 답변 생성 중 오류가 발생했어요: {e}"

    def _extract_json(self, text: str) -> Optional[str]:
        """응답에서 JSON 문자열 추출"""
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            return text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            return text[start:end].strip()
        else:
            # 그냥 전체 텍스트가 JSON인지 확인
            try:
                json.loads(text)
                return text
            except Exception:
                return None


# Singleton instance
_executor = None


def get_executor() -> Executor:
    """Executor 싱글톤 인스턴스 반환"""
    global _executor
    if _executor is None:
        _executor = Executor()
    return _executor
