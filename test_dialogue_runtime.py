from core.conversation_context import ResolvedRequest
from core.dialogue_state import DialogueStateStore
from core.executor import Executor


class _Resolver:
    def __init__(self):
        self.requests = []

    def resolve(self, request, _history, _session_id):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ResolvedRequest(
                request, request, needs_clarification=True,
                clarification_question="어느 보고서를 수정할까요, 보스?",
            )
        return ResolvedRequest(request, request)


def _executor_for_dialogue_test(tmp_path):
    executor = Executor.__new__(Executor)
    executor.context_resolver = _Resolver()
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor._progress_callback = None
    executor.max_iterations = 0
    executor.terminal_error = None
    executor.planner = type("Planner", (), {"decompose_goal": lambda *_args: []})()
    executor.build_context = lambda *_args: ""

    def initialize(goal, session_id=None):
        executor.goal = goal
        executor.session_id = session_id or ""
        executor.current_iteration = 0
        executor.terminal_error = None

    executor.initialize = initialize
    executor.finalize = lambda: "수정을 완료했습니다, 보스."
    executor._unsupported_capability_message = lambda _goal: None
    return executor


def test_clarification_answer_resumes_original_request(tmp_path):
    executor = _executor_for_dialogue_test(tmp_path)

    first = executor.execute_turn("보고서를 수정해줘", "session-1")
    assert first.status == "awaiting_user"
    assert executor.has_pending_request("session-1")

    progress = []
    second = executor.execute_turn("어제 만든 매출 보고서", "session-1", progress_callback=progress.append)
    assert second.status == "completed"
    assert "원래 요청: 보고서를 수정해줘" in executor.context_resolver.requests[-1]
    assert "사용자가 추가로 제공한 정보: 어제 만든 매출 보고서" in executor.context_resolver.requests[-1]
    assert not executor.has_pending_request("session-1")
    assert any("이어서 진행" in message for message in progress)


def test_pending_request_can_be_cancelled(tmp_path):
    executor = _executor_for_dialogue_test(tmp_path)
    executor.execute_turn("파일을 지워줘", "session-2")

    outcome = executor.execute_turn("취소", "session-2")
    assert outcome.status == "cancelled"
    assert not executor.has_pending_request("session-2")


def test_pending_request_survives_executor_restart_and_supports_task_id(tmp_path):
    first_executor = _executor_for_dialogue_test(tmp_path)
    first = first_executor.execute_turn("발표 자료를 수정해줘", "persistent-session")
    task_id = first.response.rsplit(" ", 1)[-1]

    second_executor = _executor_for_dialogue_test(tmp_path)
    second_executor.context_resolver.requests.append("skip-first-clarification")
    listing = second_executor.execute_turn("대기 작업 목록", "persistent-session")
    assert task_id in listing.response

    outcome = second_executor.execute_turn(
        f"작업 {task_id} 재개: 3분기 발표 자료", "persistent-session"
    )
    assert outcome.status == "completed"
    assert "3분기 발표 자료" in second_executor.context_resolver.requests[-1]
    assert not second_executor.has_pending_request("persistent-session")
