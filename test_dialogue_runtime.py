from core.conversation_context import ResolvedRequest
from core.dialogue_state import DialogueStateStore
from core.executor import Executor
import threading
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from plugins.calendar import CalendarPlugin
from core.verifier import ToolVerifier


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
    registry = PluginRegistry()
    registry.register_plugin(CalendarPlugin())
    executor.intent_router = IntentRouter(registry)
    executor._progress_callback = None
    executor.current_agent_task_id = ""
    executor._task_controls = {}
    executor._control_condition = threading.Condition()
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


def test_full_task_lifecycle_status_priority_pause_resume_and_cancel(tmp_path):
    executor = _executor_for_dialogue_test(tmp_path)
    task = executor.dialogue_state.create_task("control-session", "큰 보고서 만들기")
    executor.current_agent_task_id = task.task_id
    executor._task_controls[task.task_id] = {"cancel": False, "pause": False}
    executor.dialogue_state.update_task(task.task_id, status="running")

    priority = executor.handle_control_command(
        f"작업 {task.task_id} 우선순위 7", "control-session"
    )
    assert "7" in priority.response
    assert executor.dialogue_state.get_task("control-session", task.task_id).priority == 7

    paused = executor.handle_control_command(f"작업 {task.task_id} 일시정지", "control-session")
    assert paused.status == "paused"
    resumed = executor.handle_control_command(f"작업 {task.task_id} 재개", "control-session")
    assert resumed.status == "running"

    cancelled = executor.handle_control_command(f"작업 {task.task_id} 취소", "control-session")
    assert cancelled.status == "cancelled"
    assert executor._task_controls[task.task_id]["cancel"] is True
    assert executor.dialogue_state.get_task("control-session", task.task_id).status == "cancelled"


def test_completed_task_is_persisted_with_result(tmp_path):
    executor = _executor_for_dialogue_test(tmp_path)
    executor.context_resolver.requests.append("skip-clarification")

    outcome = executor.execute_turn("간단한 상태 보고", "lifecycle-session")
    stored = executor.dialogue_state.get_task("lifecycle-session", outcome.task_id)
    assert outcome.status == "completed"
    assert stored.status == "completed"
    assert stored.result == "수정을 완료했습니다, 보스."


def test_running_task_revision_cancels_old_task_and_returns_replacement_goal(tmp_path):
    executor = _executor_for_dialogue_test(tmp_path)
    task = executor.dialogue_state.create_task("revision-session", "긴 보고서 작성")
    executor.current_agent_task_id = task.task_id
    executor._task_controls[task.task_id] = {"cancel": False, "pause": False}
    executor.dialogue_state.update_task(task.task_id, status="running")

    outcome = executor.handle_control_command(
        f"작업 {task.task_id} 수정: 표는 빼고 요약만 작성해줘", "revision-session"
    )
    assert outcome.status == "cancelled"
    assert "표는 빼고 요약만" in outcome.next_goal
    assert executor._task_controls[task.task_id]["cancel"] is True


def test_interrupted_task_resume_returns_automatic_reexecution_goal(tmp_path):
    executor = _executor_for_dialogue_test(tmp_path)
    task = executor.dialogue_state.create_task("restart-session", "중단된 분석 계속하기")
    executor.dialogue_state.update_task(task.task_id, status="interrupted")

    outcome = executor.handle_control_command(f"작업 {task.task_id} 재개", "restart-session")
    assert outcome.status == "interrupted"
    assert outcome.next_goal == "중단된 분석 계속하기"


def test_queued_tasks_are_ordered_by_priority_and_survive_restart(tmp_path):
    first = _executor_for_dialogue_test(tmp_path)
    low = first.enqueue_goal("나중 작업", "queue-session", priority=1)
    high = first.enqueue_goal("먼저 작업", "queue-session", priority=9)

    second = _executor_for_dialogue_test(tmp_path)
    queued = second.dialogue_state.list_tasks("queue-session", include_finished=False)
    assert [task.task_id for task in queued] == [high.task_id, low.task_id]


def test_calendar_creation_waits_for_times_before_planner_or_llm(tmp_path):
    executor = _executor_for_dialogue_test(tmp_path)

    outcome = executor.execute_turn(
        "바탕화면에 123이라는 이름으로 캘린더 파일 하나 생성해줘", "calendar-session"
    )
    assert outcome.status == "awaiting_user"
    assert "언제 시작" in outcome.response
    assert executor.context_resolver.requests == []


def test_calendar_slots_accumulate_dates_and_execute_without_repeating_question(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    executor = _executor_for_dialogue_test(tmp_path)
    target = tmp_path / "123.ics"
    registry = executor.intent_router.registry
    executor.tool_executor = type(
        "Tools", (), {"execute_tool": lambda _self, name, data: registry.execute_tool(name, data)}
    )()
    executor.verifier = ToolVerifier()

    first = executor.execute_turn(
        f"{target}에 123이라는 이름으로 캘린더 파일을 생성해줘", "calendar-accumulate"
    )
    second = executor.execute_turn("26년 7월22일에 시작해서 23일에 끝나", "calendar-accumulate")

    assert first.status == "awaiting_user"
    assert second.status == "completed"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "DTSTART;VALUE=DATE:20260722" in content
    assert "DTEND;VALUE=DATE:20260723" in content


def test_legacy_pending_calendar_task_is_migrated_to_intent_slots(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    executor = _executor_for_dialogue_test(tmp_path)
    target = tmp_path / "legacy.ics"
    original = f"{target}에 legacy라는 이름으로 캘린더 파일을 생성해줘"
    task = executor.dialogue_state.create_task("legacy-session", original)
    executor.dialogue_state.create(
        "legacy-session", original, "캘린더 일정은 언제 시작해서 언제 끝나나요, 보스?", [], task.task_id
    )
    executor.dialogue_state.update_task(task.task_id, status="awaiting_user")
    registry = executor.intent_router.registry
    executor.tool_executor = type(
        "Tools", (), {"execute_tool": lambda _self, name, data: registry.execute_tool(name, data)}
    )()
    executor.verifier = ToolVerifier()

    outcome = executor.execute_turn("26년 7월22일에 시작해서 23일에 끝나", "legacy-session")

    assert outcome.status == "completed"
    assert target.exists()
