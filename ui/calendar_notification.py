"""Schedule reminders and standalone alarms that also appear while minimized."""
from collections import deque
from datetime import datetime
from math import ceil
import sys

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from core.local_calendar import CALENDAR_TIMEZONE
from .theme import set_widget_style


def play_notification_sound():
    try:
        if sys.platform != "win32":
            QApplication.beep()
            return
        import winsound
        try:
            winsound.PlaySound("SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


class CalendarNotification(QDialog):
    dismissed = pyqtSignal()

    def __init__(self, owner, open_calendar):
        # A native parent would hide the card when its owner is minimized.
        super().__init__(None)
        self.owner = owner
        self.open_calendar = open_calendar
        self.current_event = None
        self.setWindowTitle("아니스 · 일정 알림")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(360)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        self.heading = QLabel("일정 알림")
        self.heading.setObjectName("heading")
        root.addWidget(self.heading)
        self.title_label = QLabel()
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setMaximumHeight(72)
        set_widget_style(self.title_label, "font-size: 16px; font-weight: 600;")
        root.addWidget(self.title_label)
        self.detail_label = QLabel()
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_label.setWordWrap(True)
        self.detail_label.setMaximumHeight(110)
        root.addWidget(self.detail_label)
        buttons = QHBoxLayout()
        self.calendar_button = QPushButton("내 캘린더")
        self.calendar_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.calendar_button.clicked.connect(self._open_calendar)
        buttons.addWidget(self.calendar_button)
        buttons.addStretch()
        self.close_button = QPushButton("닫기")
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.clicked.connect(self.dismiss)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(20000)
        self.hide_timer.timeout.connect(self.dismiss)
        set_widget_style(self, "QDialog { border: 1px solid #555555; border-radius: 10px; }")

    def show_event(self, event, *, sound=True):
        self.current_event = dict(event)
        alarm = event.get("action_type") == "alarm"
        self.heading.setText("알람" if alarm else "일정 알림")
        self.setWindowTitle("아니스 · 알람" if alarm else "아니스 · 일정 알림")
        self.calendar_button.setVisible(not alarm)
        if alarm:
            self.title_label.setText(event.get("result") or "알람 시간입니다.")
            self.detail_label.setText(
                f"설정한 시간이 되었습니다.\n알림 시각: {datetime.now(CALENDAR_TIMEZONE):%H:%M:%S} (한국 시간)"
            )
        else:
            self._set_schedule_text(event)
        self.adjustSize()
        screen = self.owner.screen() if self.owner is not None else QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.move(available.right() - self.width() - 15, available.bottom() - self.height() - 15)
        self.show()
        self.hide_timer.start()
        if sound:
            play_notification_sound()

    def _set_schedule_text(self, event):
        start = datetime.fromtimestamp(event["start_at"], CALENDAR_TIMEZONE)
        minutes = ceil((start - datetime.now(CALENDAR_TIMEZONE)).total_seconds() / 60)
        self.title_label.setText(event["title"])
        detail = f"시작: {start:%Y년 %m월 %d일 %H:%M} (한국 시간)"
        detail += f"\n시작까지 {minutes}분" if minutes > 0 else "\n시작 시각이 되었습니다."
        if event.get("all_day"):
            detail += "\n종일 일정 · 첫날 00:00 기준"
        if event.get("location"):
            detail += f"\n장소: {event['location']}"
        self.detail_label.setText(detail)

    def dismiss(self):
        visible = self.isVisible()
        self.hide_timer.stop()
        self.hide()
        if visible:
            self.dismissed.emit()

    def closeEvent(self, event):
        event.ignore()
        self.dismiss()

    def reject(self):
        self.dismiss()

    def _open_calendar(self):
        if self.current_event.get("action_type") == "alarm":
            return
        try:
            self.open_calendar(dict(self.current_event))
        except Exception:
            self.detail_label.setText(self.detail_label.text() + "\n캘린더를 열지 못했습니다.")
            return
        self.dismiss()


class CalendarNotificationManager(QObject):
    def __init__(self, owner, open_calendar):
        super().__init__(owner)
        self._queue = deque()
        self._closed = False
        self.notification = CalendarNotification(owner, open_calendar)
        self.notification.dismissed.connect(self._show_next)
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        if owner is not None:
            owner.destroyed.connect(self.shutdown)

    @property
    def is_busy(self):
        return not self._closed and (self.notification.isVisible() or bool(self._queue))

    def enqueue(self, event):
        if self._closed:
            return
        self._queue.append((dict(event), True))
        if not self.notification.isVisible():
            self._show_next()

    def _show_next(self):
        if self._queue:
            event, sound = self._queue.popleft()
            self.notification.show_event(event, sound=sound)

    def enqueue_alarm(self, event):
        if self._closed:
            return
        # Timers ring immediately; restore an interrupted card without a second chime.
        if self.notification.isVisible():
            self._queue.appendleft((dict(self.notification.current_event), False))
        alarm = dict(event, id=f"alarm:{event['job_id']}", action_type="alarm")
        self.notification.show_event(alarm)

    def discard_event(self, event_id):
        if self._closed:
            return
        self._queue = deque(item for item in self._queue if item[0]["id"] != event_id)
        if self.notification.isVisible() and self.notification.current_event["id"] == event_id:
            self.notification.dismiss()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._queue.clear()
        self.notification.hide_timer.stop()
        self.notification.hide()
        self.notification.deleteLater()
