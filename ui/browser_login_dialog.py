"""Direct Chrome login settings; passwords stay in the local encrypted vault."""
from __future__ import annotations

import threading

from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout,
)

from core.plugin import ToolCancelledError
from core.browser_extension import BrowserExtensionUnavailableError
from .dialog_theme import apply_dark_dialog_theme


_ACTIVE_OPERATIONS: set[QThread] = set()


class _LoginOperation(QThread):
    outcome = pyqtSignal(str, object)

    def __init__(self, service, operation, url="", credentials=None):
        super().__init__()
        self.service, self.operation, self.url = service, operation, url
        self.credentials = credentials or {}
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()

    def checkpoint(self):
        if self.cancelled.is_set():
            raise ToolCancelledError("Chrome 로그인 작업이 취소되었습니다.")

    def run(self):
        try:
            self.checkpoint()
            if self.operation == "open":
                result = self.service.open(self.url)
            elif self.operation == "inspect":
                result = self.service.inspect(self.url, checkpoint=self.checkpoint)
            elif self.operation == "fill":
                result = self.service.fill(url=self.url, **self.credentials, checkpoint=self.checkpoint)
            elif self.operation == "fill_saved":
                result = self.service.fill_saved(self.url, **self.credentials, checkpoint=self.checkpoint)
            elif self.operation == "save":
                result = self.service.save_account(self.url, **self.credentials)
            elif self.operation == "delete":
                result = self.service.delete_account(self.url)
            else:
                self.service.disconnect()
                result = {"state": "disconnected"}
            self.checkpoint()
            self.outcome.emit("completed", result)
        except ToolCancelledError:
            self.outcome.emit("cancelled", {})
        except BrowserExtensionUnavailableError:
            self.outcome.emit("failed", {"error_code": "extension_missing"})
        except Exception:
            # Browser exceptions may include page text or credentials.
            self.outcome.emit("failed", {})
        finally:
            self.credentials.clear()


def _release_worker(worker):
    _ACTIVE_OPERATIONS.discard(worker)
    worker.deleteLater()


