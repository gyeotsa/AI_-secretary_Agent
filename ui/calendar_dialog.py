"""Korean local calendar and schedule registration."""
from datetime import date, datetime, timedelta
import threading
import time
from uuid import uuid4

from PyQt6.QtCore import QObject, QDate, QDateTime, QLocale, QSignalBlocker, QTime, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat
from PyQt6.QtWidgets import (
    QCalendarWidget, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout,
)

from core.local_calendar import CALENDAR_TIMEZONE
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context
from .dialog_theme import apply_dark_dialog_theme
from .theme import palette, set_widget_style, theme_manager


# Daemon threads keep app exit safe even if a provider is slow to honour cancellation.
_ACTIVE_COMMENTS = set()


class _CalendarComment(QObject):
    ready = pyqtSignal(int, object)
    finished = pyqtSignal()

    def __init__(self, service, request_id, change, event, store):
        super().__init__()
        self.service, self.request_id = service, request_id
        self.change, self.event, self.store = change, dict(event), store
        self.context = TurnExecutionContext(turn_id=uuid4().hex, session_id="calendar-comment")
        self._thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self._thread.start()

    def cancel(self):
        self.context.cancel()

    def run(self):
        try:
            with bind_turn_context(self.context):
                result = self.service.comment(self.change, self.event, store=self.store)
                self.context.checkpoint()
            if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
                raise ValueError("Missing calendar comment")
        except ToolCancelledError:
            result = None
        except Exception:
            result = {"text": "일정 변경은 완료되었지만 모델 코멘트를 받지 못했습니다.",
                      "model": "", "available": False}
        try:
            if result is not None and not self.context.cancelled:
                self.ready.emit(self.request_id, result)
            self.finished.emit()
        except RuntimeError:
            # Qt may already be shut down when an unresponsive provider returns.
            pass


def _release_comment(worker):
    _ACTIVE_COMMENTS.discard(worker)
    worker.deleteLater()


def shutdown_calendar_comments(timeout=2.0):
    workers = tuple(_ACTIVE_COMMENTS)
    for worker in workers:
        worker.cancel()
    deadline = time.monotonic() + max(0.0, timeout)
    for worker in workers:
        worker._thread.join(max(0.0, deadline - time.monotonic()))


class _CalendarWidget(QCalendarWidget):
    def paintCell(self, painter, rect, day):
        painter.save()
        if (day.year(), day.month()) != (self.yearShown(), self.monthShown()):
            painter.setOpacity(painter.opacity() * .45)
        super().paintCell(painter, rect, day)
        painter.restore()


