"""Single-account Naver mail settings; verification never reads or sends mail."""
from __future__ import annotations

from datetime import datetime
import threading

from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout,
)

from core.plugin import ToolCancelledError


# A parent window may itself be destroyed while an SSL operation is finishing.
# Retain the parentless worker until finished; never destroy/terminate a running
# QThread or make worker code depend on a widget's lifetime.
_ACTIVE_CHECKS: set[QThread] = set()


class _ConnectionCheck(QThread):
    outcome = pyqtSignal(str, object)

    def __init__(self, service):
        super().__init__()
        self._service = service
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()

    @property
    def cancel_requested(self):
        return self._cancelled.is_set()

    def checkpoint(self):
        if self._cancelled.is_set():
            raise ToolCancelledError("메일 연결 검사가 취소되었습니다.")

    def run(self):
        try:
            self.checkpoint()
            result = self._service.verify(checkpoint=self.checkpoint)
            self.checkpoint()
            self.outcome.emit("completed", result)
        except ToolCancelledError:
            self.outcome.emit("cancelled", {})
        except Exception:
            # Exception messages and arbitrary service reasons can contain
            # credentials or server text. Never forward them to labels/logs.
            self.outcome.emit("failed", {})


def _release_worker(worker):
    _ACTIVE_CHECKS.discard(worker)
    worker.deleteLater()


