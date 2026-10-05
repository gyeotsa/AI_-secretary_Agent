import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest

from core.continuity import ContinuityService, is_continuity_query
from core.continuity_store import ContinuityStore
from core.dialogue_state import DialogueStateStore
from core.memory import ConversationMemory
from core.plan_runtime import PlanDAG, PlanStep, PlanExecutionStore, StepStatus
from core.proactive import ProactiveNotificationPolicy
from core.proactive_runtime import ProactiveStore, InterruptionContextManager
from core.runtime.action_journal import ActionJournal
from core.tool_result import ToolRunResult, Evidence


@pytest.fixture(autouse=True)
def isolated_foreground(monkeypatch):
    # Real foreground apps must not change an offline notification oracle.
    monkeypatch.setattr(InterruptionContextManager, "_detect_fullscreen", staticmethod(lambda: False))


def setup_service(tmp_path, policy=None):
    memory = ConversationMemory(str(tmp_path / "messages.db"))
    tasks = DialogueStateStore(str(tmp_path / "tasks.db"))
    plans = PlanExecutionStore(str(tmp_path / "plans.db"))
    journal = ActionJournal(str(tmp_path / "actions.db"))
    store = ContinuityStore(tmp_path / "continuity.db")
    service = ContinuityService(store, memory_path=memory.episode_manager.db_path,
        dialogue_path=tasks.db_path, plan_path=plans.db_path, journal_path=journal.db_path,
        notification_policy=policy)
    return service, memory, tasks, plans, journal


def test_internal_sync_keeps_session_and_verified_plan_evidence(tmp_path):
    service, memory, tasks, plans, journal = setup_service(tmp_path)
    memory.save_message("s1", "user", "보고서 작성해줘")
    memory.save_message("s1", "assistant", "API_KEY=do-not-copy-this-secret")
    task = tasks.create_task("s1", "보고서", workspace_path="C:/projects/report")
    tasks.transition_task(task.task_id, "running")
    step = PlanStep("draft", "초안 작성", status=StepStatus.COMPLETED)
    plan = PlanDAG("보고서", [step, PlanStep("review", "검토", dependencies=["draft"])])
    plans.save_plan(plan)
    plans.record_attempt(plan.plan_id, step, "retry", ToolRunResult.successful(
        tool_name="write_file", raw_output="saved", evidence=[Evidence("file", "saved", {"path": "draft.md"})]), "")
    tasks.update_task(task.task_id, plan=plan.to_dict()["steps"], plan_id=plan.plan_id)
    journal.record("tool_execution", "초안 저장", "tool_executor", data={"session_id": "s1"})
    report = service.sync()
    assert report["status"] == "ok", report
    assert report["events"] == 4
    board = service.dashboard()
    assert board["next_actions"][0]["description"] == "검토"
    assert board["recent"][0]["verification"] == "verified"
    session = service.list_sessions()[0]
    detail = service.session_detail(session["session_key"])
    assert len(detail["messages"]) == 2
    assert "do-not-copy-this-secret" not in json.dumps(detail)
    assert any(e["kind"] == "tool" for e in detail["events"])
    assert service.sync()["events"] == 0
    assert tasks.get_task("s1", task.task_id).status == "running"
    assert "검토" in service.answer_now()


def test_awaiting_work_is_blocked(tmp_path):
    service, memory, tasks, plans, _ = setup_service(tmp_path)
    task = tasks.create_task("s", "승인 필요")
    tasks.transition_task(task.task_id, "running")
    tasks.transition_task(task.task_id, "awaiting_approval")
    service.sync()
    board = service.dashboard()
    assert not board["next_actions"]
    assert len(board["blocked"]) == 1


def test_waiting_task_blocks_existing_pending_plan(tmp_path):
    service, _, tasks, *_ = setup_service(tmp_path)
    task = tasks.create_task("s", "승인 필요")
    tasks.update_task(task.task_id, plan=[{"id": "one", "description": "배포", "status": "pending"}])
    tasks.transition_task(task.task_id, "running")
    tasks.transition_task(task.task_id, "awaiting_user", pending_question="대상을 선택해주세요")
    service.sync()
    board = service.dashboard()
    assert not board["next_actions"]
    assert board["blocked"][0]["status"] == "blocked"
    assert "대상을 선택해주세요" in json.dumps(board["blocked"], ensure_ascii=False)


