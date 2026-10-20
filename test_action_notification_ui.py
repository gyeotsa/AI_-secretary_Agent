"""Explicit choices, stale-request safety and nonmodal question cards."""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from ui.action_notification import RequestNotificationManager
from ui.theme import theme_manager


@pytest.fixture(scope="module", autouse=True)
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture
def notifications(app, monkeypatch):
    sounds = []
    monkeypatch.setattr("ui.action_notification.play_notification_sound", lambda: sounds.append(True))
    owner = QWidget()
    manager = RequestNotificationManager(owner)
    yield manager, owner, sounds
    manager.shutdown()
    owner.close()
    owner.deleteLater()
    app.processEvents()


def enqueue(manager, key, answers, cancels, **kwargs):
    return manager.enqueue(key, "확인이 필요해요", "어느 폴더에 저장할까요?",
                           [("작업 폴더", "workspace"), ("취소", "cancel")],
                           lambda value: answers.append((key, value)),
                           lambda: cancels.append(key), **kwargs)


def test_question_is_plain_text_nonmodal_stays_above_minimized_owner(notifications, app, monkeypatch):
    manager, owner, sounds = notifications
    available = QRect(1200, 100, 1000, 700)
    monkeypatch.setattr(owner, "screen", lambda: SimpleNamespace(availableGeometry=lambda: available))
    owner.showMinimized()
    answers = []
    message = '<img src="file:///private.png">\n' + "긴 내용\n" * 100
    manager.enqueue("request", "<b>질문</b>", message, [("예", True)], answers.append)
    app.processEvents()
    card = manager.notification
    assert card.isVisible() and not card.isModal() and card.parentWidget() is None
    assert card.windowFlags() & Qt.WindowType.Tool
    assert card.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert card.title_label.text() == "<b>질문</b>"
    assert card.title_label.textFormat() == Qt.TextFormat.PlainText
    assert card.message_view.toPlainText() == message
    assert card.message_view.maximumHeight() == 150
    assert not card.message_view.openLinks() and not card.message_view.openExternalLinks()
    assert card.x() + card.width() == available.right() - 15
    assert card.y() + card.height() == available.bottom() - 15
    assert all(not button.autoDefault() and not button.isDefault() for button in card.choice_buttons)
    assert len(sounds) == 1 and not answers
    assert not manager.validation_timer.isActive()


def test_click_answers_exactly_once_and_advances_fifo_without_overwriting(notifications):
    manager, _owner, sounds = notifications
    answers, cancels = [], []
    for key in ("first", "second", "third"):
        assert enqueue(manager, key, answers, cancels)
    assert manager.notification.request_id == "first" and len(sounds) == 1
    assert manager.notification.queue_label.text() == "대기 중인 요청 2개"
    old_button = manager.notification.choice_buttons[0]
    old_button.click()
    assert answers == [("first", "workspace")]
    assert manager.notification.request_id == "second" and len(sounds) == 2
    old_button.click()
    assert answers == [("first", "workspace")]
    manager.notification.choice_buttons[1].click()
    manager.notification.choice_buttons[0].click()
    assert answers == [("first", "workspace"), ("second", "cancel"), ("third", "workspace")]
    assert not cancels and not manager.is_busy and not manager.notification.isVisible()
    assert len(sounds) == 3


def test_close_and_escape_cancel_only_current_question(notifications):
    manager, _owner, _sounds = notifications
    answers, cancels = [], []
    enqueue(manager, "first", answers, cancels)
    enqueue(manager, "second", answers, cancels)
    enqueue(manager, "third", answers, cancels)
    manager.notification.close_button.click()
    manager.notification.close()
    QTest.keyClick(manager.notification, Qt.Key.Key_Escape)
    assert cancels == ["first", "second", "third"] and not answers
    assert not manager.is_busy and not manager.notification.isVisible()
    manager.notification.close_button.click()
    assert cancels == ["first", "second", "third"]


