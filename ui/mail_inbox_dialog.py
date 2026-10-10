"""Read Chrome mail and collect account-scoped summaries in the background."""
from __future__ import annotations

import threading
from uuid import uuid4

from PyQt6.QtCore import QEvent, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.browser_mail import BrowserMailError
from core.browser_login import SITES
from core.mail_analysis import MailAnalysisError
from core.mail_brief import MailBriefError, get_mail_brief_service
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context
from .dialog_theme import apply_dark_dialog_theme
from .mail_connection_dialog import MailConnectionDialog
from .mail_category_dialog import MailCategoryDialog


# Keep parentless workers alive when the owning window is destroyed mid-query.
_ACTIVE_OPERATIONS: set[QThread] = set()


class _MailOperation(QThread):
    outcome = pyqtSignal(str, object)
    progress = pyqtSignal(object)

    def __init__(self, service, operation, provider, message_ref="", *, account="", items=(), brief_service=None):
        super().__init__()
        self.service, self.operation, self.provider = service, operation, provider
        self.message_ref = message_ref
        self.account, self.items = account, items
        self.brief_service = brief_service
        self.cancelled = threading.Event()
        self.context = TurnExecutionContext(turn_id=uuid4().hex, session_id="mail")
        self.disconnected = False
        self.busy_rejected = False

    def cancel(self):
        self.cancelled.set()
        self.context.cancel()

    def checkpoint(self):
        if self.cancelled.is_set():
            raise ToolCancelledError("메일 조회가 취소되었습니다.")

    def run(self):
        locked = False
        try:
            with bind_turn_context(self.context):
                self.checkpoint()
                if self.operation != "brief":
                    locked = self.service.workflow_lock.acquire(blocking=False)
                    if not locked:
                        self.busy_rejected = True
                        raise BrowserMailError("다른 메일 작업이 진행 중입니다. 작업이 끝난 뒤 다시 확인하세요.")
                if self.operation == "brief":
                    brief = self.brief_service if self.brief_service is not None else get_mail_brief_service()
                    result = brief.collect(checkpoint=self.checkpoint, progress=self.progress.emit)
                elif self.operation == "open":
                    result = self.service.open(self.provider)
                elif self.operation == "list":
                    result = self.service.list_current(self.provider, limit=50, checkpoint=self.checkpoint)
                elif self.operation == "body":
                    result = self.service.read_message(self.provider, self.message_ref, checkpoint=self.checkpoint)
                elif self.operation == "collect":
                    result = self.service.analysis.collect(
                        self.provider, self.account, self.items, self.service.read_message,
                        checkpoint=self.checkpoint, progress=self.progress.emit,
                    )
                elif self.operation == "switch":
                    self.service.switch_provider()
                    result = {}
                else:
                    self.service.disconnect()
                    self.disconnected = True
                    result = {}
                self.checkpoint()
                self.outcome.emit("completed", result)
        except ToolCancelledError:
            self.outcome.emit("cancelled", {})
        except (BrowserMailError, MailAnalysisError, MailBriefError) as exc:
            self.outcome.emit("failed", {"message": str(exc)})
        except Exception:
            # Browser exceptions can contain page content or credentials.
            self.outcome.emit("failed", {})
        finally:
            cleanup_lock = False
            if self.cancelled.is_set() and not self.disconnected and not self.busy_rejected:
                try:
                    cleanup_lock = not locked and self.service.workflow_lock.acquire(blocking=False)
                    if locked or cleanup_lock:
                        self.service.disconnect()
                        self.disconnected = True
                except Exception:
                    pass
                finally:
                    if cleanup_lock:
                        self.service.workflow_lock.release()
            if locked:
                self.service.workflow_lock.release()


def _release_worker(worker):
    _ACTIVE_OPERATIONS.discard(worker)
    worker.deleteLater()