def test_cursor_integrity_metadata_is_not_redacted_but_pending_text_is(tmp_path):
    service, *_ = setup_service(tmp_path)
    source = service.sources()[0]
    digest = "a" * 20 + "01012345678" + "b" * 33
    cursor = {"prefix_hash": digest, "tail_hash": digest, "tasks": {"task": digest},
              "state": {"session_id": "session", "recent_messages": [[digest, "event"]],
                        "pending_calls": {"call": {"name": "update_plan", "input": {"explanation": "password=private-value"}}}}}
    service._ingest(source, "synthetic", [], cursor)
    saved = service.store.get_cursor(source["id"], "synthetic")
    assert saved["prefix_hash"] == saved["tail_hash"] == digest
    assert saved["tasks"] == cursor["tasks"]
    assert saved["state"]["recent_messages"] == cursor["state"]["recent_messages"]
    assert "private-value" not in json.dumps(saved)


def test_manual_plan_corrections_survive_restart_and_reimport(tmp_path):
    service, memory, *_ = setup_service(tmp_path)
    memory.save_message("chat", "user", "주간 보고서 계획")
    service.sync()
    session = service.list_sessions()[0]
    detail = service.create_manual_plan(session["session_key"], "주간 보고서", [
        {"description": "자료 수집"}, {"description": "초안 작성"}])
    step = detail["steps"][0]
    service.update_step(step["step_id"], status="done", priority=3)
    service.sync()
    reopened = ContinuityStore(service.store.db_path)
    assert reopened.get_step(step["step_id"])["verification"] == "user_confirmed"
    assert reopened.get_step(step["step_id"])["status"] == "done"
    assert len(reopened.session_detail(session["session_key"])["history"]) == 1
    reopened.close()


def test_internal_pause_survives_reconstruction_and_forget(tmp_path):
    service, memory, *_ = setup_service(tmp_path)
    memory.save_message("s", "user", "기록")
    service.sync()
    source_id = service.sources()[0]["source_id"]
    service.delete_source(source_id)
    assert not service.list_sessions()
    replacement = ContinuityService(service.store, memory_path=service.paths["messages"],
        dialogue_path=service.paths["tasks"], plan_path=service.paths["plans"], journal_path=service.paths["actions"])
    assert not replacement.sources()[0]["enabled"]
    assert replacement.sync()["events"] == 0
    assert memory.load_session("s")


