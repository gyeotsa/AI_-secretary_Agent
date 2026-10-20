"""Isolated calendar UI checks; no real schedules or external services."""
import os
import threading
from types import SimpleNamespace
from datetime import date, datetime, time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QDate, QDateTime, QLocale, QTime, Qt, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QCalendarWidget, QDialog, QDialogButtonBox, QTableView, QWidget

from core.local_calendar import CALENDAR_TIMEZONE, LocalCalendarStore
from ui.calendar_dialog import CalendarDialog, DeleteScheduleDialog, ScheduleDialog
from ui.theme import theme_manager


class Store:
    def __init__(self):
        self.events = []
        self.calls = []
        self.add_calls = []
        self.error = False
        self.add_error = False
        self.delete_error = False
        self.delete_calls = []

    def list_events(self, start, end):
        self.calls.append((start, end))
        if self.error:
            raise RuntimeError("private-path-and-token-marker")
        start_at = datetime.combine(start, time.min, CALENDAR_TIMEZONE).timestamp()
        end_at = datetime.combine(end, time.min, CALENDAR_TIMEZONE).timestamp()
        return [event for event in self.events if event["start_at"] < end_at and event["end_at"] > start_at]

    def add_event(self, title, start, end, **kwargs):
        self.add_calls.append((title, start, end, kwargs))
        if self.add_error:
            raise RuntimeError("private-path-and-token-marker")
        saved = {"id": str(len(self.events)), "title": title, "start_at": int(start.timestamp()),
                 "end_at": int(end.timestamp()), **kwargs}
        self.events.append(saved)
        return saved

    def delete_event(self, event_id):
        self.delete_calls.append(event_id)
        if self.delete_error:
            raise RuntimeError("private-path-and-token-marker")
        deleted = next((row for row in self.events if row["id"] == event_id), None)
        if deleted is not None:
            self.events.remove(deleted)
        return deleted


def event(title, start, end, *, all_day=False, location="", description=""):
    return {"id": title, "title": title,
            "start_at": int(datetime.fromisoformat(start).replace(tzinfo=CALENDAR_TIMEZONE).timestamp()),
            "end_at": int(datetime.fromisoformat(end).replace(tzinfo=CALENDAR_TIMEZONE).timestamp()),
            "all_day": all_day, "location": location, "description": description}


@pytest.fixture(scope="module", autouse=True)
def app():
    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture(autouse=True)
def permissions(tmp_path, monkeypatch):
    from core.permission import PermissionManager
    manager = PermissionManager(str(tmp_path / "permissions.json"))
    monkeypatch.setattr("core.permission.get_permission_manager", lambda: manager)
    return manager


@pytest.fixture
def calendar(monkeypatch, app):
    class KoreanClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == CALENDAR_TIMEZONE
            return cls(2026, 10, 10, 0, 15, tzinfo=tz)

    monkeypatch.setattr("ui.calendar_dialog.datetime", KoreanClock)
    store = Store()
    dialog = CalendarDialog(store)
    yield dialog, store
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


@pytest.fixture
def schedule(app):
    store = Store()
    dialog = ScheduleDialog(store, QDate(2026, 10, 25))
    yield dialog, store
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_korean_today_month_query_and_read_only_empty_state(calendar):
    dialog, store = calendar
    assert dialog.calendar.selectedDate() == QDate(2026, 10, 10)
    assert dialog.calendar.locale().language() == QLocale.Language.Korean
    assert dialog.calendar.firstDayOfWeek() == Qt.DayOfWeek.Sunday
    assert store.calls[-1] == (date(2026, 10, 1), date(2026, 11, 1))
    assert dialog.agenda.isReadOnly()
    assert dialog.agenda.toPlainText() == "등록된 일정이 없습니다."
    assert "2026년 10월 10일" in dialog.day_heading.text()
    assert "0건" in dialog.status_label.text()


def test_native_month_year_navigation_and_today(calendar):
    dialog, store = calendar
    dialog.calendar.setSelectedDate(QDate(2026, 10, 31))
    dialog.calendar.showNextMonth()
    assert dialog.calendar.selectedDate() == QDate(2026, 11, 30)
    assert store.calls[-1] == (date(2026, 11, 1), date(2026, 12, 1))
    dialog.calendar.setCurrentPage(2025, 12)
    assert dialog.calendar.selectedDate() == QDate(2025, 12, 30)
    assert store.calls[-1] == (date(2025, 12, 1), date(2026, 1, 1))
    dialog.today_button.click()
    assert dialog.calendar.selectedDate() == QDate(2026, 10, 10)
    assert (dialog.calendar.yearShown(), dialog.calendar.monthShown()) == (2026, 10)


def test_multi_day_midnight_boundaries_and_chronological_plain_text(calendar):
    dialog, store = calendar
    store.events = [
        event("밤 일정", "2026-10-10T23:00", "2026-10-12T00:00"),
        event("<b>종일 일정</b>", "2026-10-09T00:00", "2026-10-11T00:00", all_day=True,
              location="<i>서울</i>", description="<img src=x>\n회의 메모"),
        event("치과", "2026-10-10T17:00", "2026-10-10T18:00"),
    ]
    dialog.refresh_button.click()
    text = dialog.agenda.toPlainText()
    assert text.index("<b>종일 일정</b>") < text.index("치과") < text.index("밤 일정")
    assert "장소: <i>서울</i>\n<img src=x>\n회의 메모" in text
    assert "17:00 ~ 18:00" in text
    assert "종일 · 2026.10.09 ~ 2026.10.10" in text
    assert dialog._marked_dates == {date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 11)}
    assert dialog.calendar.dateTextFormat(QDate(2026, 10, 11)).fontUnderline()
    assert not dialog.calendar.dateTextFormat(QDate(2026, 10, 12)).fontUnderline()
    dialog.calendar.setSelectedDate(QDate(2026, 10, 11))
    assert "밤 일정" in dialog.agenda.toPlainText()
    assert "종일 일정" not in dialog.agenda.toPlainText()
    dialog.calendar.setSelectedDate(QDate(2026, 10, 12))
    assert dialog.agenda.toPlainText() == "등록된 일정이 없습니다."


def test_month_boundary_marks_only_the_displayed_month(calendar):
    dialog, store = calendar
    store.events = [event("월 경계", "2026-10-31T23:00", "2026-11-02T00:00")]
    dialog.calendar.setSelectedDate(QDate(2026, 10, 31))
    dialog.refresh()
    assert dialog._marked_dates == {date(2026, 10, 31)}
    dialog.calendar.showNextMonth()
    assert dialog._marked_dates == {date(2026, 11, 1)}
    assert not dialog.calendar.dateTextFormat(QDate(2026, 10, 31)).fontUnderline()
    dialog.calendar.setSelectedDate(QDate(2026, 11, 1))
    assert "월 경계" in dialog.agenda.toPlainText()
    dialog.calendar.setSelectedDate(QDate(2026, 11, 2))
    assert dialog.agenda.toPlainText() == "등록된 일정이 없습니다."


def test_refresh_failure_clears_stale_data_and_recovers(calendar):
    dialog, store = calendar
    store.events = [event("다시 보일 일정", "2026-10-10T09:00", "2026-10-10T10:00")]
    dialog.refresh()
    assert "다시 보일 일정" in dialog.agenda.toPlainText()
    store.error = True
    dialog.refresh_button.click()
    assert dialog.agenda.toPlainText() == ""
    assert dialog._marked_dates == set()
    assert not dialog.calendar.dateTextFormat(QDate(2026, 10, 10)).fontUnderline()
    assert "불러오지 못했습니다" in dialog.status_label.text()
    assert "0건" not in dialog.status_label.text()
    assert "private-path-and-token-marker" not in dialog.status_label.text()
    store.error = False
    dialog.refresh_button.click()
    assert "다시 보일 일정" in dialog.agenda.toPlainText()
    assert "1건" in dialog.status_label.text()
    store.events.clear()
    dialog.refresh()
    assert dialog.agenda.toPlainText() == "등록된 일정이 없습니다."
    assert not dialog._marked_dates


def test_reopen_preserves_selection_and_refreshes(calendar, app):
    dialog, store = calendar
    dialog.show()
    dialog.calendar.setSelectedDate(QDate(2027, 1, 25))
    dialog.close()
    store.events = [event("새 일정", "2027-01-25T17:00", "2027-01-25T18:00")]
    dialog.show()
    app.processEvents()
    assert dialog.calendar.selectedDate() == QDate(2027, 1, 25)
    assert (dialog.calendar.yearShown(), dialog.calendar.monthShown()) == (2027, 1)
    assert "새 일정" in dialog.agenda.toPlainText()


def test_live_theme_keeps_markers_readable_without_requery(calendar, app):
    dialog, store = calendar
    store.events = [event("테마 일정", "2026-10-10T09:00", "2026-10-10T10:00")]
    dialog.refresh()
    queries = len(store.calls)
    manager = theme_manager()
    original = manager.mode
    try:
        for mode in ("light", "dark"):
            manager.set_mode(mode, persist=False)
            app.processEvents()
            assert dialog.palette().color(QPalette.ColorRole.Window).name() == manager.colors["window"]
            assert dialog.calendar.dateTextFormat(QDate(2026, 10, 10)).foreground().color().name() == manager.colors["selected_text"]
            assert dialog.calendar.dateTextFormat(QDate(2026, 10, 10)).fontUnderline()
            assert "테마 일정" in dialog.agenda.toPlainText()
        assert len(store.calls) == queries
    finally:
        manager.set_mode(original, persist=False)


def test_selection_keeps_explicit_color_without_focus_and_restores_previous_marker(calendar, app):
    dialog, store = calendar
    store.events = [event("표시할 일정", "2026-10-10T09:00", "2026-10-10T10:00")]
    dialog.refresh()
    dialog.show()
    manager = theme_manager()
    original = manager.mode
    try:
        for mode in ("light", "dark"):
            manager.set_mode(mode, persist=False)
            dialog.calendar.setSelectedDate(QDate(2026, 10, 10))
            dialog.today_button.setFocus()
            app.processEvents()
            selected = dialog.calendar.dateTextFormat(QDate(2026, 10, 10))
            assert selected.background().color().name() == manager.colors["selected"]
            assert selected.foreground().color().name() == manager.colors["selected_text"]
            assert selected.fontUnderline()
            dialog.calendar.setSelectedDate(QDate(2026, 10, 25))
            old = dialog.calendar.dateTextFormat(QDate(2026, 10, 10))
            assert old.background().style() == Qt.BrushStyle.NoBrush
            assert old.foreground().color().name() == manager.colors["accent"]
            assert old.fontUnderline()
            assert dialog.calendar.dateTextFormat(QDate(2026, 10, 25)).background().color().name() == manager.colors["selected"]
        dialog.calendar.showNextMonth()
        assert dialog.calendar.dateTextFormat(QDate(2026, 10, 25)).background().style() == Qt.BrushStyle.NoBrush
        assert dialog.calendar.dateTextFormat(QDate(2026, 11, 25)).background().color().name() == manager.colors["selected"]
    finally:
        manager.set_mode(original, persist=False)


def test_adjacent_dates_dim_and_restore_with_month_and_theme(calendar, monkeypatch):
    dialog, store = calendar
    store.events = [event("월 경계 일정", "2026-10-31T23:00", "2026-11-02T00:00")]
    dialog.refresh()
    painted = []
    monkeypatch.setattr(QCalendarWidget, "paintCell", lambda self, painter, rect, day: painted.append((day, painter.opacity())))

    class Painter:
        def __init__(self):
            self.value = 1.0

        def save(self):
            self.saved = self.value

        def restore(self):
            self.value = self.saved

        def opacity(self):
            return self.value

        def setOpacity(self, value):
            self.value = value

    painter = Painter()
    manager = theme_manager()
    original = manager.mode
    try:
        for mode in ("light", "dark"):
            dialog.calendar.setCurrentPage(2026, 10)
            manager.set_mode(mode, persist=False)
            for day in (QDate(2026, 9, 27), QDate(2026, 11, 7)):
                dialog.calendar.paintCell(painter, None, day)
                assert painted[-1] == (day, .45)
                assert painter.opacity() == 1.0
            dialog.calendar.paintCell(painter, None, QDate(2026, 10, 9))
            assert painted[-1] == (QDate(2026, 10, 9), 1.0)
            assert dialog.calendar.dateTextFormat(QDate(2026, 10, 9)).foreground().style() == Qt.BrushStyle.NoBrush
            assert dialog.calendar.dateTextFormat(QDate(2026, 10, 31)).fontUnderline()
            dialog.calendar.setSelectedDate(QDate(2026, 11, 1))
            old = dialog.calendar.dateTextFormat(QDate(2026, 10, 31))
            assert not old.fontUnderline()
            dialog.calendar.paintCell(painter, None, QDate(2026, 10, 31))
            assert painted[-1] == (QDate(2026, 10, 31), .45)
            dialog.calendar.paintCell(painter, None, QDate(2026, 11, 7))
            assert painted[-1] == (QDate(2026, 11, 7), 1.0)
            assert dialog.calendar.dateTextFormat(QDate(2026, 11, 7)).foreground().style() == Qt.BrushStyle.NoBrush
            selected = dialog.calendar.dateTextFormat(QDate(2026, 11, 1))
            assert selected.background().color().name() == manager.colors["selected"]
            assert selected.fontUnderline()
    finally:
        manager.set_mode(original, persist=False)


def test_only_actual_date_click_and_registration_button_open_modal(calendar, monkeypatch, app):
    dialog, _store = calendar
    opened = []

    def reject_modal(modal):
        opened.append(modal.start_edit.date())
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ScheduleDialog, "exec", reject_modal)
    dialog.calendar.setSelectedDate(QDate(2026, 10, 25))
    dialog.calendar.showNextMonth()
    dialog.today_button.click()
    assert not opened
    dialog.show()
    app.processEvents()
    view = dialog.calendar.findChild(QTableView)
    target = next(view.model().index(row, column)
                  for row in range(1, view.model().rowCount())
                  for column in range(view.model().columnCount())
                  if str(view.model().data(view.model().index(row, column))) == "25")
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(target).center())
    assert opened == [QDate(2026, 10, 25)]
    assert dialog.calendar.selectedDate() == QDate(2026, 10, 25)
    dialog.add_button.click()
    assert opened == [QDate(2026, 10, 25), QDate(2026, 10, 25)]


def test_schedule_defaults_validation_and_plain_text_save(schedule):
    dialog, store = schedule
    assert dialog.start_edit.date() == dialog.end_edit.date() == QDate(2026, 10, 25)
    assert dialog.start_edit.time() == QTime(9, 0)
    assert dialog.end_edit.time() == QTime(10, 0)
    dialog.save_button.click()
    assert "제목" in dialog.status_label.text() and not store.add_calls
    dialog.title_edit.setText(" <b>치과 예약</b> ")
    dialog.end_edit.setTime(QTime(9, 0))
    dialog.save_button.click()
    assert "종료 시간" in dialog.status_label.text() and not store.add_calls
    dialog.end_edit.setTime(QTime(10, 0))
    dialog.location_edit.setText("<i>치과</i>")
    dialog.description_edit.setPlainText("<img src=x>\n예약 메모")
    dialog.save_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    title, start, end, options = store.add_calls[0]
    assert title == "<b>치과 예약</b>"
    assert start == datetime(2026, 10, 25, 9, 0, tzinfo=CALENDAR_TIMEZONE)
    assert end == datetime(2026, 10, 25, 10, 0, tzinfo=CALENDAR_TIMEZONE)
    assert options == {"all_day": False, "location": "<i>치과</i>", "description": "<img src=x>\n예약 메모", "reminder_minutes": 60}
    assert dialog.saved_event == store.events[0]
    dialog.save_button.click()
    assert len(store.add_calls) == 1


def test_schedule_cancel_never_writes_and_database_error_keeps_inputs(schedule):
    dialog, store = schedule
    dialog.title_edit.setText("유지할 일정")
    store.add_error = True
    dialog.show()
    dialog.save_button.click()
    assert dialog.isVisible() and dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.title_edit.text() == "유지할 일정"
    assert dialog.saved_event is None and not store.events
    assert dialog.save_button.isEnabled()
    assert "저장하지 못했습니다" in dialog.status_label.text()
    assert "private-path-and-token-marker" not in dialog.status_label.text()
    dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
    assert dialog.result() == QDialog.DialogCode.Rejected and not store.events


def test_all_day_last_date_is_inclusive_and_invalid_range_is_rejected(schedule):
    dialog, store = schedule
    dialog.title_edit.setText("휴가")
    dialog.all_day_check.setChecked(True)
    assert dialog.end_label.text() == "마지막 날짜"
    assert "HH:mm" not in dialog.end_edit.displayFormat()
    dialog.end_edit.setDate(QDate(2026, 10, 24))
    dialog.save_button.click()
    assert "마지막 날짜" in dialog.status_label.text() and not store.add_calls
    dialog.end_edit.setDate(QDate(2026, 10, 27))
    dialog.save_button.click()
    _title, start, end, options = store.add_calls[0]
    assert start == datetime(2026, 10, 25, tzinfo=CALENDAR_TIMEZONE)
    assert end == datetime(2026, 10, 28, tzinfo=CALENDAR_TIMEZONE)
    assert options["all_day"] is True


def test_saved_modal_selects_start_day_refreshes_and_presents_real_row(calendar, monkeypatch):
    dialog, store = calendar

    def save_modal(modal):
        modal.title_edit.setText("다른 날짜에 등록")
        modal.start_edit.setDateTime(QDateTime(QDate(2027, 1, 25), QTime(17, 0)))
        modal.end_edit.setDateTime(QDateTime(QDate(2027, 1, 25), QTime(18, 0)))
        modal.save_button.click()
        return modal.result()

    monkeypatch.setattr(ScheduleDialog, "exec", save_modal)
    dialog.add_button.click()
    assert len(store.events) == 1
    assert dialog.calendar.selectedDate() == QDate(2027, 1, 25)
    assert "17:00 ~ 18:00  다른 날짜에 등록" in dialog.agenda.toPlainText()
    assert dialog._marked_dates == {date(2027, 1, 25)}


def test_schedule_inherits_and_switches_theme(schedule):
    dialog, _store = schedule
    manager = theme_manager()
    original = manager.mode
    try:
        for mode in ("light", "dark"):
            manager.set_mode(mode, persist=False)
            assert dialog.palette().color(QPalette.ColorRole.Window).name() == manager.colors["window"]
            assert dialog.save_button.text() == "등록"
    finally:
        manager.set_mode(original, persist=False)


@pytest.mark.parametrize("all_day", [False, True])
def test_schedule_persists_to_real_isolated_store_and_reload_agenda(tmp_path, app, all_day):
    db_path = tmp_path / "calendar.db"
    store = LocalCalendarStore(db_path)
    registration = ScheduleDialog(store, QDate(2026, 10, 25))
    calendar = None
    try:
        registration.title_edit.setText("저장 후 다시 확인")
        registration.all_day_check.setChecked(all_day)
        registration.save_button.click()
        assert registration.result() == QDialog.DialogCode.Accepted
        reloaded = LocalCalendarStore(db_path)
        assert reloaded.get_event(registration.saved_event["id"]) == registration.saved_event
        calendar = CalendarDialog(reloaded)
        calendar.calendar.setSelectedDate(QDate(2026, 10, 25))
        calendar.refresh()
        assert "저장 후 다시 확인" in calendar.agenda.toPlainText()
        assert ("종일" if all_day else "09:00 ~ 10:00") in calendar.agenda.toPlainText()
        assert date(2026, 10, 25) in calendar._marked_dates
        calendar.calendar.setSelectedDate(QDate(2026, 10, 26))
        assert calendar.agenda.toPlainText() == "등록된 일정이 없습니다."
    finally:
        registration.close()
        registration.deleteLater()
        if calendar is not None:
            calendar.close()
            calendar.deleteLater()
        app.processEvents()


def test_all_day_date_overflow_is_visible_and_never_writes(schedule):
    dialog, store = schedule
    dialog.title_edit.setText("날짜 범위")
    dialog.all_day_check.setChecked(True)
    dialog.start_edit.setDate(QDate(9999, 12, 31))
    dialog.end_edit.setDate(QDate(9999, 12, 31))
    dialog.save_button.click()
    assert "지원하지 않는" in dialog.status_label.text()
    assert not store.add_calls and dialog.saved_event is None


@pytest.mark.parametrize("minutes", [None, 0, 15, 60, 10080])
def test_schedule_reminder_controls_and_saved_setting(schedule, minutes):
    dialog, store = schedule
    assert dialog.reminder_check.isChecked() and dialog.reminder_spin.value() == 60
    assert (dialog.reminder_spin.minimum(), dialog.reminder_spin.maximum()) == (0, 10080)
    assert dialog.reminder_spin.suffix() == "분 전"
    dialog.title_edit.setText("알림 설정 일정")
    if minutes is None:
        dialog.reminder_check.setChecked(False)
        assert not dialog.reminder_spin.isEnabled()
    else:
        dialog.reminder_spin.setValue(minutes)
    dialog.save_button.click()
    assert store.add_calls[0][3]["reminder_minutes"] == minutes


@pytest.mark.parametrize("minutes,label", [(None, "없음"), (0, "시작 시각"), (15, "15분 전"), (60, "1시간 전")])
def test_agenda_shows_actual_reminder_setting(calendar, minutes, label):
    dialog, store = calendar
    store.events = [{**event("알림 확인", "2026-10-10T09:00", "2026-10-10T10:00"), "reminder_minutes": minutes}]
    dialog.refresh()
    assert f"알림: {label}" in dialog.agenda.toPlainText()


def test_all_day_and_existing_rows_explain_reminder_time(calendar, schedule):
    dialog, store = calendar
    store.events = [event("기존 종일 일정", "2026-10-10T00:00", "2026-10-11T00:00", all_day=True)]
    dialog.refresh()
    assert "알림: 1시간 전 (첫날 00:00 기준)" in dialog.agenda.toPlainText()
    registration, _store = schedule
    registration.all_day_check.setChecked(True)
    assert "첫날 자정(00:00) 기준" in registration.all_day_hint.text()


def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "Calendar comment worker did not finish"


def test_delete_modal_uses_exact_id_and_plain_confirmation_for_duplicate_titles(app):
    store = Store()
    first = event("<b>회의</b>", "2026-10-10T09:00", "2026-10-10T10:00", location="<i>서울</i>")
    second = {**first, "id": "distinct-id", "location": "부산"}
    store.events = [first, second]
    modal = DeleteScheduleDialog(store, store.events)
    try:
        assert modal.detail_edit.isReadOnly()
        assert "제목: <b>회의</b>" in modal.detail_edit.toPlainText()
        assert "장소: <i>서울</i>" in modal.detail_edit.toPlainText()
        assert "2026년 10월 10일 09:00" in modal.detail_edit.toPlainText()
        modal.event_combo.setCurrentIndex(1)
        assert modal.event_combo.currentData() == "distinct-id"
        assert "장소: 부산" in modal.detail_edit.toPlainText()
        modal.delete_button.click()
        assert modal.result() == QDialog.DialogCode.Accepted
        assert store.events == [first] and modal.deleted_event == second
        assert store.delete_calls == ["distinct-id"]
        modal.delete_button.click()
        assert len(store.delete_calls) == 1
    finally:
        modal.close()
        modal.deleteLater()


def test_delete_cancel_error_and_missing_never_report_a_deleted_row(app):
    store = Store()
    row = event("유지할 일정", "2026-10-10T09:00", "2026-10-10T10:00")
    store.events = [row]
    modal = DeleteScheduleDialog(store, store.events)
    try:
        modal.show()
        store.delete_error = True
        modal.delete_button.click()
        assert modal.isVisible() and modal.deleted_event is None and store.events == [row]
        assert modal.delete_button.isEnabled()
        assert "삭제하지 못했습니다" in modal.status_label.text()
        assert "private-path-and-token-marker" not in modal.status_label.text()
        modal.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel).click()
        assert modal.result() == QDialog.DialogCode.Rejected and store.events == [row]
    finally:
        modal.close()
        modal.deleteLater()
    store.delete_error = False
    missing = DeleteScheduleDialog(store, [row])
    store.events.clear()
    try:
        missing.delete_button.click()
        assert missing.already_missing and missing.deleted_event is None
    finally:
        missing.close()
        missing.deleteLater()


def test_explicit_data_delete_block_prevents_calendar_ui_deletion(app, permissions):
    store = Store()
    row = event("유지할 일정", "2026-10-10T09:00", "2026-10-10T10:00")
    store.events = [row]
    modal = DeleteScheduleDialog(store, store.events)
    try:
        permissions.revoke_permission("data_delete")
        modal.delete_button.click()
        assert store.events == [row] and store.delete_calls == []
        assert "권한이 차단" in modal.status_label.text()
        permissions.grant_permission("data_delete")
        modal.delete_button.click()
        assert store.events == [] and store.delete_calls == [row["id"]]
        assert modal.result() == QDialog.DialogCode.Accepted
    finally:
        modal.close()
        modal.deleteLater()


def test_registration_comment_is_asynchronous_plain_text_and_keeps_saved_result(calendar, monkeypatch):
    dialog, store = calendar
    calls, changes = [], []
    main_thread = threading.get_ident()
    release = threading.Event()

    def comment(change, row, *, store):
        calls.append((change, row, store, threading.get_ident()))
        assert release.wait(2)
        return {"text": "<b>이동 시간을 확인하세요.</b>", "model": "현재 모델", "available": True}

    dialog.comment_service = SimpleNamespace(comment=comment)
    dialog.schedule_changed.connect(changes.append)

    def save_modal(modal):
        modal.title_edit.setText("새 일정")
        modal.save_button.click()
        return modal.result()

    monkeypatch.setattr(ScheduleDialog, "exec", save_modal)
    try:
        dialog.add_button.click()
        assert "새 일정" in dialog.agenda.toPlainText()
        assert "살펴보고 있습니다" in dialog.comment_edit.toPlainText()
        assert changes == [{"change": "added", "event": store.events[0]}]
        wait_until(lambda: bool(calls))
        assert calls[0][:3] == ("added", store.events[0], store)
        assert calls[0][3] != main_thread
    finally:
        release.set()
    wait_until(lambda: "<b>" in dialog.comment_edit.toPlainText())
    assert dialog.comment_edit.isReadOnly()
    assert dialog.comment_edit.toPlainText() == "<b>이동 시간을 확인하세요.</b>"
    assert "현재 모델" in dialog.comment_heading.text()
    assert "새 일정" in dialog.agenda.toPlainText()


def test_delete_completed_refreshes_and_model_failure_does_not_restore_event(calendar, monkeypatch):
    dialog, store = calendar
    row = event("삭제할 일정", "2026-10-10T09:00", "2026-10-10T10:00")
    store.events = [row]
    changes = []

    def fail_comment(*_args, **_kwargs):
        raise RuntimeError("private-path-and-token-marker")

    dialog.comment_service = SimpleNamespace(comment=fail_comment)
    dialog.schedule_changed.connect(changes.append)
    dialog.refresh()
    monkeypatch.setattr(DeleteScheduleDialog, "exec", lambda modal: (modal.delete_button.click(), modal.result())[1])
    dialog.delete_button.click()
    wait_until(lambda: "받지 못했습니다" in dialog.comment_edit.toPlainText())
    assert store.events == [] and not dialog.delete_button.isEnabled()
    assert dialog.agenda.toPlainText() == "등록된 일정이 없습니다."
    assert not dialog._marked_dates
    assert changes == [{"change": "deleted", "event": row}]
    assert "private-path-and-token-marker" not in dialog.comment_edit.toPlainText()


def test_delete_cancel_or_missing_never_requests_comment(calendar, monkeypatch):
    dialog, store = calendar
    store.events = [event("유지할 일정", "2026-10-10T09:00", "2026-10-10T10:00")]
    changes, calls = [], []
    dialog.schedule_changed.connect(changes.append)
    dialog.comment_service = SimpleNamespace(comment=lambda *args, **kwargs: calls.append(args))
    dialog.refresh()
    monkeypatch.setattr(DeleteScheduleDialog, "exec", lambda modal: QDialog.DialogCode.Rejected)
    dialog.delete_button.click()
    assert not changes and not calls and store.events

    def delete_missing(modal):
        store.events.clear()
        modal.delete_button.click()
        return modal.result()

    monkeypatch.setattr(DeleteScheduleDialog, "exec", delete_missing)
    dialog.delete_button.click()
    assert not changes and not calls
    assert "이미 삭제된" in dialog.status_label.text()
    assert dialog.agenda.toPlainText() == "등록된 일정이 없습니다."


def test_slow_previous_comment_cannot_overwrite_newer_change(calendar):
    dialog, _store = calendar
    release_first = threading.Event()
    first_started = threading.Event()

    def comment(change, row, **_kwargs):
        if row["title"] == "이전 일정":
            first_started.set()
            assert release_first.wait(2)
        return {"text": row["title"] + " 코멘트", "model": "모델", "available": True}

    dialog.comment_service = SimpleNamespace(comment=comment)
    try:
        dialog._schedule_changed("added", event("이전 일정", "2026-10-10T09:00", "2026-10-10T10:00"))
        wait_until(first_started.is_set)
        dialog._schedule_changed("deleted", event("최근 일정", "2026-10-10T11:00", "2026-10-10T12:00"))
        wait_until(lambda: dialog.comment_edit.toPlainText() == "최근 일정 코멘트")
    finally:
        release_first.set()
    from ui.calendar_dialog import _ACTIVE_COMMENTS
    wait_until(lambda: not _ACTIVE_COMMENTS)
    assert dialog.comment_edit.toPlainText() == "최근 일정 코멘트"
    assert "최근 일정 삭제" in dialog.comment_heading.text()


def test_no_service_honestly_preserves_change_receipt(calendar):
    dialog, _store = calendar
    row = event("완료된 일정", "2026-10-10T09:00", "2026-10-10T10:00")
    dialog._schedule_changed("added", row)
    assert "변경은 완료되었습니다" in dialog.comment_edit.toPlainText()
    assert "모델 코멘트를 사용할 수 없습니다" in dialog.comment_edit.toPlainText()


def test_main_window_injects_service_and_forwards_calendar_change(monkeypatch, app):
    from ui.main_window import JarvisMainWindow

    class Host(QWidget):
        calendar_schedule_changed = pyqtSignal(object)

    host = Host()
    host.calendar_dialog = None
    host.calendar_comment_service = None
    store = Store()
    service = SimpleNamespace(comment=lambda *_args, **_kwargs: {})
    changes = []
    host.calendar_schedule_changed.connect(changes.append)
    monkeypatch.setattr("core.local_calendar.LocalCalendarStore", lambda: store)
    monkeypatch.setattr("ui.main_window.open_tab", lambda *_args: host.calendar_dialog)
    try:
        JarvisMainWindow.set_calendar_comment_service(host, service)
        surface = JarvisMainWindow.show_local_calendar(host)
        assert surface.store is store and surface.comment_service is service
        surface.schedule_changed.emit({"change": "deleted", "event": {"id": "event-id"}})
        assert changes == [{"change": "deleted", "event": {"id": "event-id"}}]
        replacement = SimpleNamespace(comment=lambda *_args, **_kwargs: {})
        JarvisMainWindow.set_calendar_comment_service(host, replacement)
        assert surface.comment_service is replacement
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("all_day", [False, True])
def test_delete_real_saved_event_updates_agenda_and_persistent_store(tmp_path, app, monkeypatch, all_day):
    store = LocalCalendarStore(tmp_path / "calendar.db")
    start = datetime(2026, 10, 10, 0 if all_day else 9, tzinfo=CALENDAR_TIMEZONE)
    end = datetime(2026, 10, 11, tzinfo=CALENDAR_TIMEZONE) if all_day else start.replace(hour=10)
    saved = store.add_event("실제 삭제 확인", start, end, all_day=all_day)
    calendar = CalendarDialog(store)
    calendar.calendar.setSelectedDate(QDate(2026, 10, 10))
    calendar.refresh()
    changes = []
    calendar.schedule_changed.connect(changes.append)

    def confirm_delete(modal):
        assert modal.event_combo.currentData() == saved["id"]
        if all_day:
            assert "2026년 10월 10일 ~ 2026년 10월 10일 (종일)" in modal.detail_edit.toPlainText()
        modal.delete_button.click()
        return modal.result()

    monkeypatch.setattr(DeleteScheduleDialog, "exec", confirm_delete)
    try:
        assert "실제 삭제 확인" in calendar.agenda.toPlainText()
        calendar.delete_button.click()
        assert LocalCalendarStore(store.db_path).get_event(saved["id"]) is None
        assert changes == [{"change": "deleted", "event": saved}]
        assert calendar.agenda.toPlainText() == "등록된 일정이 없습니다."
        assert not calendar._marked_dates
        assert "모델 코멘트를 사용할 수 없습니다" in calendar.comment_edit.toPlainText()
    finally:
        calendar.close()
        calendar.deleteLater()
        app.processEvents()


def test_comment_shutdown_cancels_bound_transport_and_joins_daemon_worker(calendar):
    from core.turn_context import current_turn_context
    from ui.calendar_dialog import _ACTIVE_COMMENTS, shutdown_calendar_comments

    dialog, _store = calendar
    started = threading.Event()
    contexts = []

    def comment(*_args, **_kwargs):
        context = current_turn_context()
        assert context is not None
        contexts.append(context)
        started.set()
        while not context.cancelled:
            threading.Event().wait(.005)
        context.checkpoint()

    dialog.comment_service = SimpleNamespace(comment=comment)
    dialog._schedule_changed("added", event("종료 중 코멘트", "2026-10-10T09:00", "2026-10-10T10:00"))
    wait_until(started.is_set)
    worker = dialog._comment_worker
    assert worker._thread.daemon and worker._thread.is_alive()
    shutdown_calendar_comments()
    assert contexts[0].cancelled and not worker._thread.is_alive()
    wait_until(lambda: not _ACTIVE_COMMENTS)
    assert "종료 중 코멘트 등록" in dialog.comment_heading.text()
    assert "받지 못했습니다" not in dialog.comment_edit.toPlainText()
    shutdown_calendar_comments()
