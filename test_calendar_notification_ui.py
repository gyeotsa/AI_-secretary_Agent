"""Calendar reminder card checks without real schedules or audible sounds."""
import os
import sys
from datetime import datetime
from types import SimpleNamespace
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from core.local_calendar import CALENDAR_TIMEZONE
from ui.calendar_notification import CalendarNotificationManager, play_notification_sound
from ui.theme import theme_manager


@pytest.fixture(scope="module", autouse=True)
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def notifications(app, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == CALENDAR_TIMEZONE
            return cls(2026, 10, 11, 16, tzinfo=tz)

    monkeypatch.setattr("ui.calendar_notification.datetime", Clock)
    sounds = []
    monkeypatch.setattr("ui.calendar_notification.play_notification_sound", lambda: sounds.append(True))
    owner = QWidget()
    opened = []
    manager = CalendarNotificationManager(owner, lambda event: opened.append(event))
    yield manager, owner, opened, sounds
    manager.shutdown()
    owner.close()
    owner.deleteLater()
    app.processEvents()


def event(title="치과 예약", *, all_day=False, location=""):
    return {"id": title, "title": title,
            "start_at": int(datetime(2026, 10, 11, 17, tzinfo=CALENDAR_TIMEZONE).timestamp()),
            "end_at": int(datetime(2026, 10, 11, 18, tzinfo=CALENDAR_TIMEZONE).timestamp()),
            "all_day": all_day, "location": location, "description": "", "reminder_minutes": 60}


def test_card_plain_text_korean_time_no_focus_and_available_screen_position(notifications, app, monkeypatch):
    manager, owner, _opened, sounds = notifications
    available = QRect(1200, 100, 1000, 700)
    monkeypatch.setattr(owner, "screen", lambda: SimpleNamespace(availableGeometry=lambda: available))
    owner.showMinimized()
    manager.enqueue(event("<b>치과 예약</b>", location="<i>서울</i>"))
    app.processEvents()
    card = manager.notification
    assert card.isVisible() and manager.is_busy
    assert card.parentWidget() is None
    assert card.heading.text() == "일정 알림"
    assert card.title_label.text() == "<b>치과 예약</b>"
    assert card.title_label.textFormat() == card.detail_label.textFormat() == Qt.TextFormat.PlainText
    assert "2026년 10월 11일 17:00 (한국 시간)" in card.detail_label.text()
    assert "시작까지 60분" in card.detail_label.text()
    assert "장소: <i>서울</i>" in card.detail_label.text()
    assert card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert card.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert card.width() == 360
    assert card.x() + card.width() == available.right() - 15
    assert card.y() + card.height() == available.bottom() - 15
    assert card.hide_timer.interval() == 20000 and card.hide_timer.isSingleShot()
    assert len(sounds) == 1


def test_close_and_open_calendar_receive_current_event(notifications):
    manager, _owner, opened, _sounds = notifications
    first = event()
    manager.enqueue(first)
    manager.notification.close_button.click()
    assert not manager.is_busy and not manager.notification.hide_timer.isActive() and not opened
    manager.enqueue(first)
    manager.notification.calendar_button.click()
    assert opened == [first] and opened[0] is not first
    assert not manager.is_busy


def test_auto_hide_advances_queue_without_replacing_first_event(notifications):
    manager, _owner, _opened, sounds = notifications
    first, second, third = event("첫 일정"), event("두 번째"), event("세 번째")
    manager.enqueue(first)
    manager.enqueue(second)
    manager.enqueue(third)
    assert manager.notification.current_event == first and len(sounds) == 1
    manager.notification.hide_timer.start(15)
    QTest.qWait(40)
    assert manager.notification.current_event == second and manager.is_busy and len(sounds) == 2
    manager.notification.close()
    assert manager.notification.current_event == third and manager.notification.isVisible() and len(sounds) == 3
    manager.notification.close_button.click()
    assert not manager.is_busy


def test_card_live_theme_all_day_and_start_time_text(notifications, monkeypatch):
    manager, _owner, _opened, _sounds = notifications
    due = event(all_day=True)
    due["start_at"] = int(datetime(2026, 10, 11, 0, tzinfo=CALENDAR_TIMEZONE).timestamp())
    manager.enqueue(due)
    assert "첫날 00:00 기준" in manager.notification.detail_label.text()
    assert "시작 시각이 되었습니다." in manager.notification.detail_label.text()
    theme = theme_manager()
    original = theme.mode
    try:
        for mode in ("light", "dark"):
            theme.set_mode(mode, persist=False)
            assert manager.notification.palette().color(QPalette.ColorRole.Window).name() == theme.colors["window"]
            assert manager.notification.title_label.text() == due["title"]
    finally:
        theme.set_mode(original, persist=False)


def test_shutdown_is_idempotent_stops_timer_and_drops_queue(notifications):
    manager, _owner, _opened, sounds = notifications
    manager.enqueue(event("첫 일정"))
    manager.enqueue(event("대기 일정"))
    manager.shutdown()
    manager.shutdown()
    manager.enqueue(event("종료 후 일정"))
    assert not manager.is_busy and not manager.notification.isVisible()
    assert not manager.notification.hide_timer.isActive() and len(sounds) == 1


def test_deleted_reminder_is_removed_from_card_and_queue_without_hiding_other_events(notifications):
    manager, _owner, _opened, sounds = notifications
    first, second, third = event("첫 일정"), event("삭제될 대기 일정"), event("남은 일정")
    manager.enqueue(first)
    manager.enqueue(second)
    manager.enqueue(third)
    manager.discard_event(second["id"])
    assert manager.notification.current_event == first and len(sounds) == 1
    manager.discard_event(first["id"])
    assert manager.notification.current_event == third and len(sounds) == 2
    manager.discard_event("missing")
    assert manager.notification.current_event == third and manager.is_busy


def test_failed_calendar_callback_keeps_card_and_next_reminder_available(notifications):
    manager, _owner, _opened, _sounds = notifications
    def fail(_event):
        raise RuntimeError("private-error-marker")
    manager.notification.open_calendar = fail
    manager.enqueue(event("첫 일정"))
    manager.enqueue(event("두 번째"))
    manager.notification.calendar_button.click()
    assert manager.notification.isVisible() and manager.notification.hide_timer.isActive()
    assert "캘린더를 열지 못했습니다." in manager.notification.detail_label.text()
    assert "private-error-marker" not in manager.notification.detail_label.text()
    manager.notification.close_button.click()
    assert manager.notification.title_label.text() == "두 번째"


def test_windows_sound_uses_async_alias_and_falls_back_without_raising(monkeypatch):
    calls = []
    sound = SimpleNamespace(SND_ALIAS=1, SND_ASYNC=2, MB_ICONASTERISK=3,
                            PlaySound=lambda *args: calls.append(("play", args)),
                            MessageBeep=lambda *args: calls.append(("beep", args)))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winsound", sound)
    play_notification_sound()
    assert calls == [("play", ("SystemNotification", 3))]
    def fail(*args):
        raise RuntimeError("unavailable audio device")
    sound.PlaySound = fail
    play_notification_sound()
    assert calls[-1] == ("beep", (3,))
    sound.MessageBeep = fail
    play_notification_sound()


def test_other_platform_uses_application_beep(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(QApplication, "beep", lambda: calls.append(True))
    play_notification_sound()
    assert calls == [True]


def test_alarm_rings_while_schedule_card_is_visible_and_restores_it_silently(notifications, app):
    manager, owner, opened, sounds = notifications
    owner.showMinimized()
    schedule = event("회의")
    manager.enqueue(schedule)
    manager.enqueue(event("다음 일정"))
    alarm = {"job_id": 42, "action_type": "alarm", "result": "<b>휴식 끝</b>"}
    manager.enqueue_alarm(alarm)
    app.processEvents()
    card = manager.notification
    assert card.isVisible() and card.heading.text() == "알람"
    assert card.windowTitle() == "아니스 · 알람" and card.title_label.text() == "<b>휴식 끝</b>"
    assert card.title_label.textFormat() == Qt.TextFormat.PlainText
    assert "설정한 시간이 되었습니다." in card.detail_label.text()
    assert not card.calendar_button.isVisible() and card.current_event["id"] == "alarm:42"
    assert len(sounds) == 2 and not opened
    manager.discard_event("missing-calendar-event")
    assert card.heading.text() == "알람"
    card.close_button.click()
    assert card.current_event == schedule and card.calendar_button.isVisible()
    assert card.heading.text() == "일정 알림" and len(sounds) == 2
    card.close_button.click()
    assert card.title_label.text() == "다음 일정" and len(sounds) == 3


def test_each_alarm_rings_immediately_and_shutdown_ignores_new_alarm(notifications):
    manager, _owner, _opened, sounds = notifications
    manager.enqueue_alarm({"job_id": 1, "action_type": "alarm", "result": "첫 알람"})
    manager.enqueue_alarm({"job_id": 2, "action_type": "alarm", "result": "두 번째 알람"})
    assert manager.notification.title_label.text() == "두 번째 알람" and len(sounds) == 2
    manager.notification.close_button.click()
    assert manager.notification.title_label.text() == "첫 알람" and len(sounds) == 2
    manager.shutdown()
    manager.enqueue_alarm({"job_id": 3, "action_type": "alarm"})
    assert not manager.is_busy and len(sounds) == 2


def test_scheduler_signal_delivers_alarm_on_gui_thread(notifications, app):
    from PyQt6.QtCore import QThread
    from main_qt import AppSignals
    manager, _owner, _opened, sounds = notifications
    signals = AppSignals()
    delivered_threads = []
    def deliver(event):
        delivered_threads.append(QThread.currentThread())
        manager.enqueue_alarm(event)
    signals.automation_result.connect(deliver)
    worker = threading.Thread(target=lambda: signals.automation_result.emit(
        {"job_id": 9, "action_type": "alarm", "result": "백그라운드 알람"}))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and not sounds
    app.processEvents()
    assert delivered_threads == [app.thread()] and len(sounds) == 1
    assert manager.notification.title_label.text() == "백그라운드 알람"