def test_notifications_opt_in_deduplicated_snoozable(tmp_path):
    messages = []
    policy = ProactiveNotificationPolicy(messages.append, store=ProactiveStore(str(tmp_path / "notify.db")),
        context=InterruptionContextManager(str(tmp_path / "context.json")))
    service, memory, *_ = setup_service(tmp_path, policy)
    memory.save_message("s", "user", "할 일")
    service.sync()
    step = service.confirm_suggestion(service.list_sessions()[0]["session_key"], "자료 검토")
    service.update_step(step["step_id"], due_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    service.sync()
    assert not messages
    service.set_notifications_enabled(True)
    service.update_step(step["step_id"], snoozed_until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    service.sync()
    assert not messages
    service.update_step(step["step_id"], snoozed_until=None)
    service.sync()
    service.sync()
    assert len(messages) == 1
    assert "마감" in messages[0]


def test_missing_external_source_is_partial(tmp_path):
    service, *_ = setup_service(tmp_path)
    service.store.add_source("codex", str(tmp_path / "missing"))
    report = service.sync()
    assert report["status"] == "partial"
    assert report["errors"]


def test_fullscreen_defers_reminder_then_delivers_once(tmp_path, monkeypatch):
    fullscreen = [True]
    monkeypatch.setattr(InterruptionContextManager, "_detect_fullscreen",
                        staticmethod(lambda: fullscreen[0]))
    messages = []
    policy = ProactiveNotificationPolicy(messages.append,
        store=ProactiveStore(str(tmp_path / "notify.db")),
        context=InterruptionContextManager(str(tmp_path / "context.json")))
    service, memory, *_ = setup_service(tmp_path, policy)
    memory.save_message("s", "user", "검토")
    service.sync()
    step = service.confirm_suggestion(service.list_sessions()[0]["session_key"])
    service.update_step(step["step_id"], due_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    service.set_notifications_enabled(True)
    service.sync()
    service.sync()
    assert not messages and len(policy.store.pending()) == 1
    fullscreen[0] = False
    assert policy.flush_digest()
    service.sync()
    assert policy.flush_digest() is None
    assert len(messages) == 1 and not policy.store.pending()


def test_deferred_reminder_is_dismissed_after_completion(tmp_path):
    messages = []
    context = InterruptionContextManager(str(tmp_path / "focus.json"))
    context.update(focus_mode=True)
    policy = ProactiveNotificationPolicy(messages.append, store=ProactiveStore(str(tmp_path / "notify.db")), context=context)
    service, memory, *_ = setup_service(tmp_path, policy)
    memory.save_message("s", "user", "검토")
    service.sync()
    step = service.confirm_suggestion(service.list_sessions()[0]["session_key"])
    service.update_step(step["step_id"], due_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    service.set_notifications_enabled(True)
    service.sync()
    assert len(policy.store.pending()) == 1
    service.update_step(step["step_id"], status="done")
    context.update(focus_mode=False)
    assert policy.flush_digest() is None
    assert not messages and not policy.store.pending()


def test_external_source_requires_explicit_connection(tmp_path):
    service, *_ = setup_service(tmp_path)
    assert [s["provider"] for s in service.sources()] == ["internal"]
    assert not service.notifications_enabled


def test_next_action_chat_does_not_cancel_running_work(tmp_path):
    import main_qt
    import threading
    service, memory, *_ = setup_service(tmp_path)
    calls = []
    app = main_qt.JarvisApp.__new__(main_qt.JarvisApp)
    app.continuity_service = service
    app.window = SimpleNamespace(show_user_text=lambda text: calls.append("user"),
        show_assistant_text=lambda text: calls.append("assistant"),
        show_continuity_reminder=lambda: calls.append("reminders"))
    app.memory, app.messages, app.session_id = memory, [], "active"
    app._is_processing_ai = True
    app._cancel_pending_work = lambda: calls.append("cancelled")
    delivered = threading.Event()
    app.executor = SimpleNamespace(render_outcome=lambda outcome, *_args: outcome)
    app.state_machine = SimpleNamespace(start_responding=lambda: None)
    app._personalize_address = lambda text: text
    def deliver(message):
        app._on_proactive_message(message)
        delivered.set()
    app.signals = SimpleNamespace(proactive_message=SimpleNamespace(emit=deliver))
    app._on_user_input("지금 뭐 해야 돼?")
    assert delivered.wait(5)
    assert calls == ["user", "reminders", "assistant"]
    assert app._is_processing_ai
    assert len(memory.load_session("active")) == 2


def test_query_matching_preserves_execution_commands():
    assert is_continuity_query("/now")
    assert not is_continuity_query("다음 할 일을 실행해줘")
    assert not is_continuity_query("통합 리마인더 코드를 수정해줘")


def test_journal_records_turn_identity(tmp_path):
    from core.tools import ToolExecutor
    from core.turn_context import TurnExecutionContext, bind_turn_context
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._action_journal = ActionJournal(str(tmp_path / "journal.db"))
    with bind_turn_context(TurnExecutionContext("turn-a", "session-a", "C:/report")):
        executor._record_tool_run({}, ToolRunResult.successful(tool_name="read_file", raw_output="ok",
                                     evidence=[Evidence("file", "read")]))
    data = executor._action_journal.get_recent()[0].data
    assert data["session_id"] == "session-a"
    assert data["workspace_path"] == "C:/report"
