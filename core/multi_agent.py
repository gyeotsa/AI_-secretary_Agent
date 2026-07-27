import json
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime

from core.scratchpad import get_scratchpad, Scratchpad
from core.context import get_context_manager
from core.llm import get_llm_client
from core.tools import get_tool_executor, get_tool_names
from core.tool_result import ToolRunResult
from core.memory import build_memory_context


@dataclass
class AgentTask:
    """에이전트 작업"""
    id: str
    description: str
    task_type: str  # "planning", "execution", "reflection"
    status: str = "pending"  # "pending", "in_progress", "completed", "failed"
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: str = None
    completed_at: Optional[str] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()


class MultiAgentOrchestrator:
    """멀티 에이전트 오케스트레이터"""
    
    def __init__(self):
        self.scratchpad = get_scratchpad()
        self.context_manager = get_context_manager()
        self.tool_executor = get_tool_executor()
        self.llm = get_llm_client()
        self.tasks: List[AgentTask] = []
        
    def create_task(self, description: str, task_type: str) -> AgentTask:
        """작업 생성"""
        task = AgentTask(
            id=f"task_{len(self.tasks) + 1}",
            description=description,
            task_type=task_type
        )
        self.tasks.append(task)
        return task
        
    def execute_planning_agent(self, user_query: str) -> List[Dict]:
        """플래너 에이전트 실행: 사용자 쿼리를 작업으로 분해"""
        task = self.create_task(f"사용자 쿼리 계획: {user_query}", "planning")
        task.status = "in_progress"
        
        try:
            # 컨텍스트 빌드
            context = self._build_context(user_query)
            
            # 시스템 프롬프트
            system_prompt = """당신은 자비스 AI의 플래너 에이전트입니다.
사용자의 요청을 분석하여 실행 가능한 작업 단계로 분해해야 합니다.

각 작업은 다음 형식으로 반환해야 합니다:
[
  {
    "id": "task_1",
    "description": "작업 설명",
    "tool": "사용할 도구 이름 (필요한 경우)",
    "tool_input": "도구 입력 매개변수 (JSON)"
  }
]

도구를 사용하지 않는 경우 tool 필드는 생략할 수 있습니다.
"""
            
            # 플래너 호출
            prompt = f"""{system_prompt}

{context}

사용자 요청: {user_query}

작업 계획:"""
            
            response = self.llm.chat([{"role": "user", "content": prompt}])
            
            # 응답 파싱
            try:
                # JSON 부분 추출
                import re
                json_match = re.search(r'\[.*\]', response, re.DOTALL)
                if json_match:
                    plan = json.loads(json_match.group())
                else:
                    # 단순 응답인 경우 단일 작업으로
                    plan = [{
                        "id": "task_1",
                        "description": response,
                        "tool": None
                    }]
                
                # 스크래치패드에 계획 저장
                for i, step in enumerate(plan):
                    self.scratchpad.add_task(
                        task_description=step["description"],
                        priority=i + 1
                    )
                
                task.status = "completed"
                task.result = json.dumps(plan, ensure_ascii=False)
                return plan
                
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                raise
                
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            raise
        finally:
            task.completed_at = datetime.now().isoformat()
            
    def execute_executor_agent(self, plan: List[Dict]) -> str:
        """실행자 에이전트 실행: 계획된 작업 실행"""
        task = self.create_task("계획 실행", "execution")
        task.status = "in_progress"
        
        results = []
        
        try:
            for step in plan:
                step_task = self.create_task(step["description"], "execution")
                step_task.status = "in_progress"
                
                try:
                    if step.get("tool"):
                        # 도구 실행
                        tool_name = step["tool"]
                        if tool_name not in get_tool_names():
                            raise ValueError(f"등록되지 않은 도구입니다: {tool_name}")
                        raw_input = step.get("tool_input", {})
                        tool_input = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
                        if not isinstance(tool_input, dict):
                            raise ValueError("tool_input은 JSON 객체여야 합니다.")
                        result = self.tool_executor.execute_tool(tool_name, tool_input)
                        if isinstance(result, ToolRunResult):
                            if not result.succeeded:
                                raise RuntimeError(result.raw_output)
                            result_text = result.raw_output
                        else:
                            raise TypeError(
                                f"Tool Runtime 계약 위반: {tool_name}이 "
                                "ToolRunResult가 아닌 값을 반환했습니다."
                            )
                        results.append(f"단계 {step['id']}: {result_text}")
                        
                        # 스크래치패드에 관찰 기록
                        self.scratchpad.add_observation(
                            tool_name=tool_name,
                            input_data=tool_input,
                            result=result_text,
                            success=True
                        )
                    else:
                        # 직접 응답
                        results.append(f"단계 {step['id']}: {step['description']}")
                        
                    step_task.status = "completed"
                    step_task.result = results[-1]
                    
                except Exception as e:
                    step_task.status = "failed"
                    step_task.error = str(e)
                    results.append(f"단계 {step['id']} 실패: {str(e)}")
                    self.scratchpad.fail_task(
                        task_id=step_task.id,
                        reason=str(e)
                    )
                finally:
                    step_task.completed_at = datetime.now().isoformat()
                    
            # 최종 결과 생성
            final_result = "\n".join(results)
            task.status = "completed"
            task.result = final_result
            return final_result
            
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            raise
        finally:
            task.completed_at = datetime.now().isoformat()
            
    def execute_reflection_agent(self, result: str) -> str:
        """반성 에이전트 실행: 결과 분석 및 개선"""
        task = self.create_task("결과 반성", "reflection")
        task.status = "in_progress"
        
        try:
            context = self._build_context("결과 분석")
            scratchpad_context = self.scratchpad.get_context()
            
            system_prompt = """당신은 자비스 AI의 반성 에이전트입니다.
실행 결과를 분석하고 다음을 확인해야 합니다:
1. 목표가 달성되었는가?
2. 개선할 점이 있는가?
3. 다음에 유사한 작업을 할 때 참고할 점은 무엇인가?

답변은 한국어로 작성하세요.
"""
            
            prompt = f"""{system_prompt}

{context}

스크래치패드 상태:
{scratchpad_context}

실행 결과:
{result}

분석 및 개선 제안:"""
            
            reflection = self.llm.chat([{"role": "user", "content": prompt}])
            
            # 스크래치패드에 반성 기록
            self.scratchpad.add_observation(
                tool_name="reflection_agent",
                input_data={},
                result=reflection,
                success=True
            )
            
            task.status = "completed"
            task.result = reflection
            return reflection
            
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            raise
        finally:
            task.completed_at = datetime.now().isoformat()
            
    def execute_full_pipeline(self, user_query: str) -> str:
        """전체 파이프라인 실행: 계획 → 실행 → 반성"""
        # 1. 계획
        plan = self.execute_planning_agent(user_query)
        
        # 2. 실행
        result = self.execute_executor_agent(plan)
        
        # 3. 반성
        reflection = self.execute_reflection_agent(result)
        
        # 최종 답변 생성
        final_prompt = f"""사용자 요청: {user_query}

실행 결과:
{result}

반성:
{reflection}

최종 답변을 한국어로 작성하세요."""
        
        final_answer = self.llm.chat([{"role": "user", "content": final_prompt}])
        return final_answer
        
    def _build_context(self, query: str) -> str:
        """컨텍스트 빌드"""
        parts = []
        
        # 1. 메모리 컨텍스트
        try:
            memory_context = build_memory_context(
                session_id="multi_agent",
                max_episodes=10,
                include_semantic=True
            )
            if memory_context:
                parts.append(memory_context)
        except Exception as e:
            pass
            
        # 2. 컨텍스트 매니저
        try:
            context_info = self.context_manager.get_full_context(query, "multi_agent")
            if context_info:
                parts.append(f"\n### 시스템 상태 ###\n{context_info}")
        except Exception as e:
            pass
            
        return "\n".join(parts)


# 싱글톤 인스턴스
_multi_agent_orchestrator = None


def get_multi_agent_orchestrator() -> MultiAgentOrchestrator:
    global _multi_agent_orchestrator
    if _multi_agent_orchestrator is None:
        _multi_agent_orchestrator = MultiAgentOrchestrator()
    return _multi_agent_orchestrator