class MailInboxDialog(QDialog):
    login_requested = pyqtSignal(str)

    def __init__(self, service, parent=None, *, brief_service=None):
        super().__init__(parent)
        self.service = service
        self.brief_service = brief_service
        self._worker = None
        self._pending_outcome = None
        self._pending_connection_settings = False
        self._connection_dialog = None
        self._category_dialog = None
        self._auto_configured = False
        self._closed = False
        self._items = []
        self._account = ""
        self._body_ref = None
        self._body_row = None
        self._body_view = None
        self._body_failed = False
        self._observed_count = 0
        self.setWindowTitle("메일 조회 · Gmail / 네이버")
        self.setMinimumSize(760, 520)
        self.setModal(False)
        apply_dark_dialog_theme(self)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "로그인된 Chrome의 Gmail·네이버 메일 목록을 조회합니다.\n"
            "로그인이 필요하면 아니스에 저장한 계정으로 로그인합니다. 추가 인증은 Chrome에서 직접 완료하세요.\n"
            "Chrome에서 선택한 현재 페이지의 목록 최대 50건을 표시합니다."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(intro)
        actions = QHBoxLayout()
        self.provider = QComboBox()
        self.provider.setAccessibleName("메일 서비스")
        self.provider.addItem("Gmail", "gmail")
        self.provider.addItem("네이버 메일", "naver")
        actions.addWidget(self.provider)
        self.open_button = QPushButton("Chrome 메일 열기")
        self.list_button = QPushButton("현재 목록 조회")
        self.refresh_button = QPushButton("새로고침")
        self.login_button = QPushButton("로그인 계정 설정")
        self.disconnect_button = QPushButton("연결 해제")
        for button in (self.open_button, self.list_button, self.refresh_button, self.login_button, self.disconnect_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        connection = QHBoxLayout()
        self.connection_button = QPushButton("자동 연결 설정")
        self.connection_button.setAutoDefault(False)
        self.connection_label = QLabel()
        self.connection_label.setWordWrap(True)
        self.connection_label.setTextFormat(Qt.TextFormat.PlainText)
        connection.addWidget(self.connection_button)
        connection.addWidget(self.connection_label, 1)
        layout.addLayout(connection)
        analysis = QHBoxLayout()
        self.category_button = QPushButton("요약·카테고리 설정")
        self.category_button.setAutoDefault(False)
        self.brief_button = QPushButton("메일 현황 수집")
        self.brief_button.setAutoDefault(False)
        self.brief_button.setToolTip("네이버·Gmail 받은편지함과 미발송 초안을 수집하고 신규·회신 검토 건수를 확인합니다.")
        self.analysis_label = QLabel()
        self.analysis_label.setWordWrap(True)
        self.analysis_label.setTextFormat(Qt.TextFormat.PlainText)
        analysis.addWidget(self.category_button)
        analysis.addWidget(self.brief_button)
        analysis.addWidget(self.analysis_label, 1)
        layout.addLayout(analysis)
        self.account_label = QLabel("조회된 계정: 아직 확인되지 않음")
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        self.account_label.setWordWrap(True)
        layout.addWidget(self.account_label)
        filters = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("조회된 목록에서 보낸 사람·제목 검색")
        self.search_input.setAccessibleName("조회된 메일 검색")
        self.search_input.setMaxLength(300)
        self.unread_only = QCheckBox("읽지 않은 메일만")
        filters.addWidget(self.search_input)
        filters.addWidget(self.unread_only)
        layout.addLayout(filters)
        self.count_label = QLabel("조회된 목록이 없습니다.")
        self.count_label.setWordWrap(True)
        self.count_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.count_label)
        self.table = QTableWidget(0, 4)
        self.table.setAccessibleName("현재 페이지 메일 목록")
        self.table.setHorizontalHeaderLabels(["보낸 사람", "제목", "수신 일시", "읽음 상태"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        self.table.cellClicked.connect(self._toggle_body)
        self.table.installEventFilter(self)
        self.status_label = QLabel("Chrome 메일 목록을 연 뒤 현재 목록 조회를 누르세요.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        note = QLabel(
            "자동 연결 토큰을 등록하면 대상 메일 탭을 자동 선택합니다. 미등록 시 Allow & select에서 직접 선택하세요.\n"
            "추가 인증은 Chrome에서 완료하세요. 계정·폴더·페이지를 바꾼 뒤에는 현재 목록을 다시 조회하세요.\n"
            "메일 행을 클릭하거나 Enter·Space를 누르면 아래에 본문을 펼칩니다. 메일을 열면 서비스에서 읽음 처리될 수 있습니다.\n"
            "자동 요약·분류가 켜져 있으면 조회 목록의 본문을 읽고 기억합니다. 수집 시 읽음 처리될 수 있습니다.\n"
            "표시 건수는 전체 메일함·신규 수신 건수가 아닙니다. 검색·읽지 않음 필터는 조회된 목록에만 적용됩니다."
        )
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("조회 취소")
        self.close_button = QPushButton("닫기")
        for button in (self.cancel_button, self.close_button):
            button.setAutoDefault(False)
            footer.addWidget(button)
        layout.addLayout(footer)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.open_button.clicked.connect(lambda: self._start("open"))
        self.list_button.clicked.connect(lambda: self._start("list"))
        self.refresh_button.clicked.connect(lambda: self._start("list"))
        self.login_button.clicked.connect(self._request_login)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.connection_button.clicked.connect(self._request_connection_settings)
        self.category_button.clicked.connect(self._show_category_settings)
        self.brief_button.clicked.connect(lambda: self._start("brief"))
        self.search_input.textChanged.connect(self._filter)
        self.unread_only.toggled.connect(self._filter)
        self.cancel_button.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.reject)
        self._refresh_connection_status()
        self._refresh_analysis_status()
        self._update_controls()

    def _refresh_analysis_status(self):
        try:
            settings = self.service.analysis.settings()
            self._analysis_enabled = isinstance(settings, dict) and settings.get("enabled") is True
            self.analysis_label.setText("자동 요약·분류: 켜짐" if self._analysis_enabled else "자동 요약·분류: 꺼짐")
        except Exception:
            self._analysis_enabled = False
            self.analysis_label.setText("메일 요약·분류 설정 확인 불가")

    def _show_category_settings(self):
        if self._worker is not None or self._closed or self._category_dialog is not None or self._connection_dialog is not None:
            return
        if not self.service.workflow_lock.acquire(blocking=False):
            self.status_label.setText("다른 메일 작업이 진행 중입니다. 작업이 끝난 뒤 설정을 변경하세요.")
            return
        try:
            try:
                analysis = self.service.analysis
            except Exception:
                self.analysis_label.setText("메일 요약·분류 설정 확인 불가")
                return
            dialog = MailCategoryDialog(analysis, self)
            self._category_dialog = dialog
            dialog.settings_changed.connect(self._category_settings_changed)
            self._update_controls()
            try:
                dialog.exec()
            finally:
                self._category_dialog = None
                dialog.deleteLater()
                self._refresh_analysis_status()
                self._update_controls()
        finally:
            self.service.workflow_lock.release()

    def _category_settings_changed(self):
        self._refresh_analysis_status()
        self.status_label.setText("메일 분류 설정을 저장했습니다. 현재 목록 조회·새로고침에서 변경한 기준을 적용합니다.")

    def _refresh_connection_status(self):
        try:
            self._auto_configured = self.service.connection_credentials.status().get("configured") is True
            self.connection_label.setText(
                "자동 연결 토큰: 등록됨 · 연결·로그인은 조회 시 확인" if self._auto_configured else
                "자동 연결 토큰: 미등록 · 수동 탭 승인 사용"
            )
        except Exception:
            self._auto_configured = False
            self.connection_label.setText("자동 연결 등록 상태 확인 불가")

    def _request_connection_settings(self):
        if self._worker is not None or self._closed or self._connection_dialog is not None or self._category_dialog is not None:
            return
        self._pending_connection_settings = True
        self._start("disconnect")

    def _show_connection_settings(self):
        if not self.service.workflow_lock.acquire(blocking=False):
            self.status_label.setText("다른 메일 작업이 진행 중입니다. 작업이 끝난 뒤 설정을 변경하세요.")
            return
        try:
            dialog = MailConnectionDialog(self.service.connection_credentials, self)
            self._connection_dialog = dialog
            dialog.registration_changed.connect(self._refresh_connection_status)
            self._update_controls()
            try:
                dialog.exec()
            finally:
                self._connection_dialog = None
                dialog.deleteLater()
                self._refresh_connection_status()
                self._update_controls()
        finally:
            self.service.workflow_lock.release()

    def _clear(self):
        self._collapse_body()
        self._items = []
        self._account = ""
        self._observed_count = 0
        self._pending_outcome = None
        self.table.clearContents()
        self.table.setRowCount(0)
        self.account_label.setText("조회된 계정: 아직 확인되지 않음")
        self.count_label.setText("조회된 목록이 없습니다.")

    def _provider_changed(self, *_args):
        self._clear()
        if self._worker is not None:
            self._cancel()
        else:
            self._start("switch")

    def _request_login(self):
        if self._worker is None and not self._closed:
            if self.service.workflow_lock.locked() is True:
                self.status_label.setText("다른 메일 작업이 진행 중입니다. 작업이 끝난 뒤 로그인 설정을 확인하세요.")
                return
            self._clear()
            site = "google" if self.provider.currentData() == "gmail" else "naver"
            self.login_requested.emit(SITES[site]["url"])

    def _disconnect(self):
        self._clear()
        if self._worker is not None:
            self._cancel()
        else:
            self._start("disconnect")

    def _update_controls(self):
        modal = self._connection_dialog is not None or self._category_dialog is not None
        busy = self._worker is not None or modal
        for button in (self.open_button, self.list_button, self.refresh_button, self.login_button, self.connection_button, self.category_button, self.brief_button):
            button.setEnabled(not busy and not self._closed)
        self.provider.setEnabled(not self._pending_connection_settings and not modal and not self._closed)
        self.disconnect_button.setEnabled(not modal and not self._closed)
        self.cancel_button.setEnabled(self._worker is not None and not self._worker.cancelled.is_set() and not self._closed)

    def _start(self, operation, message_ref=""):
        if (self._worker is not None or (self._closed and operation != "disconnect")
                or self._connection_dialog is not None or self._category_dialog is not None):
            return
        if operation not in ("body", "collect"):
            self._clear()
        if operation == "collect":
            self._collapse_body()
        worker = _MailOperation(self.service, operation, self.provider.currentData(), message_ref,
                                account=self._account, items=[dict(item) for item in self._items],
                                brief_service=self.brief_service)
        self._worker = worker
        _ACTIVE_OPERATIONS.add(worker)
        worker.outcome.connect(self._record_outcome)
        worker.progress.connect(self._record_progress)
        worker.finished.connect(self._finished)
        worker.finished.connect(lambda: _release_worker(worker))
        self.destroyed.connect(worker.cancel)
        if not self._closed:
            self.status_label.setText(
                "선택한 메일의 본문을 확인하고 있습니다…" if operation == "body" else
                "네이버·Gmail 메일 현황을 수집하고 있습니다…" if operation == "brief" else
                f"조회한 목록 {len(self._items)}건의 본문을 읽고 요약·분류하고 있습니다…" if operation == "collect" else
                "연결을 해제하고 있습니다…" if operation == "disconnect" else
                "메일 서비스를 전환하고 있습니다…" if operation == "switch" else
                "Chrome 메일 작업 중… 저장된 자동 연결 토큰으로 대상 탭을 확인합니다." if self._auto_configured else
                "Chrome 메일 작업 중… 대상 탭 선택까지 최대 5분 기다립니다."
            )
        self._update_controls()
        worker.start()

    @pyqtSlot(str, object)
    def _record_outcome(self, kind, payload):
        worker = self._worker
        if not self._closed and worker is not None and worker is self.sender() and not worker.cancelled.is_set():
            self._pending_outcome = (kind, payload)

    @pyqtSlot(object)
    def _record_progress(self, payload):
        worker = self._worker
        if (self._closed or worker is None or worker is not self.sender()
                or worker.cancelled.is_set() or worker.provider != self.provider.currentData()):
            return
        if worker.operation == "brief":
            if isinstance(payload, dict) and payload.get("provider") in ("naver", "gmail"):
                name = "네이버" if payload["provider"] == "naver" else "Gmail"
                self.status_label.setText(f"{name} 메일 현황을 수집하고 있습니다…")
            return
        if worker.operation != "collect":
            return
        if isinstance(payload, dict) and all(type(payload.get(key)) is int and 0 <= payload[key] <= len(worker.items)
                                             for key in ("processed", "partial", "skipped", "failed")):
            self.status_label.setText(
                f"메일 요약·분류 중 · 분류 완료 {payload['processed']}건 · 부분 요약 {payload['partial']}건 · "
                f"기존 기록 {payload['skipped']}건 · 미분류 {payload['failed']}건"
            )

    @pyqtSlot()
    def _finished(self):
        worker = self.sender()
        if worker is None or worker is not self._worker:
            return
        self._worker = None
        if worker is not None:
            try:
                self.destroyed.disconnect(worker.cancel)
            except (TypeError, RuntimeError):
                pass
        kind, payload = self._pending_outcome or ("failed", {})
        self._pending_outcome = None
        show_connection_settings = (self._pending_connection_settings and worker is not None
                                    and worker.operation == "disconnect" and not worker.cancelled.is_set()
                                    and kind == "completed" and not self._closed)
        self._pending_connection_settings = False
        collect = False
        # Cancellation may arrive after run() finished but before Qt delivers
        # its queued result. That worker can no longer clean up in finally.
        if (worker is not None and worker.operation != "disconnect" and not worker.disconnected and not worker.busy_rejected
                and (self._closed or worker.cancelled.is_set())):
            self._start("disconnect")
        if self._closed:
            return
        if worker is None or worker.cancelled.is_set() or worker.provider != self.provider.currentData():
            self._clear()
            self.status_label.setText("작업을 취소하고 연결을 해제했습니다. 대상 메일 목록을 다시 조회하세요.")
        elif kind == "completed":
            if worker.operation == "body":
                self._finish_body(worker, kind, payload)
            elif worker.operation == "collect":
                self._finish_collection(payload)
            elif worker.operation == "brief":
                summary = payload.get("summary") if isinstance(payload, dict) else None
                self.status_label.setText(summary if isinstance(summary, str) and 1 <= len(summary) <= 6000 else
                                          "메일 현황 집계 결과를 확인하지 못했습니다. 다시 수집하세요.")
            elif worker.operation == "open":
                self.status_label.setText("Chrome에 메일 주소를 전달했습니다. 현재 목록 조회 시 저장된 로그인 계정을 활용합니다.")
            elif worker.operation == "disconnect":
                self.status_label.setText("앱의 메일 탭 연결을 해제했습니다. Chrome 로그인 상태는 유지됩니다.")
            elif worker.operation == "switch":
                self.status_label.setText("메일 서비스를 전환했습니다. 현재 목록 조회에서 대상 메일 계정을 확인하세요.")
            else:
                try:
                    self._apply_result(payload)
                    self.status_label.setText("선택한 Chrome 메일 목록을 조회했습니다. 메일을 열거나 읽음 상태를 변경하지 않았습니다.")
                    self._refresh_analysis_status()
                    collect = self._analysis_enabled and bool(self._items)
                except (TypeError, ValueError, KeyError):
                    self._clear()
                    self.status_label.setText("메일 목록을 확인하지 못했습니다. 올바른 메일 목록 탭에서 다시 조회하세요.")
        elif kind == "cancelled":
            self._clear()
            self.status_label.setText("메일 조회를 취소했습니다.")
        else:
            if worker is not None and worker.busy_rejected:
                self._clear()
                self.status_label.setText("다른 메일 작업이 진행 중입니다. 작업이 끝난 뒤 다시 확인하세요.")
            elif worker is not None and worker.operation == "body":
                self._finish_body(worker, kind, payload)
            elif worker is not None and worker.operation == "collect":
                message = payload.get("message") if isinstance(payload, dict) else None
                self.status_label.setText(message if isinstance(message, str) and message else
                                          "자동 요약·분류를 마치지 못했습니다. 현재 목록을 다시 조회하세요.")
            else:
                self._clear()
                message = payload.get("message") if isinstance(payload, dict) else None
                self.status_label.setText(message if isinstance(message, str) and message else
                                          "메일을 조회하지 못했습니다. Chrome 로그인·확장 설치·탭 선택을 확인하고 다시 조회하세요.")
        self._update_controls()
        if show_connection_settings:
            self._show_connection_settings()
        elif collect:
            self._start("collect")

    def _finish_collection(self, payload):
        if (not isinstance(payload, dict) or any(type(payload.get(key)) is not int or payload[key] < 0
                                                for key in ("processed", "partial", "skipped", "failed"))
                or sum(payload[key] for key in ("processed", "partial", "skipped", "failed")) > len(self._items)):
            self.status_label.setText("메일 요약·분류 결과를 확인하지 못했습니다. 현재 목록을 다시 조회하세요.")
            return
        self.status_label.setText(
            f"메일 요약·분류 처리 종료 · 분류 완료 {payload['processed']}건 · 부분 요약 {payload['partial']}건 · "
            f"기존 기록 {payload['skipped']}건 · 미분류 {payload['failed']}건. "
            "부분 요약·미분류는 알림 대상에서 제외합니다. 목록의 읽음 상태는 새로고침 시 확인됩니다."
        )

    def _apply_result(self, payload):
        if not isinstance(payload, dict) or payload.get("scope") != "current_view" or payload.get("provider") != self.provider.currentData():
            raise ValueError("Invalid mail scope")
        items, account, observed = payload.get("items"), payload.get("account"), payload.get("observed_count")
        if not isinstance(items, list) or len(items) > 50 or not isinstance(account, str) or not account.strip() or len(account) > 320:
            raise ValueError("Invalid mail result")
        if type(observed) is not int or not len(items) <= observed <= 10000:
            raise ValueError("Invalid mail count")
        for item in items:
            if not isinstance(item, dict) or type(item.get("unread")) is not bool:
                raise ValueError("Invalid mail row")
            if any(not isinstance(item.get(key), str) or len(item[key]) > 4096 for key in ("sender", "subject", "date")):
                raise ValueError("Invalid mail text")
            if "message_ref" in item and (not isinstance(item["message_ref"], str) or len(item["message_ref"]) > 4096):
                raise ValueError("Invalid mail reference")
        self._items = items
        self._account = account
        self._observed_count = observed
        self.account_label.setText(f"조회된 계정: {account}")
        self._filter()

    def _filter(self, *_args):
        if self._worker is not None and self._worker.operation == "body":
            self._cancel()
            return
        self._collapse_body()
        self.table.clearSpans()
        query = self.search_input.text().strip().casefold()
        items = [item for item in self._items if
                 (not self.unread_only.isChecked() or item["unread"]) and
                 (not query or query in item["sender"].casefold() or query in item["subject"].casefold())]
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            for column, value in enumerate((item["sender"], item["subject"], item["date"], "읽지 않음" if item["unread"] else "읽음")):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, item)
                if item.get("message_ref"):
                    cell.setToolTip("클릭 또는 Enter·Space로 본문 펼치기 / 접기")
                self.table.setItem(row, column, cell)
        if self._items or self._observed_count:
            unread = sum(item["unread"] for item in self._items)
            self.count_label.setText(
                f"현재 페이지 목록 {self._observed_count}건 중 조회 {len(self._items)}건 · "
                f"조회된 목록의 읽지 않음 {unread}건 · 필터 표시 {len(items)}건"
            )
        else:
            self.count_label.setText("현재 조회된 목록은 0건입니다. 전체 메일함 건수는 확인하지 않습니다.")

    def eventFilter(self, watched, event):
        if watched is self.table and event.type() == QEvent.Type.KeyPress and event.key() in (
                Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._toggle_body(self.table.currentRow())
            return True
        return super().eventFilter(watched, event)

    def _collapse_body(self):
        if self._body_view is not None:
            self._body_view.setPlainText("")
        if self._body_row is not None:
            self.table.removeRow(self._body_row)
        self._body_ref = self._body_row = self._body_view = None
        self._body_failed = False

    def _toggle_body(self, row, _column=0):
        cell = self.table.item(row, 0)
        item = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
        if self._closed or not isinstance(item, dict) or not item.get("message_ref"):
            return
        message_ref = item["message_ref"]
        if message_ref == self._body_ref and not self._body_failed:
            self._collapse_body()
            return
        if self._worker is not None:
            return
        if self._body_row is not None and row > self._body_row:
            row -= 1
        self._collapse_body()
        self._body_ref, self._body_row = message_ref, row + 1
        self.table.insertRow(self._body_row)
        self.table.setSpan(self._body_row, 0, 1, self.table.columnCount())
        self._body_view = QPlainTextEdit()
        self._body_view.setReadOnly(True)
        self._body_view.setAccessibleName("선택한 메일 본문")
        self._body_view.setPlainText("메일 본문을 불러오고 있습니다…")
        self.table.setCellWidget(self._body_row, 0, self._body_view)
        self.table.setRowHeight(self._body_row, 240)
        self.table.scrollToItem(self.table.item(row, 0), QAbstractItemView.ScrollHint.PositionAtTop)
        self._start("body", message_ref)

    def _finish_body(self, worker, kind, payload):
        if worker.message_ref != self._body_ref or self._body_view is None:
            self.status_label.setText("메일 본문을 접었습니다.")
            return
        if kind == "completed":
            valid = (isinstance(payload, dict) and payload.get("provider") == worker.provider
                     and payload.get("account") == self._account and payload.get("message_ref") == worker.message_ref
                     and all(isinstance(payload.get(key), str) for key in ("subject", "sender", "date", "body"))
                     and len(payload["body"]) <= 200000 and type(payload.get("truncated", False)) is bool)
            if valid:
                text = payload["body"] or "표시할 텍스트 본문이 없습니다."
                if payload.get("truncated"):
                    text += "\n\n[긴 본문은 일부만 표시합니다. 전체 내용은 Chrome에서 확인하세요.]"
                self._body_view.setPlainText(text)
                self.status_label.setText("선택한 메일 본문을 펼쳤습니다. 다시 클릭하면 접습니다. 목록의 읽음 상태는 새로고침 시 확인됩니다.")
                return
        self._body_failed = True
        message = payload.get("message") if kind == "failed" and isinstance(payload, dict) else None
        safe_message = message if isinstance(message, str) and message else "메일 본문을 확인하지 못했습니다. 현재 목록을 다시 조회하세요."
        self._body_view.setPlainText(safe_message + "\n현재 목록을 다시 조회한 뒤 메일을 선택하세요.")
        self.status_label.setText("메일 본문을 불러오지 못했습니다.")

    def _cancel(self):
        self._pending_connection_settings = False
        self._clear()
        if self._worker is not None:
            self._worker.cancel()
            if not self._closed:
                self.status_label.setText("취소 요청 중… 메일 연결을 정리하고 있습니다.")
        self._update_controls()

    def _prepare_close(self):
        if self._closed:
            return
        self._closed = True
        self._pending_connection_settings = False
        if self._connection_dialog is not None:
            self._connection_dialog.reject()
        if self._category_dialog is not None:
            self._category_dialog.reject()
        self._clear()
        self.search_input.clear()
        self.unread_only.setChecked(False)
        self._disconnect()

    def can_close_workspace_tab(self):
        self._prepare_close()
        return True

    def done(self, result):
        self._prepare_close()
        super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        self.done(QDialog.DialogCode.Rejected)
        event.accept()

    def showEvent(self, event):
        self._closed = False
        self._update_controls()
        super().showEvent(event)
