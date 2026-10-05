"""Direct user OAuth input; secrets never enter the chat, labels, or logs."""
from __future__ import annotations

import os
import re
from datetime import datetime

from PyQt6.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from core.oauth_connection import OAuthConsentDenied, OAuthLoopbackConnection
from core.plugin import ToolCancelledError
from .dialog_theme import apply_dark_dialog_theme


_ACTIVE_CONNECTIONS: set[QThread] = set()


class _OAuthConnect(QThread):
    outcome = pyqtSignal(str, object)
    browser_started = pyqtSignal(str)

    def __init__(self, connection, provider, account, credentials):
        super().__init__()
        self.connection = connection
        self.provider, self.account = provider, account
        self.credentials = credentials
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True
        self.connection.cancel()

    def run(self):
        try:
            result = self.connection.connect(self.provider, self.account,
                                             on_browser=self.browser_started.emit, **self.credentials)
            self.outcome.emit("completed", result)
        except ToolCancelledError:
            self.outcome.emit("cancelled", {})
        except OAuthConsentDenied:
            self.outcome.emit("denied", {})
        except Exception:
            self.outcome.emit("failed", {})
        finally:
            self.credentials.clear()


def _release_worker(worker):
    _ACTIVE_CONNECTIONS.discard(worker)
    worker.deleteLater()