class MailAccountDialog(QDialog):
    """UI adapter for an injected status/save/verify/disconnect service.

    Passwords are write-only input, cleared after every save attempt and close.
    Connection checks authenticate IMAP/SMTP only; the service must not read
    messages or send mail. Saved configuration is not authentication evidence.
    """

    settings_changed = pyqtSignal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._worker = None
        self._pending_outcome = None
        self._pending_close = None
        self._configured = False
        self._saved_username = ""
        self.setWindowTitle("네이버 메일 계정")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        intro = QLabel("네이버 메일 계정 하나를 이 앱에 연결합니다. 일반 비밀번호 대신 앱 비밀번호를 사용하세요.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(intro)
        form = QFormLayout()
        self.username_input = QLineEdit()
        self.username_input.setObjectName("mailAccountUsername")
        self.username_input.setMaxLength(320)
        self.username_input.setPlaceholderText("네이버 메일 계정")
        self.password_input = QLineEdit()
        self.password_input.setObjectName("mailAccountPassword")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setMaxLength(4096)
        self.password_input.setInputMethodHints(
            Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText
            | Qt.InputMethodHint.ImhNoAutoUppercase,
        )
        self.password_input.setPlaceholderText("저장된 비밀번호는 표시하지 않습니다")
        form.addRow("계정", self.username_input)
        form.addRow("앱 비밀번호", self.password_input)
        layout.addLayout(form)
        self.status_label = QLabel()
        self.status_label.setObjectName("mailAccountStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        note = QLabel("연결 검사는 IMAP·SMTP 로그인만 확인하며 메일 목록·본문 조회나 전송은 하지 않습니다.")
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)

        actions = QHBoxLayout()
        self.save_button = QPushButton("암호화 저장")
        self.verify_button = QPushButton("연결 검사")
        self.disconnect_button = QPushButton("로컬 연결 해제")
        for button in (self.save_button, self.verify_button, self.disconnect_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel_button = QPushButton("검사 취소")
        self.close_button = QPushButton("닫기")
        self.cancel_button.setAutoDefault(False)
        self.close_button.setAutoDefault(False)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.save_button.clicked.connect(self._save)
        self.verify_button.clicked.connect(self._verify)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.cancel_button.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.reject)
        self.username_input.textChanged.connect(self._update_controls)
        self.password_input.textChanged.connect(self._update_controls)
        try:
            self._apply_status(service.status())
        except Exception:
            self.status_label.setText("저장된 연결 상태를 확인하지 못했습니다. 비밀번호는 표시하지 않습니다.")
            self._update_controls()

    @staticmethod
    def _verified_time(value):
        if not isinstance(value, str) or len(value) > 64:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(timespec="seconds")
        except ValueError:
            return None

    def _apply_status(self, status, *, freshly_saved=False):
        if not isinstance(status, dict) or type(status.get("configured")) is not bool:
            raise ValueError("Invalid mail settings status")
        username = status.get("username", "")
        if status["configured"] and (
                not isinstance(username, str) or not username.strip() or len(username) > 320):
            raise ValueError("Configured mail account has no valid identity")
        self._configured = status["configured"]
        self._saved_username = username if self._configured and isinstance(username, str) else ""
        self.username_input.setText(self._saved_username)
        self.password_input.clear()
        verified = None if freshly_saved else self._verified_time(status.get("verified_at"))
        if not self._configured:
            self.status_label.setText("저장된 로컬 연결이 없습니다.")
        elif verified:
            self.status_label.setText(f"암호화 저장됨 · 마지막 연결 확인: {verified}\n저장 상태와 현재 접속 가능 여부는 다릅니다.")
        else:
            self.status_label.setText("암호화 저장됨 · 연결 미확인. 저장만으로 로그인 성공을 확인한 것은 아닙니다.")
        self._update_controls()

    def _update_controls(self, *_args):
        busy = self._worker is not None
        dirty = (self.username_input.text() != self._saved_username or bool(self.password_input.text()))
        self.username_input.setEnabled(not busy)
        self.password_input.setEnabled(not busy)
        self.save_button.setEnabled(not busy and bool(self.username_input.text().strip()) and bool(self.password_input.text()))
        self.verify_button.setEnabled(not busy and self._configured and not dirty)
        self.disconnect_button.setEnabled(not busy and self._configured)
        self.cancel_button.setEnabled(busy and self._pending_close is None and not self._worker.cancel_requested)

    @pyqtSlot()
    def _save(self):
        if self._worker is not None or not self.save_button.isEnabled():
            return
        try:
            result = self.service.save(self.username_input.text(), self.password_input.text())
            if not isinstance(result, dict) or result.get("configured") is not True:
                raise ValueError("Credential save was not confirmed")
            self._apply_status(result, freshly_saved=True)
            self.settings_changed.emit()
        except Exception:
            self.status_label.setText("암호화 저장을 완료하지 못했습니다. 계정과 앱 비밀번호를 다시 확인하세요.")
        finally:
            self.password_input.clear()
            self._update_controls()

    @pyqtSlot()
    def _verify(self):
        if self._worker is not None or not self.verify_button.isEnabled():
            return
        worker = _ConnectionCheck(self.service)
        self._worker = worker
        self._pending_outcome = None
        _ACTIVE_CHECKS.add(worker)
        worker.outcome.connect(self._record_outcome)
        worker.finished.connect(self._check_finished)
        worker.finished.connect(lambda: _release_worker(worker))
        # Also cancel if an owning parent is destroyed directly, bypassing this
        # dialog's normal close path. The thread remains retained until finished.
        self.destroyed.connect(worker.cancel)
        self.status_label.setText("IMAP·SMTP 로그인을 확인하고 있습니다. 메일은 조회하거나 전송하지 않습니다.")
        self._update_controls()
        worker.start()

    @pyqtSlot(str, object)
    def _record_outcome(self, kind, payload):
        self._pending_outcome = (kind, payload)

    @pyqtSlot()
    def _check_finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                self.destroyed.disconnect(worker.cancel)
            except (TypeError, RuntimeError):
                pass
        kind, payload = self._pending_outcome or ("failed", {})
        # Cancellation can be requested after run() emitted its result but
        # before the queued GUI signal is delivered. Do not display stale success.
        if worker is not None and worker.cancel_requested:
            kind = "cancelled"
        self._pending_outcome = None
        self._update_controls()
        if self._pending_close is not None:
            result, self._pending_close = self._pending_close, None
            self.done(result)
            return
        if kind == "cancelled":
            self.status_label.setText("연결 검사를 취소했습니다. 저장된 로컬 연결 정보는 유지됩니다.")
        elif kind == "completed" and isinstance(payload, dict):
            imap = payload.get("imap_authenticated") is True
            smtp = payload.get("smtp_authenticated") is True
            verified = self._verified_time(payload.get("verified_at"))
            if imap and smtp and verified:
                self.status_label.setText(f"IMAP·SMTP 로그인 확인 완료 · {verified}\n메일 조회·전송은 하지 않았습니다.")
                self.settings_changed.emit()
            else:
                self.status_label.setText(
                    f"IMAP 로그인: {'확인' if imap else '미확인'} · SMTP 로그인: {'확인' if smtp else '미확인'}\n"
                    "연결 검증을 완료하지 못했습니다. 계정 설정과 앱 비밀번호를 확인하세요.")
        else:
            self.status_label.setText("연결 검사를 완료하지 못했습니다. 네트워크와 계정 설정을 확인하세요.")

    @pyqtSlot()
    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("검사 취소를 요청했습니다. 진행 중인 연결 작업이 정리될 때까지 기다립니다.")

    @pyqtSlot()
    def _disconnect(self):
        if self._worker is not None or not self._configured:
            return
        answer = QMessageBox.question(
            self, "로컬 연결 해제",
            "이 앱에 저장된 로컬 네이버 메일 연결 정보만 삭제합니다.\n"
            "네이버 앱 비밀번호는 네이버 계정 설정에서 별도로 폐기해야 합니다.\n계속하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._apply_status(self.service.disconnect())
            if self._configured:
                raise ValueError("Disconnect not confirmed")
            self.status_label.setText("로컬 연결 정보를 삭제했습니다. 네이버 앱 비밀번호는 네이버에서 별도로 폐기하세요.")
            self.settings_changed.emit()
        except Exception:
            self.status_label.setText("로컬 연결 해제를 확인하지 못했습니다. 저장 상태를 다시 확인하세요.")

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
