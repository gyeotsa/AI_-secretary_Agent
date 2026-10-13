from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from core.local_calendar import CALENDAR_TIMEZONE, LocalCalendarStore
from main_qt import JarvisApp


def bare_app(store):
    app = JarvisApp.__new__(JarvisApp)
    app._runtime_shutdown_started = False
    app.calendar_reminder_store = store
    app.calendar_notifications = SimpleNamespace(is_busy=False, enqueue=Mock(), discard_event=Mock(),
                                                notification=SimpleNamespace(isVisible=lambda: False))
    return app


def test_poll_delivers_due_schedule_once_and_holds_others_when_busy(tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    now = datetime.now(CALENDAR_TIMEZONE).replace(microsecond=0)
    first = store.add_event("첫 일정", now + timedelta(minutes=10), now + timedelta(hours=1))
    second = store.add_event("두 번째", now + timedelta(minutes=20), now + timedelta(hours=2))
    store.add_event("알림 끔", now, now + timedelta(hours=1), reminder_minutes=None)
    app = bare_app(store)
    app._check_calendar_reminders()
    app.calendar_notifications.enqueue.assert_called_once_with(first)
    app.calendar_notifications.is_busy = True
    app._check_calendar_reminders()
    app.calendar_notifications.enqueue.assert_called_once()
    app.calendar_notifications.is_busy = False
    app._check_calendar_reminders()
    assert app.calendar_notifications.enqueue.call_args.args == (second,)
    app._check_calendar_reminders()
    assert app.calendar_notifications.enqueue.call_count == 2


def test_shutdown_does_not_claim_pending_schedule():
    store = Mock()
    app = bare_app(store)
    app._runtime_shutdown_started = True
    app._check_calendar_reminders()
    store.take_due_reminders.assert_not_called()


def test_poll_discards_visible_reminder_after_schedule_deleted_in_chat(tmp_path):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    now = datetime.now(CALENDAR_TIMEZONE)
    event = store.add_event("삭제할 일정", now, now + timedelta(hours=1))
    app = bare_app(store)
    app.calendar_notifications.notification = SimpleNamespace(isVisible=lambda: True, current_event=event)
    app.calendar_notifications.is_busy = True
    app._check_calendar_reminders()
    app.calendar_notifications.discard_event.assert_not_called()
    store.delete_event(event["id"])
    app._check_calendar_reminders()
    app.calendar_notifications.discard_event.assert_called_once_with(event["id"])


def test_ui_deletion_discards_only_deleted_reminder():
    app = bare_app(Mock())
    app._on_calendar_schedule_changed({"change": "added", "event": {"id": "other"}})
    app.calendar_notifications.discard_event.assert_not_called()
    app._on_calendar_schedule_changed({"change": "deleted", "event": {"id": "removed"}})
    app.calendar_notifications.discard_event.assert_called_once_with("removed")


def test_poll_storage_failure_is_private_and_retried(capsys):
    store = Mock()
    store.take_due_reminders.side_effect = OSError("private-token-marker")
    app = bare_app(store)
    app._check_calendar_reminders()
    app._check_calendar_reminders()
    output = capsys.readouterr().out
    assert output.count("OSError") == 1 and "private-token-marker" not in output
    app.calendar_notifications.enqueue.assert_not_called()
    store.take_due_reminders.side_effect = None
    store.take_due_reminders.return_value = []
    app._check_calendar_reminders()
    assert not app._calendar_reminder_error


def test_open_reminder_calendar_restores_window_and_selects_schedule_date():
    app = JarvisApp.__new__(JarvisApp)
    app.window = Mock()
    app.window.isMinimized.return_value = True
    event = {"start_at": int(datetime(2026, 10, 25, 17, tzinfo=CALENDAR_TIMEZONE).timestamp())}
    app._open_reminder_calendar(event)
    app.window.showNormal.assert_called_once()
    app.window.show_local_calendar.assert_called_once()
    selected = app.window.calendar_dialog.calendar.setSelectedDate.call_args.args[0]
    assert (selected.year(), selected.month(), selected.day()) == (2026, 10, 25)
    app.window.activateWindow.assert_called_once()


def test_calendar_poll_preserves_standalone_alarm_card():
    store = Mock()
    app = bare_app(store)
    app.calendar_notifications.notification = SimpleNamespace(
        isVisible=lambda: True, current_event={"id": "alarm:3", "action_type": "alarm"})
    app.calendar_notifications.is_busy = True
    app._check_calendar_reminders()
    store.get_event.assert_not_called()
    store.take_due_reminders.assert_not_called()
    app.calendar_notifications.discard_event.assert_not_called()


def test_due_alarm_uses_card_without_overwriting_chat_or_waiting_for_model_or_tts():
    app = bare_app(Mock())
    app.calendar_notifications.enqueue_alarm = Mock()
    app.window = Mock()
    app.memory = Mock()
    app.messages = []
    app.session_id = "current-chat"
    app.notify_user = Mock()
    app._is_processing_ai = True
    alarm = {"job_id": 7, "action_type": "alarm", "result": "회의 준비"}
    app._on_automation_result(alarm)
    app.calendar_notifications.enqueue_alarm.assert_called_once_with(alarm)
    app.window.show_assistant_text.assert_not_called()
    app.memory.save_message.assert_not_called()
    assert app.messages == []
    app.notify_user.assert_not_called()
    assert app._is_processing_ai
    app._runtime_shutdown_started = True
    app._on_automation_result(alarm)
    app.calendar_notifications.enqueue_alarm.assert_called_once()


def test_other_automation_results_keep_existing_response_handling():
    app = bare_app(Mock())
    app.notify_user = Mock()
    app._on_automation_result({"action_type": "llm", "result": "완료"})
    assert app.notify_user.call_args.kwargs["status"] == "completed"
    app._on_automation_result({"action_type": "alarm", "error": "실패"})
    assert app.notify_user.call_args.kwargs["status"] == "failed"
