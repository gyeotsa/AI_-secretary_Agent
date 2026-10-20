"""Nonmodal, queued questions that require an explicit user choice."""
from collections import deque
import logging

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QVBoxLayout,
)

from .calendar_notification import play_notification_sound
from .theme import set_widget_style

logger = logging.getLogger(__name__)


class RequestNotification(QDialog):
    answered = pyqtSignal(str, object)
    cancelled = pyqtSignal(str)

    def __init__(self, owner):
        # A native parent would hide the question when the app is minimized.
        super().__init__(None)
        self.owner = owner
        self.request_id = ""
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("아니스 · 확인 요청")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFixedWidth(380)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        self.title_label = QLabel()
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setAccessibleName("요청 제목")
        set_widget_style(self.title_label, "font-size: 16px; font-weight: 600;")
        root.addWidget(self.title_label)
        self.message_view = QTextBrowser()
        self.message_view.setFrameShape(QFrame.Shape.NoFrame)
        self.message_view.setOpenLinks(False)
        self.message_view.setOpenExternalLinks(False)
        self.message_view.setAccessibleName("요청 내용")
        self.message_view.setMinimumHeight(60)
        self.message_view.setMaximumHeight(150)
        root.addWidget(self.message_view)
        self.choice_layout = QVBoxLayout()
        self.choice_buttons = []
        root.addLayout(self.choice_layout)
        bottom = QHBoxLayout()
        self.queue_label = QLabel()
        self.queue_label.setObjectName("muted")
        bottom.addWidget(self.queue_label)
        bottom.addStretch()
        self.close_button = QPushButton("닫기 · 취소")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(lambda: self.cancelled.emit(self.request_id))
        bottom.addWidget(self.close_button)
        root.addLayout(bottom)
        set_widget_style(self, "QDialog { border: 1px solid #555555; border-radius: 10px; }")

    def show_request(self, request, pending_count):
        self.request_id = request["id"]
        self.title_label.setText(request["title"])
        self.message_view.setPlainText(request["message"])
        self.message_view.verticalScrollBar().setValue(0)
        for button in self.choice_buttons:
            self.choice_layout.removeWidget(button)
            button.hide()
            button.deleteLater()
        self.choice_buttons = []
        for label, value in request["choices"]:
            button = QPushButton(label)
            button.setAutoDefault(False)
            button.clicked.connect(
                lambda _checked=False, key=request["id"], answer=value: self.answered.emit(key, answer)
            )
            self.choice_layout.addWidget(button)
            self.choice_buttons.append(button)
        self.set_pending_count(pending_count)
        self.adjustSize()
        screen = self.owner.screen() if self.owner is not None else QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.move(available.right() - self.width() - 15, available.bottom() - self.height() - 15)
        self.show()
        play_notification_sound()

    def set_pending_count(self, count):
        self.queue_label.setText(f"대기 중인 요청 {count}개" if count else "")

    def closeEvent(self, event):
        event.ignore()
        self.cancelled.emit(self.request_id)

    def reject(self):
        self.cancelled.emit(self.request_id)


class RequestNotificationManager(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self._queue = deque()
        self._current = None
        self._closed = False
        self.notification = RequestNotification(owner)
        self.notification.answered.connect(self._answer)
        self.notification.cancelled.connect(self.cancel)
        self.validation_timer = QTimer(self)
        self.validation_timer.setInterval(200)
        self.validation_timer.timeout.connect(self._discard_invalid)
        QApplication.instance().aboutToQuit.connect(self.shutdown)
        if owner is not None:
            owner.destroyed.connect(self.shutdown)

    @property
    def is_busy(self):
        return not self._closed and (self._current is not None or bool(self._queue))

    def enqueue(self, request_id, title, message, choices, on_answer, on_cancel=None, is_valid=None):
        if self._closed or not request_id or self._contains(request_id):
            return False
        choices = list(choices)
        if not choices:
            raise ValueError("A request needs at least one explicit choice")
        self._queue.append({"id": request_id, "title": title, "message": message,
                            "choices": choices, "answer": on_answer, "cancel": on_cancel,
                            "valid": is_valid})
        self._show_next()
        return True

    def _contains(self, request_id):
        return ((self._current is not None and self._current["id"] == request_id)
                or any(request["id"] == request_id for request in self._queue))

    @staticmethod
    def _valid(request):
        try:
            return request["valid"] is None or bool(request["valid"]())
        except Exception:
            return False

    @staticmethod
    def _deliver(callback, *args):
        if callback is not None:
            try:
                callback(*args)
            except Exception as exc:
                logger.error("Request callback failed (%s)", type(exc).__name__)

    def _show_next(self):
        if self._closed:
            return
        while self._current is None and self._queue:
            request = self._queue.popleft()
            if not self._valid(request):
                self._deliver(request["cancel"])
                continue
            self._current = request
            self.notification.show_request(request, len(self._queue))
        self.notification.set_pending_count(len(self._queue))
        if self.is_busy and any(request["valid"] is not None
                                for request in [self._current, *self._queue] if request is not None):
            self.validation_timer.start()
        else:
            self.validation_timer.stop()

    def _answer(self, request_id, value):
        if self._current is None or self._current["id"] != request_id:
            return
        request = self._current
        if not self._valid(request):
            self.cancel(request_id)
            return
        self._finish(request["answer"], value)

    def _finish(self, callback, *args):
        self._current = None
        self.validation_timer.stop()
        self.notification.hide()
        self._deliver(callback, *args)
        self._show_next()

    def cancel(self, request_id):
        if self._current is not None and self._current["id"] == request_id:
            self._finish(self._current["cancel"])
            return
        for request in self._queue:
            if request["id"] == request_id:
                self._queue.remove(request)
                self._deliver(request["cancel"])
                self._show_next()
                return

    def _discard_invalid(self):
        for request in [self._current, *self._queue]:
            if request is not None and not self._valid(request):
                self.cancel(request["id"])

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        pending = [self._current, *self._queue]
        self._current = None
        self._queue.clear()
        self.validation_timer.stop()
        self.notification.hide()
        self.notification.deleteLater()
        for request in pending:
            if request is not None:
                self._deliver(request["cancel"])
