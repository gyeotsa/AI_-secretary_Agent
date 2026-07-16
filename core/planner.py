from typing import List, Dict, Any
from dataclasses import dataclass, field
import json
from core.llm import get_llm_client
from core.scratchpad import get_scratchpad, Scratchpad


@dataclass
class DecomposedTask:
    """분해된 작업 단위"""
    id: str
    description: str
    required_tools: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    priority: int = 0
    estimated_steps: int = 1


class Planner:
    """
    사용자의 요청을 작은 작업으로 분해하는 Planner 클래스
    - LLM을 사용해 목표를 분해
    - Scratchpad에 작업 저장
    """

    def __init__(self):
        self.llm = get_llm_client()
        self.scratchpad = get_scratchpad()

    def decompose_goal(self, goal: str, context: str = "") -> List[DecomposedTask]:
        """
        사용자의 목표를 작업으로 분해합니다.
        
        Args:
            goal: 사용자의 원래 요청
            context: 추가 컨텍스트 (대화 기록, RAG 결과 등)
            
        Returns:
            분해된 작업 리스트
        """
        # Scratchpad 초기화 및 목표 설정
        self.scratchpad.reset()
        self.scratchpad.set_goal(goal)

        # LLM에 전달할 시스템 프롬프트
        system_prompt = """당신은 AI 어시스턴트의 Planner입니다. 사용자의 요청을 작고 실행 가능한 작업으로 분해해야 합니다.

규칙:
1. 각 작업은 하나의 Tool만 사용하거나, 간단한 텍스트 답변으로 완료될 수 있어야 합니다.
2. 작업은 순서대로 실행될 수 있도록 의존성을 가져야 합니다. (예: "엑셀 생성" → "데이터 입력" → "저장")
3. 각 작업에 필요한 Tool을 명시하세요. (없으면 빈 리스트)
4. 작업은 한국어로 작성하세요.

응답 형식 (JSON만 반환하세요!):
{
    "tasks": [
        {
            "id": "task_1",
            "description": "첫 번째 작업 설명",
            "required_tools": ["tool_name_1", "tool_name_2"],
            "dependencies": [],
            "priority": 1,
            "estimated_steps": 1
        },
        {
            "id": "task_2",
            "description": "두 번째 작업 설명",
            "required_tools": ["tool_name_3"],
            "dependencies": ["task_1"],
            "priority": 2,
            "estimated_steps": 1
        }
    ]
}

사용 가능한 Tool 목록:
- read_file: 파일 읽기
- write_file: 파일 쓰기
- list_directory: 폴더 내용 보기
- run_command: 명령 실행
- web_search: 웹 검색
- set_profile: 사용자 프로필 설정
- get_profile: 사용자 프로필 가져오기
- set_preference: 사용자 선호 설정
- speak_text: 텍스트 음성으로 읽기
- listen: 음성 듣기
- add_document: RAG 문서 추가
- search_docs: RAG 문서 검색
- list_documents: RAG 문서 목록 보기
- add_schedule_job: 스케줄 작업 추가
- list_schedule_jobs: 스케줄 작업 목록 보기
- delete_schedule_job: 스케줄 작업 삭제
- start_scheduler: 스케줄러 시작
- stop_scheduler: 스케줄러 중지
- start_wakeword_detection: 웨이크워드 감지 시작
- stop_wakeword_detection: 웨이크워드 감지 중지
- start_clap_detection: 박수 감지 시작
- stop_clap_detection: 박수 감지 중지
"""

        # 사용자 프롬프트
        user_prompt = f"사용자 요청: {goal}\n\n"
        if context:
            user_prompt += f"추가 컨텍스트:\n{context}\n\n"
        user_prompt += "위 요청을 작업으로 분해해주세요."

        # LLM 호출
        try:
            response = self.llm.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])

            # JSON 파싱
            # 응답에서 ```json ... ``` 부분 추출 (있으면)
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                response = response.split("```")[1].strip()

            result = json.loads(response)
            tasks_data = result.get("tasks", [])

            # DecomposedTask로 변환하고 Scratchpad에 저장
            decomposed_tasks = []
            for task_data in tasks_data:
                task = DecomposedTask(
                    id=task_data["id"],
                    description=task_data["description"],
                    required_tools=task_data.get("required_tools", []),
                    dependencies=task_data.get("dependencies", []),
                    priority=task_data.get("priority", 0),
                    estimated_steps=task_data.get("estimated_steps", 1)
                )
                decomposed_tasks.append(task)
                # Scratchpad에 Task로 추가 (기존 Task 클래스 사용)
                self.scratchpad.add_task(task.description, task.priority)

            print(f"[Planner] 목표를 {len(decomposed_tasks)}개의 작업으로 분해했습니다!")
            return decomposed_tasks

        except Exception as e:
            print(f"[Planner] 작업 분해 오류: {e}")
            import traceback
            traceback.print_exc()
            # 오류시 기본 작업 하나 반환
            fallback_task = DecomposedTask(
                id="task_1",
                description=f"사용자 요청 처리: {goal}",
                required_tools=[],
                dependencies=[],
                priority=1,
                estimated_steps=1
            )
            self.scratchpad.add_task(fallback_task.description, 1)
            return [fallback_task]


# Singleton instance
_planner = None


def get_planner() -> Planner:
    """Planner 싱글톤 인스턴스 반환"""
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner
