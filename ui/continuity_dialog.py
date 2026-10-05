"""Local-first work continuity dashboard; imported content stays plain text."""
from __future__ import annotations
from .theme import set_widget_style

import json
import threading
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QObject, QDateTime, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFileDialog,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from .dialog_theme import apply_dark_dialog_theme

STATUS_LABELS = {"pending": "할 일", "confirmed": "할 일", "in_progress": "진행 중",
                 "blocked": "막힘", "done": "완료", "cancelled": "취소", "suggested": "확인할 제안"}
PROVIDER_LABELS = {"internal": "아니스", "codex": "Codex", "claude_code": "Claude Code"}


def _text(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value or "")


def _step_key(row):
    return str(row.get("step_id") or row.get("id") or "")


def _status_text(row):
    status = row.get("status", "pending")
    if status == "done" and row.get("verification") == "reported":
        return "AI 기록상 완료 (미검증)"
    if status == "done" and row.get("verification") == "user_confirmed":
        return "완료 (사용자 확인)"
    return STATUS_LABELS.get(status, str(status))


def _date_text(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%m/%d %H:%M")
    except (TypeError, ValueError):
        return str(value)


class _SyncBridge(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)


class ContinuityDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._snapshot, self._selected_step, self._selected_session = {}, None, None
        self._event_limit = 500
        self._busy, self._sync_thread = False, None
        self._bridge = _SyncBridge(self)
        self._bridge.completed.connect(self._sync_completed)
        self._bridge.failed.connect(self._sync_failed)
        self.setWindowTitle("아니스 · 통합 리마인더")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.resize(1100, 810)
        apply_dark_dialog_theme(self)
        set_widget_style(self, """
            QTableWidget, QComboBox, QDateTimeEdit { background: #0e1927; color: #e6eef8; border: 1px solid #49627c; padding: 4px; }
            QHeaderView::section, QTabBar::tab { background: #172c42; color: #e6eef8; border: 0; padding: 8px; }
            QTabBar::tab:selected { background: #214f72; color: #67d8ef; }
            QTableWidget::item:selected { background: #214f72; }
            QTabWidget::pane { border: 1px solid #49627c; }
        """)
        self._build()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(15000)
        self.refresh_timer.timeout.connect(self._periodic_refresh)
        self.refresh_timer.start()
        self.refresh()

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        return table

    def _button(self, label, callback, layout):
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build(self):
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel("통합 리마인더")
        set_widget_style(title, "font-size: 22px; font-weight: 700;")
        heading.addWidget(title)
        heading.addStretch()
        self.refresh_button = self._button("목록 새로고침", self.refresh, heading)
        self.sync_button = self._button("연결된 로그 동기화", self.sync, heading)
        layout.addLayout(heading)
        self.summary_label = QLabel()
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setObjectName("dialogGuide")
        layout.addWidget(self.summary_label)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dashboard(), "다음 할 일")
        self.tabs.addTab(self._build_sessions(), "세션 · 계획")
        self.tabs.addTab(self._build_sources(), "연결 · 알림")
        layout.addWidget(self.tabs, 1)
        self.editor = self._build_editor()
        layout.addWidget(self.editor)
        self.tabs.currentChanged.connect(lambda index: self.editor.setVisible(index != 2))
        self.status_label = QLabel("연결된 소스만 읽습니다. 원본 파일은 수정하지 않습니다.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _build_dashboard(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.category = QComboBox()
        for label, key in (("지금 · 다음 할 일", "next_actions"), ("대기 · 막힌 작업", "blocked"),
                           ("최근 완료", "recent"), ("확인할 제안", "suggested")):
            self.category.addItem(label, key)
        self.category.currentIndexChanged.connect(self._render_tasks)
        layout.addWidget(self.category)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tasks_table = self._table(["계획 단계", "상태", "출처", "마감"])
        self.tasks_table.itemSelectionChanged.connect(self._task_selected)
        split.addWidget(self.tasks_table)
        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setPlaceholderText("단계를 선택하면 진행 근거와 다음 행동을 볼 수 있습니다.")
        split.addWidget(self.evidence)
        split.setSizes([620, 380])
        layout.addWidget(split, 1)
        self.empty_label = QLabel()
        layout.addWidget(self.empty_label)
        return page

    def _build_sessions(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("소스와 워크스페이스별 전체 세션"))
        controls.addStretch()
        self.manual_plan_button = self._button("선택 세션에 계획 작성", self._create_manual_plan, controls)
        self.manual_plan_button.setEnabled(False)
        layout.addLayout(controls)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.sessions_list = QListWidget()
        self.sessions_list.currentItemChanged.connect(self._session_selected)
        split.addWidget(self.sessions_list)
        details = QWidget()
        details_layout = QVBoxLayout(details)
        self.plan_summary = QLabel()
        self.plan_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.plan_summary.setWordWrap(True)
        details_layout.addWidget(self.plan_summary)
        self.plan_table = self._table(["계획 단계", "상태", "마감"])
        self.plan_table.itemSelectionChanged.connect(self._plan_step_selected)
        details_layout.addWidget(self.plan_table, 1)
        self.timeline = QPlainTextEdit()
        self.timeline.setReadOnly(True)
        self.timeline.setPlaceholderText("세션을 선택하면 대화, 계획 이력, 실행 기록을 볼 수 있습니다.")
        details_layout.addWidget(self.timeline, 2)
        self.more_events_button = QPushButton("이전 기록 더 보기")
        self.more_events_button.clicked.connect(self._load_more_events)
        self.more_events_button.setEnabled(False)
        details_layout.addWidget(self.more_events_button)
        split.addWidget(details)
        split.setSizes([330, 670])
        layout.addWidget(split, 1)
        return page

    def _build_sources(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        guide = QLabel("외부 로그는 연결한 폴더만 수집합니다. 폴더를 찾은 뒤 원하는 소스를 선택해 연결하세요.")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self.source_table = self._table(["로그 폴더", "프로그램", "상태", "최근 수집 오류"])
        layout.addWidget(self.source_table, 1)
        actions = QHBoxLayout()
        self.discover_button = self._button("로그 폴더 찾기", self._discover_sources, actions)
        self.connect_button = self._button("선택 폴더 연결", self._connect_selected_source, actions)
        self.custom_button = self._button("직접 폴더 선택", self._connect_custom_source, actions)
        self.pause_button = self._button("수집 일시정지 / 재개", self._toggle_source, actions)
        self.delete_button = self._button("연결 해제 · 수집본 삭제", self._delete_source, actions)
        layout.addLayout(actions)
        self.notifications = QCheckBox("마감 · 오래 멈춘 작업 알림 받기 (아니스 실행 중)")
        self.notifications.setChecked(bool(getattr(self.service, "notifications_enabled", False)))
        self.notifications.toggled.connect(self._set_notifications)
        layout.addWidget(self.notifications)
        layout.addWidget(QLabel("일시정지는 수집본을 유지합니다. 연결 해제는 아니스의 수집본만 삭제합니다."))
        return page

    def _build_editor(self):
        editor = QWidget()
        layout = QVBoxLayout(editor)
        self.selection_label = QLabel()
        self.selection_label.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)
        controls = QHBoxLayout()
        self.status_combo = QComboBox()
        for status in ("pending", "in_progress", "blocked", "done", "cancelled", "suggested"):
            self.status_combo.addItem(STATUS_LABELS[status], status)
        controls.addWidget(QLabel("상태"))
        controls.addWidget(self.status_combo)
        self.priority_combo = QComboBox()
        for label, value in (("보통", 0), ("관심", 1), ("중요", 2), ("긴급", 3)):
            self.priority_combo.addItem(label, value)
        controls.addWidget(QLabel("우선순위"))
        controls.addWidget(self.priority_combo)
        self.due_enabled = QCheckBox("마감")
        controls.addWidget(self.due_enabled)
        self.due_edit = QDateTimeEdit(QDateTime.currentDateTime().addDays(1))
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due_edit.setCalendarPopup(True)
        self.due_enabled.toggled.connect(self.due_edit.setEnabled)
        controls.addWidget(self.due_edit)
        self.save_step_button = self._button("변경 저장", self._save_step, controls)
        layout.addLayout(controls)
        reminders = QHBoxLayout()
        self.snooze_label = QLabel()
        self.snooze_label.setTextFormat(Qt.TextFormat.PlainText)
        reminders.addWidget(self.snooze_label)
        reminders.addStretch()
        self.confirm_done_button = self._button("완료 직접 확인", lambda: self._update_selected(status="done"), reminders)
        self.snooze_button = self._button("1시간 뒤에 다시 보기", self._snooze_step, reminders)
        self.unsnooze_button = self._button("미루기 해제", self._unsnooze_step, reminders)
        self.promote_button = self._button("이 제안을 할 일로 등록", self._confirm_suggestion, reminders)
        layout.addLayout(reminders)
        self._set_step(None)
        return editor

    def _periodic_refresh(self):
        # Do not replace pending user edits when the periodic timer fires.
        if self.isVisible() and not self._busy and self._selected_step is None:
            self.refresh()

    def refresh(self):
        try:
            self._snapshot = self.service.dashboard()
            self._render_tasks()
            self._render_sessions(self.service.list_sessions(limit=10000))
            self._render_sources(self.service.sources())
            self.summary_label.setText(
                f"다음 할 일 {len(self._snapshot.get('next_actions', []))} · "
                f"대기 {len(self._snapshot.get('blocked', []))} · "
                f"확인할 제안 {len(self._snapshot.get('suggested', []))} · "
                f"세션 {self.sessions_list.count()}"
            )
        except Exception as exc:
            self._show_error(f"목록을 읽지 못했습니다: {exc}")

    def _render_tasks(self):
        if not hasattr(self, "tasks_table"):
            return
        rows = self._snapshot.get(self.category.currentData(), [])
        self.tasks_table.blockSignals(True)
        self.tasks_table.setRowCount(0)
        for row in rows:
            self._append_step(self.tasks_table, row, include_provider=True)
        self.tasks_table.blockSignals(False)
        self.empty_label.setText("표시할 항목이 없습니다. 세션에서 계획을 작성하거나 로그 폴더를 연결하세요." if not rows else "")
        self.evidence.clear()
        if hasattr(self, "save_step_button"):
            self._set_step(None)

    @staticmethod
    def _append_step(table, row, *, include_provider=False):
        index = table.rowCount()
        table.insertRow(index)
        values = [row.get("description") or row.get("title") or row.get("goal") or "작업 이어가기", _status_text(row)]
        if include_provider:
            values.append(PROVIDER_LABELS.get(row.get("provider"), row.get("provider", "")))
        values.append(_date_text(row.get("due_at")))
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, row)
            table.setItem(index, column, item)

    @staticmethod
    def _table_selection(table):
        index = table.currentRow()
        item = table.item(index, 0) if index >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _task_selected(self):
        row = self._table_selection(self.tasks_table)
        self._set_step(row)
        if row:
            self.evidence.setPlainText(self._evidence_text(row))

    @staticmethod
    def _evidence_text(row):
        return "\n".join([
            _text(row.get("description") or row.get("title")), f"상태: {_status_text(row)}",
            f"목표: {_text(row.get('goal'))}", f"프로젝트: {_text(row.get('workspace_path'))}",
            f"세션: {_text(row.get('session_key'))}", f"마감: {_date_text(row.get('due_at'))}",
            f"최근 변경: {_date_text(row.get('updated_at'))}", f"대기 이유: {_text(row.get('waiting_reason'))}",
            "", "진행 근거", _text(row.get("evidence") or row.get("source_ref") or "근거 없음"),
        ])

    def _set_step(self, row):
        self._selected_step = row
        key = _step_key(row or {})
        suggestion = key.startswith("session:")
        editable = bool(key) and not suggestion and not self._busy
        for widget in (self.status_combo, self.priority_combo, self.due_enabled,
                       self.save_step_button, self.snooze_button, self.unsnooze_button):
            widget.setEnabled(editable)
        self.promote_button.setVisible(suggestion)
        self.promote_button.setEnabled(suggestion and not self._busy)
        self.confirm_done_button.setVisible(bool(row) and row.get("status") == "done" and row.get("verification") == "reported")
        self.confirm_done_button.setEnabled(editable)
        if row:
            self.selection_label.setText(_text(row.get("description") or row.get("title")))
            self.status_combo.setCurrentIndex(max(0, self.status_combo.findData(row.get("status"))))
            self.priority_combo.setCurrentIndex(max(0, self.priority_combo.findData(row.get("priority", 0))))
            date = QDateTime.fromString(str(row.get("due_at") or ""), Qt.DateFormat.ISODate)
            self.due_enabled.setChecked(date.isValid())
            self.due_edit.setDateTime(date.toLocalTime() if date.isValid() else QDateTime.currentDateTime().addDays(1))
            self.snooze_label.setText("다시 보기: " + _date_text(row.get("snoozed_until")))
        else:
            self.selection_label.setText("계획 단계를 선택하면 상태, 우선순위, 마감과 알림을 수정할 수 있습니다.")
            self.snooze_label.clear()
            self.due_enabled.setChecked(False)
        self.due_edit.setEnabled(editable and self.due_enabled.isChecked())

    def _save_step(self):
        if not self._selected_step or self._busy:
            return
        due = self.due_edit.dateTime().toUTC().toString(Qt.DateFormat.ISODate) if self.due_enabled.isChecked() else None
        changes = {"priority": self.priority_combo.currentData(), "due_at": due}
        status = self.status_combo.currentData()
        if status != self._selected_step.get("status"):
            changes["status"] = status
        self._update_selected(**changes)

    def _update_selected(self, **changes):
        if not self._selected_step or self._busy:
            return
        key = _step_key(self._selected_step)
        self._run_operation(lambda: self.service.update_step(key, **changes),
                            "사용자 수정 내용을 저장하는 중…", "사용자 수정 내용을 저장했습니다.")

    def _snooze_step(self):
        self._update_selected(snoozed_until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())

    def _unsnooze_step(self):
        self._update_selected(snoozed_until=None)

    def _confirm_suggestion(self):
        if not self._selected_step or self._busy:
            return
        row = self._selected_step
        description, accepted = QInputDialog.getText(self, "다음 할 일 등록", "할 일", text=_text(row.get("description") or row.get("title")))
        if accepted and description.strip():
            self._run_operation(lambda: self.service.confirm_suggestion(row["session_key"], description.strip()),
                                "다음 할 일을 등록하는 중…", "선택한 제안을 다음 할 일로 등록했습니다.")

    def _render_sessions(self, rows):
        selected_key = (self._selected_session or {}).get("session_key")
        self.sessions_list.blockSignals(True)
        self.sessions_list.clear()
        selected_item = None
        for row in rows:
            title = row.get("title") or row.get("goal") or row.get("session_key", "세션")
            item = QListWidgetItem(f"{title}\n{PROVIDER_LABELS.get(row.get('provider'), row.get('provider', ''))} · {row.get('workspace_path') or '작업 폴더 미지정'}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.sessions_list.addItem(item)
            if row.get("session_key") == selected_key:
                selected_item = item
        self.sessions_list.blockSignals(False)
        if selected_item is not None:
            self.sessions_list.setCurrentItem(selected_item)
        elif selected_key:
            self._session_selected(None)

    def _session_selected(self, item, previous=None):
        new_key = (item.data(Qt.ItemDataRole.UserRole) if item else {}).get("session_key")
        if new_key != (self._selected_session or {}).get("session_key"):
            self._event_limit = 500
        self._selected_session = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.manual_plan_button.setEnabled(bool(item) and not self._busy)
        self.plan_table.setRowCount(0)
        self.timeline.clear()
        if not item:
            return
        try:
            if self._event_limit == 500:
                detail = self.service.session_detail(self._selected_session["session_key"])
            else:
                detail = self.service.session_detail(self._selected_session["session_key"], limit=self._event_limit)
            self.more_events_button.setEnabled(bool(detail.get("has_more")) and self._event_limit < 10000)
            session = detail.get("session", self._selected_session)
            current_steps = detail.get("steps", [])
            complete = sum(s.get("status") == "done" for s in current_steps)
            verified = sum(s.get("status") == "done" and s.get("verification") != "reported" for s in current_steps)
            self.plan_summary.setText(f"계획 단계 {len(current_steps)}개 · 완료 기록 {complete}개 (검증/사용자 확인 {verified}개)")
            for row in detail.get("steps", []):
                self._append_step(self.plan_table, {**session, **row})
            pieces = [f"세션: {_text(session.get('title') or session.get('session_key'))}", f"프로젝트: {_text(session.get('workspace_path'))}"]
            if detail.get("has_more"):
                pieces.append(f"전체 {detail.get('event_count')}개 중 최근 {len(detail.get('events', []))}개 기록")
            for label, key in (("계획 버전", "plans"), ("대화", "messages"), ("실행 기록", "events"), ("상태 변경 이력", "history")):
                pieces.extend(["", label])
                pieces.extend(_text(record) for record in detail.get(key, []))
            self.timeline.setPlainText("\n".join(pieces))
        except Exception as exc:
            self._show_error(f"세션을 읽지 못했습니다: {exc}")

    def _load_more_events(self):
        self._event_limit = min(10000, self._event_limit + 500)
        self._session_selected(self.sessions_list.currentItem())

    def _plan_step_selected(self):
        row = self._table_selection(self.plan_table)
        self._set_step(row)
        if row:
            self.selection_label.setText(_text(row.get("description")) + " · " + _status_text(row))

    def _create_manual_plan(self):
        if not self._selected_session or self._busy:
            return
        text, accepted = QInputDialog.getMultiLineText(self, "세션 계획 작성", "실행할 단계를 한 줄에 하나씩 입력하세요.")
        steps = [{"description": line.strip(), "status": "pending"} for line in text.splitlines() if line.strip()]
        if not accepted or not steps:
            return
        session = self._selected_session
        self._run_operation(lambda: self.service.create_manual_plan(session["session_key"], session.get("title") or "사용자 계획", steps),
                            "계획을 저장하는 중…", "계획을 저장했습니다. '다음 할 일'에서 바로 확인할 수 있습니다.")

    def _render_sources(self, connected, candidates=()):
        known = {(r.get("provider"), str(r.get("path", "")).replace("\\", "/").casefold()) for r in connected}
        rows = list(connected) + [r for r in candidates if (r.get("provider"), str(r.get("path", "")).replace("\\", "/").casefold()) not in known]
        self.source_table.setRowCount(0)
        for row in rows:
            index = self.source_table.rowCount()
            self.source_table.insertRow(index)
            registered = bool(row.get("id") or row.get("source_id"))
            state = ("수집 중" if row.get("enabled") else "일시정지") if registered else ("연결 가능" if row.get("exists") else "폴더 없음")
            if row.get("pending_bytes") and row.get("enabled"):
                state += f" · {row['pending_bytes'] / 1048576:.1f} MB 남음"
            if row.get("skipped_records"):
                state += f" · 생략 {row['skipped_records']}개"
            values = (row.get("path", ""), PROVIDER_LABELS.get(row.get("provider"), row.get("provider", "")), state, row.get("last_error", ""))
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self.source_table.setItem(index, col, item)

    def _discover_sources(self):
        try:
            self._render_sources(self.service.sources(), self.service.discover_sources())
            self.status_label.setText("후보 폴더를 찾았습니다. '선택 폴더 연결'로 수집을 시작합니다.")
        except Exception as exc:
            self._show_error(f"로그 폴더를 찾지 못했습니다: {exc}")

    def _connect_selected_source(self):
        row = self._table_selection(self.source_table)
        if not row:
            self.status_label.setText("연결할 폴더를 먼저 선택하세요.")
        elif row.get("id") or row.get("source_id"):
            self.status_label.setText("이미 연결된 폴더입니다. 일시정지 / 재개를 사용할 수 있습니다.")
        else:
            self._add_source(row["provider"], row["path"])

    def _connect_custom_source(self):
        label, accepted = QInputDialog.getItem(self, "프로그램 선택", "로그 프로그램", ["Codex", "Claude Code"], 0, False)
        if accepted:
            path = QFileDialog.getExistingDirectory(self, "세션 로그가 저장된 폴더 선택")
            if path:
                self._add_source("codex" if label == "Codex" else "claude_code", path)

    def _add_source(self, provider, path):
        def connect_and_sync():
            self.service.add_source(provider, path)
            return self.service.sync()
        self._run_operation(connect_and_sync, "폴더를 연결하고 새 로그를 수집하는 중…", "폴더를 연결하고 로그를 수집했습니다.")

    def _toggle_source(self):
        row = self._table_selection(self.source_table)
        source_id = (row or {}).get("id") or (row or {}).get("source_id")
        if source_id:
            self._run_operation(lambda: self.service.set_source_enabled(source_id, not row.get("enabled", False)),
                                "수집 설정을 변경하는 중…", "수집 설정을 변경했습니다.")

    def _delete_source(self):
        row = self._table_selection(self.source_table)
        source_id = (row or {}).get("id") or (row or {}).get("source_id")
        if not source_id:
            return
        reply = QMessageBox.question(self, "연결 해제", "선택한 소스의 수집본과 계획을 아니스에서 삭제할까요?\n원본 로그 파일은 유지됩니다.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._run_operation(lambda: self.service.delete_source(source_id, purge=True), "연결을 해제하는 중…",
                                "연결과 아니스의 수집본을 삭제했습니다. 원본 로그는 유지되었습니다.")

    def _set_notifications(self, enabled):
        try:
            self.service.set_notifications_enabled(enabled)
        except Exception as exc:
            self.notifications.blockSignals(True)
            self.notifications.setChecked(bool(getattr(self.service, "notifications_enabled", False)))
            self.notifications.blockSignals(False)
            self._show_error(f"알림 설정을 저장하지 못했습니다: {exc}")

    def _set_busy(self, busy):
        self._busy = busy
        for widget in (self.sync_button, self.refresh_button, self.discover_button, self.connect_button,
                       self.custom_button, self.pause_button, self.delete_button):
            widget.setEnabled(not busy)
        self.manual_plan_button.setEnabled(bool(self._selected_session) and not busy)
        self._set_step(self._selected_step)

    def sync(self):
        self._run_operation(self.service.sync, "연결된 로그의 새 내용을 수집하는 중…",
                            "동기화를 마쳤습니다. 새 계획과 진행 근거를 반영했습니다.")

    def _run_operation(self, callback, progress, completed):
        if self._busy:
            return
        self._set_busy(True)
        self.status_label.setText(progress)
        self._completion_message = completed
        bridge = self._bridge

        def work():
            try:
                result = callback()
            except Exception as exc:
                try:
                    bridge.failed.emit(str(exc))
                except RuntimeError:
                    pass  # The application can close while this daemon finishes.
            else:
                try:
                    bridge.completed.emit(result)
                except RuntimeError:
                    pass

        self._sync_thread = threading.Thread(target=work, name="continuity-ui-sync", daemon=True)
        self._sync_thread.start()

    def _sync_completed(self, result):
        self._set_busy(False)
        self.refresh()
        errors = (result or {}).get("errors") if isinstance(result, dict) else None
        if errors:
            self._show_error("일부 로그를 수집하지 못했습니다: " + _text(errors))
        elif isinstance(result, dict) and result.get("pending_bytes"):
            self.status_label.setText(f"새 기록 {result.get('events', 0)}개 반영 · 초기 수집 {result['pending_bytes'] / 1048576:.1f} MB 남음 (백그라운드에서 계속 수집)")
        else:
            self.status_label.setText(self._completion_message)

    def _sync_failed(self, message):
        self._set_busy(False)
        self._show_error("작업 실패: " + message)

    def _show_error(self, message):
        self.status_label.setText(str(message))