def test_duplicate_rejected_and_queued_cancellation_never_shows_or_sounds(notifications):
    manager, _owner, sounds = notifications
    answers, cancels = [], []
    assert enqueue(manager, "first", answers, cancels)
    assert not enqueue(manager, "first", answers, cancels)
    assert enqueue(manager, "second", answers, cancels)
    assert not enqueue(manager, "second", answers, cancels)
    manager.cancel("second")
    manager.cancel("second")
    manager.cancel("missing")
    assert cancels == ["second"] and len(sounds) == 1
    assert manager.notification.request_id == "first" and not manager.notification.queue_label.text()
    manager.notification.choice_buttons[0].click()
    assert answers == [("first", "workspace")] and not manager.is_busy


def test_invalid_queued_and_active_requests_cancel_without_automatic_answer(notifications):
    manager, _owner, sounds = notifications
    answers, cancels = [], []
    valid = {"first": True, "second": True}
    enqueue(manager, "first", answers, cancels, is_valid=lambda: valid["first"])
    enqueue(manager, "second", answers, cancels, is_valid=lambda: valid["second"])
    assert manager.validation_timer.isActive() and manager.validation_timer.interval() == 200
    valid["first"] = valid["second"] = False
    QTest.qWait(240)
    assert cancels == ["first", "second"] and not answers and len(sounds) == 1
    assert not manager.is_busy and not manager.validation_timer.isActive()


def test_validity_checked_again_at_click_before_poll_and_exceptions_fail_closed(notifications):
    manager, _owner, sounds = notifications
    answers, cancels = [], []
    valid = [True]
    enqueue(manager, "first", answers, cancels, is_valid=lambda: valid[0])
    valid[0] = False
    manager.notification.choice_buttons[0].click()
    assert cancels == ["first"] and not answers
    def failed_check():
        raise RuntimeError("private-error-marker")
    enqueue(manager, "second", answers, cancels, is_valid=failed_check)
    assert cancels == ["first", "second"] and not answers and len(sounds) == 1
    assert not manager.is_busy


def test_callback_failure_does_not_block_next_request_or_expose_details(notifications, caplog):
    manager, _owner, _sounds = notifications
    answers, cancels = [], []
    def fail(_value):
        raise RuntimeError("private-error-marker")
    manager.enqueue("first", "질문", "내용", [("예", True)], fail)
    enqueue(manager, "second", answers, cancels)
    manager.notification.choice_buttons[0].click()
    assert manager.notification.request_id == "second" and manager.is_busy
    assert "private-error-marker" not in caplog.text
    manager.notification.choice_buttons[0].click()
    assert answers == [("second", "workspace")] and not manager.is_busy


def test_reentrant_answer_can_enqueue_without_overwriting_waiting_question(notifications):
    manager, _owner, sounds = notifications
    answers, cancels = [], []
    def add_third(value):
        answers.append(("first", value))
        enqueue(manager, "third", answers, cancels)
    manager.enqueue("first", "질문", "내용", [("예", True)], add_third)
    enqueue(manager, "second", answers, cancels)
    manager.notification.choice_buttons[0].click()
    assert manager.notification.request_id == "second" and len(sounds) == 2
    manager.notification.choice_buttons[0].click()
    assert manager.notification.request_id == "third" and len(sounds) == 3


def test_shutdown_cancels_all_once_stops_timer_and_rejects_future_requests(notifications):
    manager, _owner, sounds = notifications
    answers, cancels = [], []
    enqueue(manager, "first", answers, cancels, is_valid=lambda: True)
    enqueue(manager, "second", answers, cancels)
    manager.shutdown()
    manager.shutdown()
    assert cancels == ["first", "second"] and not answers
    assert not enqueue(manager, "third", answers, cancels)
    assert not manager.is_busy and not manager.notification.isVisible()
    assert not manager.validation_timer.isActive() and len(sounds) == 1


def test_card_uses_live_light_and_dark_theme(notifications):
    manager, _owner, _sounds = notifications
    enqueue(manager, "first", [], [])
    theme = theme_manager()
    original = theme.mode
    try:
        for mode in ("light", "dark"):
            theme.set_mode(mode, persist=False)
            assert manager.notification.palette().color(QPalette.ColorRole.Window).name() == theme.colors["window"]
            assert manager.notification.title_label.text() == "확인이 필요해요"
    finally:
        theme.set_mode(original, persist=False)