class ScheduleDialog(QDialog):
    def __init__(self, store, selected_date, parent=None):
        super().__init__(parent)
        self.store = store
        self.saved_event = None
        self.setWindowTitle("아니스 · 일정 등록")
        self.resize(520, 460)
        root = QVBoxLayout(self)
        heading = QLabel("일정 등록")
        heading.setObjectName("heading")
        root.addWidget(heading)
        subtitle = QLabel("한국 시간 (UTC+9)")
        subtitle.setObjectName("subtitle")
        root.addWidget(subtitle)
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("예: 치과 예약")
        form.addRow("제목 *", self.title_edit)
        self.start_edit = QDateTimeEdit(QDateTime(selected_date, QTime(9, 0)))
        self.end_edit = QDateTimeEdit(QDateTime(selected_date, QTime(10, 0)))
        self.start_label, self.end_label = QLabel("시작"), QLabel("종료")
        for label, edit in ((self.start_label, self.start_edit), (self.end_label, self.end_edit)):
            edit.setCalendarPopup(True)
            edit.setLocale(QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea))
            edit.setDisplayFormat("yyyy년 M월 d일 HH:mm")
            form.addRow(label, edit)
        self.all_day_check = QCheckBox("종일 일정")
        self.all_day_check.toggled.connect(self._toggle_all_day)
        form.addRow("", self.all_day_check)
        self.all_day_hint = QLabel("마지막 날짜도 일정에 포함됩니다.")
        self.all_day_hint.setObjectName("muted")
        self.all_day_hint.hide()
        form.addRow("", self.all_day_hint)
        reminders = QHBoxLayout()
        self.reminder_check = QCheckBox("일정 알림")
        self.reminder_check.setChecked(True)
        self.reminder_spin = QSpinBox()
        self.reminder_spin.setRange(0, 10080)
        self.reminder_spin.setValue(60)
        self.reminder_spin.setSuffix("분 전")
        self.reminder_spin.setAccessibleName("일정 시작 전 알림 시간")
        self.reminder_check.toggled.connect(self.reminder_spin.setEnabled)
        reminders.addWidget(self.reminder_check)
        reminders.addWidget(self.reminder_spin)
        form.addRow("", reminders)
        reminder_hint = QLabel("0분은 시작 시각에 알립니다. 아니스가 실행 중일 때 알림을 받습니다.")
        reminder_hint.setObjectName("muted")
        reminder_hint.setWordWrap(True)
        form.addRow("", reminder_hint)
        self.location_edit = QLineEdit()
        form.addRow("장소 (선택)", self.location_edit)
        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("일정에 필요한 메모")
        form.addRow("메모 (선택)", self.description_edit)
        root.addLayout(form)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        set_widget_style(self.status_label, "color: #ff9ca7;")
        root.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("등록")
        self.save_button.setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        apply_dark_dialog_theme(self)

    def _toggle_all_day(self, checked):
        self.start_label.setText("시작 날짜" if checked else "시작")
        self.end_label.setText("마지막 날짜" if checked else "종료")
        self.all_day_hint.setVisible(checked)
        self.all_day_hint.setText("마지막 날짜도 포함됩니다. 알림은 첫날 자정(00:00) 기준입니다.")
        for edit in (self.start_edit, self.end_edit):
            edit.setDisplayFormat("yyyy년 M월 d일" if checked else "yyyy년 M월 d일 HH:mm")

    def _save(self):
        title = self.title_edit.text().strip()
        if not title:
            self.status_label.setText("일정 제목을 입력하세요.")
            self.title_edit.setFocus()
            return
        all_day = self.all_day_check.isChecked()
        try:
            start = self.start_edit.dateTime().toPyDateTime().replace(tzinfo=CALENDAR_TIMEZONE)
            end = self.end_edit.dateTime().toPyDateTime().replace(tzinfo=CALENDAR_TIMEZONE)
            if all_day:
                start = start.replace(hour=0, minute=0, second=0, microsecond=0)
                end = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        except (OverflowError, ValueError):
            self.status_label.setText("지원하지 않는 일정 날짜 범위입니다.")
            return
        if end <= start:
            self.status_label.setText("마지막 날짜는 시작 날짜와 같거나 이후여야 합니다." if all_day
                                      else "종료 시간은 시작 시간보다 뒤여야 합니다.")
            self.end_edit.setFocus()
            return
        self.save_button.setEnabled(False)
        try:
            self.saved_event = self.store.add_event(title, start, end, all_day=all_day,
                                                    location=self.location_edit.text(),
                                                    description=self.description_edit.toPlainText(),
                                                    reminder_minutes=self.reminder_spin.value() if self.reminder_check.isChecked() else None)
        except Exception:
            self.status_label.setText("일정을 저장하지 못했습니다. 입력 내용과 저장소를 확인한 뒤 다시 시도하세요.")
            self.save_button.setEnabled(True)
            return
        self.accept()