class OAuthAccountDialog(QDialog):
    settings_changed = pyqtSignal()

    def __init__(self, oauth, parent=None, *, connection_factory=OAuthLoopbackConnection):
        super().__init__(parent)
        self.oauth, self.connection_factory = oauth, connection_factory
        self._worker = None
        self._pending_outcome = None
        self._pending_close = None
        self._configured = False
        self.setWindowTitle("Google · Microsoft 계정 연결")
        self.setMinimumWidth(580)
        apply_dark_dialog_theme(self)
        layout = QVBoxLayout(self)
        intro = QLabel("로그인과 동의는 기본 브라우저에서 직접 진행합니다. 비밀번호·인증 코드·토큰을 채팅에 입력하지 마세요.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(intro)
        form = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItem("Google", "google")
        self.provider.addItem("Microsoft", "microsoft")
        self.account_input = QLineEdit()
        self.account_input.setMaxLength(320)
        self.account_input.setPlaceholderText("연결할 계정 이메일 (다른 계정으로 로그인하면 저장하지 않음)")
        self.client_id_input = QLineEdit()
        self.client_id_input.setMaxLength(2048)
        self.client_id_input.setPlaceholderText("직접 발급한 OAuth 앱 Client ID")
        self.client_secret_input = QLineEdit()
        self.client_secret_input.setMaxLength(4096)
        self.client_secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_input.setInputMethodHints(Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText)
        self.client_secret_input.setPlaceholderText("필요한 앱만 입력 · 저장된 Secret은 표시하지 않음")
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("자동 (Google)")
        form.addRow("제공자", self.provider)
        form.addRow("계정 이메일", self.account_input)
        form.addRow("OAuth Client ID", self.client_id_input)
        form.addRow("OAuth Client Secret", self.client_secret_input)
        form.addRow("콜백 포트", self.port)
        layout.addLayout(form)
        self.setup_label = QLabel()
        self.setup_label.setWordWrap(True)
        self.setup_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.setup_label)
        self.status_label = QLabel("계정 이메일을 입력해 저장 상태를 확인하세요.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        actions = QHBoxLayout()
        self.connect_button = QPushButton("브라우저에서 연결 / 재연결")
        self.status_button = QPushButton("로컬 저장 상태")
        self.disconnect_button = QPushButton("로컬 연결 해제")
        for button in (self.connect_button, self.status_button, self.disconnect_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)
        footer = QHBoxLayout()
        self.cancel_button = QPushButton("연결 취소")
        self.close_button = QPushButton("닫기")
        self.cancel_button.setAutoDefault(False)
        self.close_button.setAutoDefault(False)
        footer.addStretch()
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.connect_button.clicked.connect(self._connect)
        self.status_button.clicked.connect(self._refresh)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.cancel_button.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.reject)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.account_input.textChanged.connect(self._account_changed)
        self.client_id_input.textChanged.connect(self._update_controls)
        self.port.valueChanged.connect(self._setup_text)
        self._provider_changed()

    def _provider_changed(self, *_args):
        self.client_secret_input.clear()
        self._configured = False
        provider = self.provider.currentData()
        self.client_id_input.setText(os.getenv(self.oauth.SPECS[provider]["client_id"], ""))
        try:
            self.port.setValue(OAuthLoopbackConnection.default_port(provider))
        except Exception:
            self.port.setValue(8765 if provider == "microsoft" else 0)
        self._setup_text()
        self._account_changed()

    def _setup_text(self, *_args):
        if self.provider.currentData() == "google":
            text = "Google Cloud에서 Desktop app 클라이언트를 사용하세요. Gmail·Calendar·Drive 접근 동의를 요청합니다."
        else:
            text = ("Microsoft 앱 등록에 Public client 리디렉션 URI를 정확히 등록하세요:\n"
                    f"http://127.0.0.1:{self.port.value()}/oauth/callback\n"
                    "프로필·메일·캘린더·파일·Teams 범위를 요청하며 조직의 관리자 동의가 필요할 수 있습니다.")
        self.setup_label.setText(text + "\n연결 완료 시 앱 자격증명과 토큰은 Windows DPAPI로 암호화 저장됩니다. 조회·전송은 별도 작업입니다.")
        self._update_controls()

    def _account_valid(self):
        return bool(re.fullmatch(r"[^\s@]{1,160}@[^\s@]{1,160}", self.account_input.text()))

    def _account_changed(self, *_args):
        self._configured = False
        self.status_label.setText("계정 이메일을 입력해 로컬 저장 상태를 확인하세요. 저장 여부는 현재 접속 성공을 뜻하지 않습니다.")
        self._update_controls()

    def _update_controls(self, *_args):
        busy = self._worker is not None
        valid = self._account_valid()
        for widget in (self.provider, self.account_input, self.client_id_input, self.client_secret_input, self.port):
            widget.setEnabled(not busy)
        port_valid = self.provider.currentData() != "microsoft" or self.port.value() > 0
        self.connect_button.setEnabled(not busy and valid and bool(self.client_id_input.text().strip()) and port_valid)
        self.status_button.setEnabled(not busy and valid)
        self.disconnect_button.setEnabled(not busy and self._configured)
        self.cancel_button.setEnabled(busy and not self._worker.cancel_requested and self._pending_close is None)

    def _apply_status(self, status):
        if (not isinstance(status, dict) or type(status.get("configured")) is not bool
                or status.get("provider") != self.provider.currentData()
                or status.get("account") != self.account_input.text()):
            raise ValueError("OAuth status target mismatch")
        self._configured = status["configured"]
        if not self._configured:
            self.status_label.setText("이 제공자·계정에 저장된 로컬 연결이 없습니다.")
        else:
            identity = status.get("identity_email", "")
            verified = status.get("identity_verified_at")
            if not isinstance(identity, str) or not re.fullmatch(r"[^\s@]{1,160}@[^\s@]{1,160}", identity):
                identity = "신원 미확인 · 재연결 필요"
            try:
                checked = datetime.fromtimestamp(verified).isoformat(timespec="seconds") if type(verified) in (int, float) else "미확인"
            except (ValueError, OSError, OverflowError):
                checked = "미확인"
            scopes = status.get("granted_scopes", [])
            known = self.oauth.SPECS[self.provider.currentData()]["scopes"]
            granted = {str(scope).removeprefix("https://graph.microsoft.com/").casefold()
                       for scope in scopes} if isinstance(scopes, list) else set()
            # Only fixed application scopes reach a label; not arbitrary token/server text.
            allowed = ", ".join(scope for scope in known if scope.casefold() in granted) or "없음 / 미확인"
            scope_state = "응답에 명시된 허용 범위" if status.get("scopes_verified") is True else "허용 범위 미확인"
            token_state = "로컬 토큰 유효기간 내" if status.get("authenticated") is True else "토큰 만료 / 인증 미확인"
            refresh = "갱신 토큰 있음" if status.get("refresh_available") is True else "갱신 토큰 없음 · 만료 시 재연결"
            self.status_label.setText(f"암호화 저장됨 · {identity}\n마지막 신원 확인: {checked}\n{token_state} · {refresh}\n{scope_state}: {allowed}\n현재 API 접속·모든 기능의 실행 성공은 별도로 확인해야 합니다.")
        self._update_controls()

    @pyqtSlot()
    def _refresh(self):
        if self._worker is not None or not self._account_valid():
            return
        try:
            self._apply_status(self.oauth.status(self.provider.currentData(), self.account_input.text()))
        except Exception:
            self._configured = False
            self.status_label.setText("저장된 연결을 확인하지 못했습니다. 암호화 저장소와 계정을 확인하세요.")
            self._update_controls()

    @pyqtSlot()
    def _connect(self):
        if self._worker is not None or not self.connect_button.isEnabled():
            return
        connection = self.connection_factory(self.oauth)
        worker = _OAuthConnect(connection, self.provider.currentData(), self.account_input.text(),
                               {"client_id": self.client_id_input.text().strip(),
                                "client_secret": self.client_secret_input.text() or None,
                                "port": self.port.value()})
        self.client_secret_input.clear()
        self._worker = worker
        self._pending_outcome = None
        _ACTIVE_CONNECTIONS.add(worker)
        worker.browser_started.connect(self._browser_started)
        worker.outcome.connect(self._record_outcome)
        worker.finished.connect(self._connect_finished)
        worker.finished.connect(lambda: _release_worker(worker))
        self.destroyed.connect(worker.cancel)
        self.status_label.setText("브라우저 로그인과 동의를 기다립니다. 연결 대기는 최대 3분이며 취소할 수 있습니다.")
        self._update_controls()
        worker.start()

    @pyqtSlot(str)
    def _browser_started(self, _redirect):
        # Callback URLs are not copied into chat or logs.
        if self._worker is not None and not self._worker.cancel_requested:
            self.status_label.setText("기본 브라우저에서 선택한 계정으로 로그인하고 접근 범위를 확인하세요. 아직 연결 완료가 아닙니다.")

    @pyqtSlot(str, object)
    def _record_outcome(self, kind, payload):
        self._pending_outcome = kind, payload

    @pyqtSlot()
    def _connect_finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                self.destroyed.disconnect(worker.cancel)
            except (TypeError, RuntimeError):
                pass
        kind, payload = self._pending_outcome or ("failed", {})
        self._pending_outcome = None
        if kind == "completed":
            self.settings_changed.emit()
        if self._pending_close is not None:
            result, self._pending_close = self._pending_close, None
            self.done(result)
            return
        if worker is not None and worker.cancel_requested:
            self._refresh()
            self.status_label.setText("연결 취소를 요청했습니다. 취소 직전에 저장이 완료됐을 수 있으니 로컬 저장 상태를 확인하세요.")
        elif kind == "completed":
            try:
                self._apply_status(payload)
            except Exception:
                self.status_label.setText("연결 결과의 제공자·계정·저장 상태를 확인하지 못했습니다.")
        elif kind == "denied":
            self.status_label.setText("브라우저에서 동의를 거부했습니다. 새 연결은 저장하지 않았습니다.")
        elif kind == "cancelled":
            self.status_label.setText("계정 연결을 취소했습니다. 기존에 저장된 연결은 유지됩니다.")
        else:
            self.status_label.setText("연결을 완료하지 못했습니다. 앱 등록·리디렉션 URI·네트워크·선택 계정을 확인하세요. 기존 연결은 유지됩니다.")
        self._update_controls()

    @pyqtSlot()
    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("연결 취소 요청 중 · 진행 중인 제한 시간 HTTP 요청이 정리될 때까지 기다립니다.")
            self._update_controls()

    @pyqtSlot()
    def _disconnect(self):
        if self._worker is not None or not self._configured:
            return
        if QMessageBox.question(self, "로컬 연결 해제", "이 제공자·계정의 암호화된 로컬 토큰과 앱 자격증명만 삭제할까요?\n제공자 계정의 앱 접근 권한은 Google 또는 Microsoft에서 별도로 철회해야 합니다.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.oauth.vault.delete(self.provider.currentData(), self.account_input.text())
            self._refresh()
            self.settings_changed.emit()
        except Exception:
            self.status_label.setText("로컬 연결 해제를 확인하지 못했습니다.")

    def done(self, result):
        self.client_secret_input.clear()
        if self._worker is not None:
            self._pending_close = int(result)
            self._cancel()
            return
        super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        self.client_secret_input.clear()
        if self._worker is not None:
            self._pending_close = int(QDialog.DialogCode.Rejected)
            self._cancel()
            event.ignore()
            return
        super().closeEvent(event)


def open_oauth_account_dialog(registry, parent):
    plugin = registry.get_plugin("cloud_communication")
    if plugin is None or not hasattr(plugin, "oauth"):
        QMessageBox.warning(parent, "계정 연결", "클라우드 플러그인이 등록되지 않았습니다.")
        return
    dialog = OAuthAccountDialog(plugin.oauth, parent)
    dialog.settings_changed.connect(parent.refresh)
    dialog.exec()
    parent.refresh()