class BrowserLoginDialog(QDialog):
    def __init__(self, service, parent=None, *, url=""):
        super().__init__(parent)
        from core.browser_login import SITES

        self.service = service
        self._worker = None
        self._pending_outcome = None
        self._pending_close = None
        self._fields = {}
        self._account_configured = False
        self._observed_origin = ""
        self.setWindowTitle("Chrome 로그인")
        self.setMinimumWidth(590)
        self.setModal(False)
        apply_dark_dialog_theme(self)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "평소 사용하는 Chrome의 로그인 화면을 연결합니다. Chrome 비밀번호 관리자나 직접 로그인도 사용할 수 있습니다.\n"
            "아이디·비밀번호를 암호화 저장해 재사용할 수 있습니다. 비밀번호는 채팅에 입력하지 마세요."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(intro)
        form = QFormLayout()
        self.site = QComboBox()
        for spec in SITES.values():
            self.site.addItem(spec["name"], spec["url"])
        self.site.addItem("직접 입력", "")
        self.url_input = QLineEdit()
        self.url_input.setMaxLength(2048)
        self.url_input.setPlaceholderText("https://서비스의 로그인 주소")
        self.username_input = QLineEdit()
        self.username_input.setMaxLength(320)
        self.username_input.setPlaceholderText("현재 단계의 아이디 / 이메일")
        self.password_input = QLineEdit()
        self.password_input.setMaxLength(4096)
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setInputMethodHints(
            Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText
            | Qt.InputMethodHint.ImhNoAutoUppercase,
        )
        self.password_input.setPlaceholderText("일반 로그인 비밀번호 · 저장된 값은 표시하지 않음")
        form.addRow("서비스", self.site)
        form.addRow("HTTPS 주소", self.url_input)
        form.addRow("아이디", self.username_input)
        form.addRow("비밀번호", self.password_input)
        layout.addLayout(form)
        accounts = QHBoxLayout()
        self.save_button = QPushButton("아이디·비밀번호 암호화 저장")
        self.delete_button = QPushButton("저장 정보 삭제")
        for button in (self.save_button, self.delete_button):
            button.setAutoDefault(False)
            accounts.addWidget(button)
        layout.addLayout(accounts)
        self.account_label = QLabel()
        self.account_label.setWordWrap(True)
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.account_label)
        actions = QHBoxLayout()
        self.open_button = QPushButton("Chrome 열기")
        self.inspect_button = QPushButton("탭 연결 / 상태 확인")
        self.disconnect_button = QPushButton("탭 연결 해제")
        for button in (self.open_button, self.inspect_button, self.disconnect_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        saved_inputs = QHBoxLayout()
        self.saved_fill_button = QPushButton("저장된 계정으로 입력")
        self.saved_submit_button = QPushButton("저장된 계정으로 로그인 / 다음")
        for button in (self.saved_fill_button, self.saved_submit_button):
            button.setAutoDefault(False)
            saved_inputs.addWidget(button)
        layout.addLayout(saved_inputs)
        inputs = QHBoxLayout()
        self.fill_button = QPushButton("입력만")
        self.submit_button = QPushButton("입력 후 로그인 / 다음")
        for button in (self.fill_button, self.submit_button):
            button.setAutoDefault(False)
            inputs.addWidget(button)
        layout.addLayout(inputs)
        self.status_label = QLabel("Chrome을 연 뒤 탭 연결 / 상태 확인을 누르세요. 페이지 열기는 로그인 성공을 뜻하지 않습니다.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        note = QLabel(
            "첫 연결 시 공식 Playwright 확장의 Allow & select에서 대상 탭을 직접 선택하세요.\n"
            "네이버 웹 로그인은 일반 비밀번호를 사용합니다. 메일 앱 비밀번호와 별도로 저장합니다.\n"
            "캡차·2단계 인증·패스키는 Chrome에서 직접 완료한 후 상태를 다시 확인하세요.\n"
            "단계가 바뀌면 현재 화면을 다시 확인하고 입력하세요. 일반 사이트의 로그인 성공은 자동 확인이 어려울 수 있습니다."
        )
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("작업 취소")
        self.close_button = QPushButton("닫기")
        for button in (self.cancel_button, self.close_button):
            button.setAutoDefault(False)
            footer.addWidget(button)
        layout.addLayout(footer)
        self.site.currentIndexChanged.connect(self._site_changed)
        self.url_input.textChanged.connect(self._target_changed)
        self.username_input.textChanged.connect(self._update_controls)
        self.password_input.textChanged.connect(self._update_controls)
        self.open_button.clicked.connect(lambda: self._start("open"))
        self.inspect_button.clicked.connect(lambda: self._start("inspect"))
        self.disconnect_button.clicked.connect(lambda: self._start("disconnect"))
        self.save_button.clicked.connect(self._save)
        self.delete_button.clicked.connect(lambda: self._start("delete"))
        self.saved_fill_button.clicked.connect(lambda: self._fill_saved(False))
        self.saved_submit_button.clicked.connect(lambda: self._fill_saved(True))
        self.fill_button.clicked.connect(lambda: self._fill(False))
        self.submit_button.clicked.connect(lambda: self._fill(True))
        self.cancel_button.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.reject)
        self._site_changed()
        if url:
            self.set_url(url)

    def set_url(self, url):
        if self._worker is not None:
            return
        index = self.site.findData(url)
        self.site.setCurrentIndex(index if index >= 0 else self.site.count() - 1)
        self.url_input.setText(url)
        self._target_changed()

    def _site_changed(self, *_args):
        self.username_input.clear()
        self.password_input.clear()
        self.url_input.setReadOnly(bool(self.site.currentData()))
        self.url_input.setText(self.site.currentData() or "")
        self._target_changed()

    def _target_changed(self, *_args):
        self._fields = {}
        self._observed_origin = ""
        self.password_input.clear()
        self._refresh_account()
        self.status_label.setText("대상 탭의 현재 화면을 확인한 뒤 입력할 수 있습니다.")
        self._update_controls()

    def _refresh_account(self):
        self._account_configured = False
        try:
            status = self.service.account_status(self.url_input.text().strip())
            configured = status.get("configured") is True
            username = status.get("username", "")
            if not isinstance(username, str) or len(username) > 320 or (configured and not username.strip()):
                raise ValueError("Invalid account status")
            self._account_configured = configured
            self.username_input.setText(username if configured else "")
            self.account_label.setText("암호화 저장됨 · 저장된 비밀번호는 이 창에 다시 표시하지 않습니다." if configured else "이 사이트에 저장된 로그인 정보가 없습니다.")
        except Exception:
            self.username_input.clear()
            self.account_label.setText("저장 상태를 확인하지 못했습니다. 올바른 HTTPS 주소와 계정 정보를 확인하세요.")

    def _update_controls(self, *_args):
        busy = self._worker is not None
        target = self.url_input.text().strip().startswith("https://")
        for widget in (self.site, self.url_input, self.username_input, self.password_input):
            widget.setEnabled(not busy)
        self.open_button.setEnabled(not busy and target)
        self.inspect_button.setEnabled(not busy and target)
        self.disconnect_button.setEnabled(not busy)
        self.save_button.setEnabled(not busy and target and bool(self.username_input.text().strip()) and bool(self.password_input.text()))
        self.delete_button.setEnabled(not busy and self._account_configured)
        has_input = (
            self._fields.get("username_field") is True and bool(self.username_input.text().strip())
            or self._fields.get("password_field") is True and bool(self.password_input.text())
        )
        self.fill_button.setEnabled(not busy and bool(has_input))
        self.submit_button.setEnabled(not busy and bool(has_input) and self._fields.get("submit") is True)
        saved_input = self._account_configured and any(self._fields.get(key) is True for key in ("username_field", "password_field"))
        self.saved_fill_button.setEnabled(not busy and saved_input)
        self.saved_submit_button.setEnabled(not busy and saved_input and self._fields.get("submit") is True)
        self.cancel_button.setEnabled(busy and not self._worker.cancelled.is_set())

    def _save(self):
        if self._worker is not None or not self.save_button.isEnabled():
            return
        credentials = {"username": self.username_input.text().strip(), "password": self.password_input.text()}
        self.password_input.clear()
        self._start("save", credentials)

    def _fill_saved(self, submit):
        button = self.saved_submit_button if submit else self.saved_fill_button
        if self._worker is None and button.isEnabled():
            self.password_input.clear()
            self._start("fill_saved", {"submit": submit})

    def _fill(self, submit):
        button = self.submit_button if submit else self.fill_button
        if self._worker is not None or not button.isEnabled():
            return
        credentials = {
            "username": self.username_input.text() if self._fields.get("username_field") is True else "",
            "password": self.password_input.text() if self._fields.get("password_field") is True else "",
            "submit": submit,
        }
        self.password_input.clear()
        self._start("fill", credentials)

    def _start(self, operation, credentials=None):
        if self._worker is not None:
            return
        worker = _LoginOperation(self.service, operation, self.url_input.text().strip(), credentials)
        self._worker = worker
        self._pending_outcome = None
        self._fields = {}
        _ACTIVE_OPERATIONS.add(worker)
        worker.outcome.connect(self._record_outcome)
        worker.finished.connect(self._finished)
        worker.finished.connect(lambda: _release_worker(worker))
        self.destroyed.connect(worker.cancel)
        self.status_label.setText("Chrome 화면 작업 중… 첫 연결은 탭 선택까지 최대 5분 기다립니다.")
        self._update_controls()
        worker.start()

    @pyqtSlot(str, object)
    def _record_outcome(self, kind, payload):
        self._pending_outcome = (kind, payload)

    @pyqtSlot()
    def _finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                self.destroyed.disconnect(worker.cancel)
            except (TypeError, RuntimeError):
                pass
        kind, payload = self._pending_outcome or ("failed", {})
        self._pending_outcome = None
        if worker is not None and worker.cancelled.is_set():
            kind = "cancelled"
        if self._pending_close is not None:
            result, self._pending_close = self._pending_close, None
            self._update_controls()
            self.done(result)
            return
        if kind == "completed" and isinstance(payload, dict):
            if worker.operation in {"save", "delete"}:
                self._refresh_account()
                if worker.operation == "save" and self._account_configured:
                    message = "로그인 정보를 암호화 저장했습니다. 탭 연결 / 상태 확인 후 저장된 계정으로 로그인하세요."
                elif worker.operation == "delete" and not self._account_configured:
                    message = "이 사이트의 저장 정보를 삭제했습니다."
                else:
                    message = "저장 정보 변경을 확인하지 못했습니다."
                self.status_label.setText(message)
                self._update_controls()
                return
            origin = payload.get("origin", "")
            if worker.operation == "inspect" or not self._observed_origin or origin == self._observed_origin:
                self._fields = {key: payload.get(key) is True for key in ("username_field", "password_field", "submit")}
            self._observed_origin = origin if isinstance(origin, str) else ""
            messages = {
                "opened": "Chrome에 로그인 주소를 전달했습니다. 탭을 연결해 상태를 확인하세요. 로그인은 아직 미확인입니다.",
                "authenticated": "선택한 Chrome 탭에서 로그인된 화면을 확인했습니다.",
                "login_required": "로그인 입력 화면을 확인했습니다. 현재 단계의 입력칸만 채우거나 Chrome에서 직접 로그인하세요.",
                "user_action_required": "Chrome에서 캡차·추가 인증 또는 계정 선택을 완료한 뒤 상태를 다시 확인하세요.",
                "disconnected": "앱의 탭 연결을 해제했습니다. Chrome의 로그인 상태는 유지됩니다.",
            }
            state = payload.get("state")
            self.status_label.setText(messages.get(state if isinstance(state, str) else "", "현재 화면만으로 로그인 성공을 확인하지 못했습니다. Chrome에서 직접 확인하세요."))
        elif kind == "cancelled":
            self.status_label.setText("작업을 취소했습니다. 이미 입력·제출된 내용은 Chrome에서 확인하세요.")
        elif isinstance(payload, dict) and payload.get("error_code") == "extension_missing":
            self.status_label.setText("Chrome에서 공식 Playwright 확장을 찾지 못했습니다. 평소 사용하는 Chrome 프로필에 확장을 설치한 뒤 다시 연결하세요.")
        else:
            self.status_label.setText("작업을 확인하지 못했습니다. Chrome·공식 확장 설치와 선택한 HTTPS 탭을 확인하고 다시 연결하세요.")
        self._update_controls()

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("취소 요청 중… Chrome 작업이 정리될 때까지 기다립니다.")
            self._update_controls()

    def done(self, result):
        self.password_input.clear()
        if self._worker is not None:
            self._pending_close = int(result)
            self._cancel()
            return
        super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        self.password_input.clear()
        if self._worker is not None:
            self._pending_close = int(QDialog.DialogCode.Rejected)
            self._cancel()
            event.ignore()
            return
        super().closeEvent(event)


def open_browser_login_dialog(registry, parent=None, url=""):
    plugin = registry.get_plugin("browser_login") if registry is not None else None
    if plugin is None:
        QMessageBox.warning(parent, "Chrome 로그인", "로그인 플러그인이 등록되지 않았습니다. 앱을 다시 실행하세요.")
        return None
    dialog = getattr(parent, "_browser_login_dialog", None) if parent is not None else None
    if dialog is None:
        dialog = BrowserLoginDialog(plugin.service, parent, url=url)
        if parent is not None:
            parent._browser_login_dialog = dialog
    elif url:
        dialog.set_url(url)
    elif dialog._worker is None:
        dialog._target_changed()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