class DeleteScheduleDialog(QDialog):
    def __init__(self, store, events, parent=None):
        super().__init__(parent)
        self.store = store
        self.events = {event["id"]: dict(event) for event in events}
        self.deleted_event = None
        self.already_missing = False
        self.setWindowTitle("아니스 · 일정 삭제")
        self.resize(520, 340)
        root = QVBoxLayout(self)
        heading = QLabel("삭제할 일정을 확인하세요.")
        heading.setObjectName("heading")
        root.addWidget(heading)
        self.event_combo = QComboBox()
        self.event_combo.setAccessibleName("삭제할 일정 선택")
        for event in events:
            start = datetime.fromtimestamp(event["start_at"], CALENDAR_TIMEZONE)
            self.event_combo.addItem(f"{start:%m월 %d일 %H:%M} · {event['title']}", event["id"])
        root.addWidget(self.event_combo)
        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setReadOnly(True)
        self.detail_edit.setAccessibleName("삭제할 일정의 제목 시간 장소")
        root.addWidget(self.detail_edit)
        warning = QLabel("선택한 일정과 해당 일정의 알림이 삭제됩니다.")
        warning.setWordWrap(True)
        root.addWidget(warning)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        set_widget_style(self.status_label, "color: #ff9ca7;")
        root.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.delete_button = buttons.addButton("일정 삭제", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.delete_button.setAutoDefault(False)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText("취소")
        cancel.setDefault(True)
        self.delete_button.clicked.connect(self._delete)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.event_combo.currentIndexChanged.connect(self._show_selected)
        self._show_selected()
        apply_dark_dialog_theme(self)

    def _show_selected(self, *_):
        event = self.events.get(self.event_combo.currentData())
        self.delete_button.setEnabled(event is not None)
        self.status_label.clear()
        if event is None:
            self.detail_edit.clear()
            return
        start = datetime.fromtimestamp(event["start_at"], CALENDAR_TIMEZONE)
        end = datetime.fromtimestamp(event["end_at"], CALENDAR_TIMEZONE)
        period = (f"{start:%Y년 %m월 %d일} ~ {end - timedelta(microseconds=1):%Y년 %m월 %d일} (종일)"
                  if event["all_day"] else f"{start:%Y년 %m월 %d일 %H:%M} ~ {end:%Y년 %m월 %d일 %H:%M}")
        detail = f"제목: {event['title']}\n시간: {period} · 한국 시간\n장소: {event.get('location') or '없음'}"
        if event.get("description"):
            detail += f"\n메모: {event['description']}"
        self.detail_edit.setPlainText(detail)

    def _delete(self):
        event_id = self.event_combo.currentData()
        if event_id not in self.events:
            return
        from core.permission import get_permission_manager
        if get_permission_manager().is_blocked("data_delete"):
            self.status_label.setText("데이터 삭제 권한이 차단되어 일정을 삭제할 수 없습니다. 권한 관리에서 허용해 주세요.")
            return
        self.delete_button.setEnabled(False)
        try:
            self.deleted_event = self.store.delete_event(event_id)
        except Exception:
            self.status_label.setText("일정을 삭제하지 못했습니다. 저장소를 확인한 뒤 다시 시도하세요.")
            self.delete_button.setEnabled(True)
            return
        self.already_missing = self.deleted_event is None
        self.accept()


class CalendarDialog(QDialog):
    schedule_changed = pyqtSignal(object)

    def __init__(self, store, parent=None, *, comment_service=None):
        super().__init__(parent)
        self.store = store
        self.comment_service = comment_service
        self._comment_request_id = 0
        self._comment_worker = None
        self._events = []
        self._marked_dates = set()
        self._selected_date = None
        self._load_failed = False
        self.setWindowTitle("아니스 · 내 캘린더")
        self.resize(760, 760)
        root = QVBoxLayout(self)
        title = QLabel("내 캘린더")
        title.setObjectName("heading")
        root.addWidget(title)
        subtitle = QLabel("외부 계정 없이 사용하는 개인 캘린더 · 한국 시간 (UTC+9)")
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        actions = QHBoxLayout()
        self.today_button = QPushButton("오늘")
        self.today_button.clicked.connect(self._show_today)
        actions.addWidget(self.today_button)
        self.refresh_button = QPushButton("새로고침")
        self.refresh_button.clicked.connect(self.refresh)
        actions.addWidget(self.refresh_button)
        self.add_button = QPushButton("일정 등록")
        self.add_button.clicked.connect(lambda: self._register_schedule(self.calendar.selectedDate()))
        actions.addWidget(self.add_button)
        self.delete_button = QPushButton("일정 삭제")
        self.delete_button.clicked.connect(self._delete_schedule)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        root.addLayout(actions)

        self.calendar = _CalendarWidget()
        self.calendar.setAccessibleName("내 캘린더 날짜 선택")
        self.calendar.setToolTip("날짜를 클릭하면 일정 등록 창이 열립니다.")
        self.calendar.setLocale(QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea))
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Sunday)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setGridVisible(True)
        today = datetime.now(CALENDAR_TIMEZONE).date()
        self.calendar.setSelectedDate(QDate(today.year, today.month, today.day))
        root.addWidget(self.calendar, 2)

        self.day_heading = QLabel()
        self.day_heading.setTextFormat(Qt.TextFormat.PlainText)
        set_widget_style(self.day_heading, "font-size: 17px; font-weight: 600;")
        root.addWidget(self.day_heading)
        self.agenda = QPlainTextEdit()
        self.agenda.setReadOnly(True)
        self.agenda.setAccessibleName("선택한 날짜의 일정")
        root.addWidget(self.agenda, 1)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self.comment_heading = QLabel("아니스 코멘트")
        self.comment_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.comment_heading.setWordWrap(True)
        root.addWidget(self.comment_heading)
        self.comment_edit = QPlainTextEdit()
        self.comment_edit.setReadOnly(True)
        self.comment_edit.setAccessibleName("일정 변경에 대한 아니스 코멘트")
        self.comment_edit.setMaximumHeight(130)
        self.comment_edit.setPlaceholderText("일정을 등록하거나 삭제하면 현재 모델의 코멘트가 표시됩니다.")
        root.addWidget(self.comment_edit)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        root.addWidget(close)

        apply_dark_dialog_theme(self)
        self.calendar.selectionChanged.connect(self._render_agenda)
        self.calendar.currentPageChanged.connect(self._page_changed)
        self.calendar.clicked.connect(self._register_schedule)
        theme_manager().changed.connect(self._apply_theme)
        self._apply_theme()
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _show_today(self):
        today = datetime.now(CALENDAR_TIMEZONE).date()
        with QSignalBlocker(self.calendar):
            self.calendar.setSelectedDate(QDate(today.year, today.month, today.day))
            self.calendar.showSelectedDate()
        self.refresh()

    def _page_changed(self, year, month):
        selected = self.calendar.selectedDate()
        if (selected.year(), selected.month()) != (year, month):
            first = QDate(year, month, 1)
            with QSignalBlocker(self.calendar):
                self.calendar.setSelectedDate(QDate(year, month, min(selected.day(), first.daysInMonth())))
        self.refresh()

    def refresh(self):
        self._clear_markers()
        try:
            start = date(self.calendar.yearShown(), self.calendar.monthShown(), 1)
            end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
            self._events = sorted(self.store.list_events(start, end), key=lambda row: row["start_at"])
            self._load_failed = False
            self._mark_dates()
            self._render_agenda()
        except Exception:
            self._events = []
            self._load_failed = True
            self._clear_markers()
            self._render_agenda()

    def _register_schedule(self, selected_date):
        dialog = ScheduleDialog(self.store, selected_date, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            start = datetime.fromtimestamp(dialog.saved_event["start_at"], CALENDAR_TIMEZONE).date()
            with QSignalBlocker(self.calendar):
                self.calendar.setSelectedDate(QDate(start.year, start.month, start.day))
                self.calendar.showSelectedDate()
            self.refresh()
            self._schedule_changed("added", dialog.saved_event)
        finally:
            dialog.deleteLater()

    def _delete_schedule(self):
        if self._load_failed:
            return
        selected = self.calendar.selectedDate().toPyDate()
        events = [event for event in self._events
                  if self._event_span(event)[0].date() <= selected <= self._event_span(event)[2]]
        if not events:
            return
        dialog = DeleteScheduleDialog(self.store, events, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self.refresh()
            if dialog.deleted_event is not None:
                self._schedule_changed("deleted", dialog.deleted_event)
            elif dialog.already_missing and not self._load_failed:
                self.status_label.setText("이미 삭제된 일정입니다. 캘린더를 새로고침했습니다.")
        finally:
            dialog.deleteLater()

    def _schedule_changed(self, change, event):
        if self._comment_worker is not None:
            self._comment_worker.cancel()
        self._comment_request_id += 1
        request_id = self._comment_request_id
        self.schedule_changed.emit({"change": change, "event": dict(event)})
        action = "등록" if change == "added" else "삭제"
        self.comment_heading.setText(f"아니스 코멘트 · {event['title']} {action}")
        if self.comment_service is None:
            self.comment_edit.setPlainText("일정 변경은 완료되었습니다. 현재 모델 코멘트를 사용할 수 없습니다.")
            return
        self.comment_edit.setPlainText("일정 변경이 완료되었습니다. 현재 모델이 일정을 살펴보고 있습니다…")
        worker = _CalendarComment(self.comment_service, request_id, change, event, self.store)
        self._comment_worker = worker
        _ACTIVE_COMMENTS.add(worker)
        worker.ready.connect(self._comment_ready)
        worker.finished.connect(lambda: _release_comment(worker))
        worker.start()

    def _comment_ready(self, request_id, result):
        if request_id != self._comment_request_id:
            return
        text = result["text"]
        if result.get("facts"):
            text = result["facts"] + "\n\nAI 의견: " + text
        self.comment_edit.setPlainText(text)
        if result.get("available") is True and result.get("model"):
            self.comment_heading.setText(self.comment_heading.text() + f" · {result['model']}")

    @staticmethod
    def _event_span(event):
        start = datetime.fromtimestamp(event["start_at"], CALENDAR_TIMEZONE)
        end = datetime.fromtimestamp(event["end_at"], CALENDAR_TIMEZONE)
        # Midnight ends belong to the preceding day, just like the store's half-open query.
        return start, end, (end - timedelta(microseconds=1)).date()

    def _clear_markers(self):
        for marked in self._marked_dates:
            self.calendar.setDateTextFormat(QDate(marked.year, marked.month, marked.day), QTextCharFormat())
        self._marked_dates.clear()

    def _mark_dates(self):
        first = date(self.calendar.yearShown(), self.calendar.monthShown(), 1)
        last = date(first.year, first.month, QDate(first.year, first.month, 1).daysInMonth())
        for event in self._events:
            start, _end, last_day = self._event_span(event)
            current, limit = max(first, start.date()), min(last, last_day)
            while current <= limit:
                self._marked_dates.add(current)
                current += timedelta(days=1)
        for marked in self._marked_dates:
            day = QDate(marked.year, marked.month, marked.day)
            self.calendar.setDateTextFormat(day, self._date_format(day))

    def _date_format(self, day):
        colors = theme_manager().colors
        result = QTextCharFormat()
        if date(day.year(), day.month(), day.day()) in self._marked_dates:
            result.setFontWeight(QFont.Weight.Bold)
            result.setFontUnderline(True)
            result.setForeground(QColor(colors["accent"]))
        if day == self.calendar.selectedDate():
            result.setBackground(QColor(colors["selected"]))
            result.setForeground(QColor(colors["selected_text"]))
        return result

    def _style_selection(self):
        selected = self.calendar.selectedDate()
        if self._selected_date is not None and self._selected_date != selected:
            self.calendar.setDateTextFormat(self._selected_date, self._date_format(self._selected_date))
        self.calendar.setDateTextFormat(selected, self._date_format(selected))
        self._selected_date = selected

    def _render_agenda(self):
        self._style_selection()
        selected = self.calendar.selectedDate()
        self.day_heading.setText(self.calendar.locale().toString(selected, "yyyy년 M월 d일 dddd"))
        set_widget_style(self.status_label, "color: #ff9ca7;" if self._load_failed else "")
        if self._load_failed:
            self.delete_button.setEnabled(False)
            self.agenda.clear()
            self.status_label.setText("일정을 불러오지 못했습니다. 새로고침으로 다시 시도하세요.")
            return
        selected_day = date(selected.year(), selected.month(), selected.day())
        entries = []
        for event in self._events:
            start, end, last_day = self._event_span(event)
            if not start.date() <= selected_day <= last_day:
                continue
            if event["all_day"]:
                time = "종일"
                if start.date() != last_day:
                    time += f" · {start:%Y.%m.%d} ~ {last_day:%Y.%m.%d}"
            elif start.date() == end.date():
                time = f"{start:%H:%M} ~ {end:%H:%M}"
            else:
                time = f"{start:%Y.%m.%d %H:%M} ~ {end:%Y.%m.%d %H:%M}"
            text = f"{time}  {event['title']}"
            reminder = event.get("reminder_minutes", 60)
            alert = ("없음" if reminder is None else "시작 시각" if reminder == 0
                     else "1시간 전" if reminder == 60 else f"{reminder}분 전")
            text += f"\n알림: {alert}"
            if event["all_day"] and reminder is not None:
                text += " (첫날 00:00 기준)"
            if event.get("location"):
                text += f"\n장소: {event['location']}"
            if event.get("description"):
                text += f"\n{event['description']}"
            entries.append(text)
        self.agenda.setPlainText("\n\n".join(entries) if entries else "등록된 일정이 없습니다.")
        self.delete_button.setEnabled(bool(entries))
        self.status_label.setText(f"선택한 날짜의 일정 {len(entries)}건 · 일정이 있는 날짜는 강조 표시됩니다.")

    def _apply_theme(self, *_):
        colors = theme_manager().colors
        self.calendar.setPalette(palette(colors))
        self.calendar.setStyleSheet(f"""
            QCalendarWidget QAbstractItemView {{ background: {colors['input']}; color: {colors['text']};
                selection-background-color: {colors['selected']}; selection-color: {colors['selected_text']};
                gridline-color: {colors['border']}; }}
            QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {colors['surface']}; }}
        """)
        header = QTextCharFormat()
        header.setForeground(QColor(colors["text"]))
        header.setBackground(QColor(colors["surface"]))
        self.calendar.setHeaderTextFormat(header)
        for weekday in Qt.DayOfWeek:
            day = QTextCharFormat()
            day.setForeground(QColor(colors["text"]))
            self.calendar.setWeekdayTextFormat(weekday, day)
        self._clear_markers()
        self._mark_dates()
        self._style_selection()
