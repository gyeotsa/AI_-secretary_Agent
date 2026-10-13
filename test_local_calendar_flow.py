"""Real semantic routing, persisted clarification, and local schedule writes."""
from datetime import date, datetime, timedelta

import pytest

from core.local_calendar import CALENDAR_TIMEZONE, LocalCalendarStore
from core.executor import ExecutionOutcome
from core.tool_result import Evidence, ToolRunResult
from plugins.local_calendar import LocalCalendarPlugin
from core.response_realizer import ResponseRealizer
from test_semantic_executor_flow import make_executor


class NoModelCalls:
    def chat(self, *_args, **_kwargs):
        raise AssertionError("Explicit local calendar requests must not need model guesses")


def test_saved_schedule_receipt_is_not_rewritten():
    receipt = "내 캘린더에 치과 예약을 등록했습니다. 2026-10-25 17:00 ~ 18:00 (기본 1시간)"
    result = ResponseRealizer(NoModelCalls()).realize(
        receipt, tool_name="local_calendar_create_event", user_request="치과 일정 등록해줘",
        assistant_name="아니스", address="사용자",
    )
    assert result == receipt


def test_chat_registration_reaches_saved_calendar(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    result = executor.execute_turn("2026-10-25 17:00 치과 예약을 등록해줘", "calendar-flow")
    assert result.status == "completed"
    events = store.list_events(date(2026, 10, 25), date(2026, 10, 26))
    assert len(events) == 1 and events[0]["title"] == "치과 예약"
    assert events[0]["end_at"] - events[0]["start_at"] == 3600
    assert events[0]["reminder_minutes"] == 60
    assert "60분 전" in result.response
    assert "등록" in result.response
    assert executor.plan_calls[0].steps[0].tool_name == "local_calendar_create_event"


def test_missing_time_then_reply_reuses_pending_request(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    first = executor.execute_turn("10월 25일 치과 예약을 등록해줘", "calendar-followup")
    assert first.status == "awaiting_user" and not executor.plan_calls
    second = executor.execute_turn("2026-10-25 17:00", "calendar-followup")
    assert second.status == "completed" and first.task_id == second.task_id
    assert len(store.list_events(date(2026, 10, 25), date(2026, 10, 26))) == 1


def test_new_registration_does_not_inherit_pending_title(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    executor.execute_turn("10월 25일 치과 예약을 등록해줘", "calendar-new")
    new_request = executor.execute_turn("2026-10-26 15:00 일정 등록해줘", "calendar-new")
    assert new_request.status == "awaiting_user" and "제목" in new_request.response
    assert not executor.plan_calls
    reply = executor.execute_turn("제목은 회의", "calendar-new")
    assert reply.status == "completed"
    events = store.list_events(date(2026, 10, 26), date(2026, 10, 27))
    assert len(events) == 1 and events[0]["title"] == "회의"


@pytest.mark.parametrize("setting,minutes", [("30분 전에 알림 설정해서", 30), ("알림 없이", None)])
def test_chat_registration_keeps_explicit_reminder_setting(make_executor, tmp_path, setting, minutes):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    result = executor.execute_turn(f"2026-10-25 17:00 회의 일정 {setting} 등록해줘", "calendar-reminder")
    assert result.status == "completed"
    events = store.list_events(date(2026, 10, 25), date(2026, 10, 26))
    assert len(events) == 1 and events[0]["title"] == "회의"
    assert events[0]["reminder_minutes"] == minutes


def test_invalid_chat_reminder_fails_without_saving_default(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    result = executor.execute_turn("2026-10-25 17:00 회의 일정 10081분 전 알림 등록해줘", "calendar-reminder-invalid")
    assert result.status == "failed"
    assert store.list_events(date(2026, 10, 25), date(2026, 10, 26)) == []


class CommentService:
    def __init__(self, failure=False):
        self.calls = []
        self.failure = failure

    def comment(self, change, event, store=None):
        self.calls.append((change, dict(event), store))
        if self.failure:
            raise OSError("main model unavailable")
        return {"text": "앞 일정과 간격이 짧습니다. 이동 시간을 확인해주세요.", "model": "selected-main", "available": True}


def test_verified_chat_add_and_delete_append_comment_without_rewriting_receipts(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    service = executor.calendar_comment_service = CommentService()
    added = executor.execute_turn("2026-10-25 17:00 치과 예약 일정 등록해줘", "calendar-comment")
    assert added.status == "completed" and added.response_generated
    saved = store.list_events(date(2026, 10, 25), date(2026, 10, 26))[0]
    assert service.calls == [("added", saved, store)]
    assert added.response == added.tool_result.raw_output + "\n\nAI 코멘트: 앞 일정과 간격이 짧습니다. 이동 시간을 확인해주세요."
    executor.render_outcome(added, added.goal, session_id="calendar-comment")
    assert len(service.calls) == 1
    deleted = executor.execute_turn("2026-10-25 치과 예약 일정 삭제해줘", "calendar-comment")
    assert deleted.status == "completed" and "삭제했습니다" in deleted.response
    assert service.calls[-1] == ("deleted", saved, store)
    assert deleted.response.startswith(deleted.tool_result.raw_output + "\n\nAI 코멘트: ")
    assert store.get_event(saved["id"]) is None
    task = executor.dialogue_state.get_task("calendar-comment", deleted.task_id, executor._workspace_scope())
    assert task.result == deleted.response


def test_comment_model_failure_preserves_verified_change_and_success(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    service = executor.calendar_comment_service = CommentService(failure=True)
    result = executor.execute_turn("2026-10-25 17:00 회의 일정 등록해줘", "calendar-comment-failure")
    assert result.status == "completed" and "등록했습니다" in result.response
    assert "AI 코멘트를 받지 못했습니다" in result.response
    assert len(store.list_events(date(2026, 10, 25), date(2026, 10, 26))) == 1
    assert len(service.calls) == 1


def test_ambiguous_chat_delete_waits_and_exact_time_reply_deletes_one(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    begin = datetime(2026, 10, 25, 17, tzinfo=CALENDAR_TIMEZONE)
    first = store.add_event("회의", begin, begin + timedelta(hours=1))
    second = store.add_event("회의", begin + timedelta(hours=2), begin + timedelta(hours=3))
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    service = executor.calendar_comment_service = CommentService()
    waiting = executor.execute_turn("2026-10-25 회의 일정 삭제해줘", "calendar-delete-duplicate")
    assert waiting.status == "awaiting_user" and "17:00" in waiting.response and "19:00" in waiting.response
    assert not executor.plan_calls and not service.calls
    completed = executor.execute_turn("오후 5시", "calendar-delete-duplicate")
    assert completed.status == "completed" and waiting.task_id == completed.task_id
    assert store.get_event(first["id"]) is None and store.get_event(second["id"]) == second
    assert service.calls == [("deleted", first, store)]


@pytest.mark.parametrize("status", ["failed", "unverified", "awaiting_user", "cancelled"])
def test_calendar_comment_only_runs_for_completed_verified_mutations(make_executor, status):
    executor = make_executor(model=NoModelCalls())
    service = executor.calendar_comment_service = CommentService()
    tool = ToolRunResult.successful(
        tool_name="local_calendar_create_event", raw_output="receipt",
        evidence=[Evidence("local_calendar_event", "snapshot", {"event": {"id": "test"}})],
    )
    outcome = ExecutionOutcome("원래 결과", status, tool_result=tool)
    rendered = executor.render_outcome(outcome, "요청")
    assert not service.calls and rendered.response == "원래 결과"


def test_new_delete_request_does_not_inherit_old_title_or_date(make_executor, tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    executor = make_executor(model=NoModelCalls())
    executor.intent_router.registry.register_plugin(LocalCalendarPlugin(store))
    executor.calendar_comment_service = CommentService()
    executor.execute_turn("2026-10-25 회의 일정 삭제해줘", "calendar-delete-new")
    new = executor.execute_turn("2026-10-26 일정 삭제해줘", "calendar-delete-new")
    assert new.status == "awaiting_user" and "제목" in new.response
    assert not executor.plan_calls
