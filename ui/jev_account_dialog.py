"""Jev account manager. Keys are write-only UI input backed by the DPAPI vault."""
from __future__ import annotations

import threading
from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from core import jev_client
from core.jev_client import JEV_CONSOLE_URL, JevError
from core.plugin import ToolCancelledError
from .theme import set_widget_style, theme_manager

# Keep a parentless check alive even if the dialog/window is destroyed.
_ACTIVE_CHECKS: set[QThread] = set()


class _ConnectionCheck(QThread):
    def __init__(self, service, account_id):
        super().__init__()
        self.service, self.account_id = service, account_id
        self.cancelled = threading.Event()
        self.message = "Jev 연결 확인을 완료하지 못했습니다."

    def cancel(self):
        self.cancelled.set()

    def checkpoint(self):
        if self.cancelled.is_set():
            raise ToolCancelledError("Jev 연결 확인 취소")

    def run(self):
        try:
            self.checkpoint()
            self.service.verify(self.account_id, checkpoint=self.checkpoint)
            self.checkpoint()
            self.message = "인증 확인 완료 · 사용할 계정을 적용한 뒤 채팅바에서 Jev를 ON으로 켜세요."
        except ToolCancelledError:
            self.message = "연결 확인 취소 · 암호화 저장된 계정은 유지됩니다."
        except JevError as exc:
            self.message = str(exc)
        except Exception:
            self.message = "Jev 연결 확인 실패 · 네트워크와 계정 설정을 확인하세요."


def _release_worker(worker):
    _ACTIVE_CHECKS.discard(worker)
    worker.deleteLater()


class JevAccountDialog(QDialog):
    records_changed = pyqtSignal()

    def __init__(self, service=None, parent=None):
        super().__init__(parent)
        self.service = service if service is not None else jev_client.JevAccounts()
        self._editing_id = None
        self._records = []
        self._worker = None
        self.setWindowTitle("Jev 계정 및 API 키")
        self.resize(650, 550)
        root = QVBoxLayout(self)
        title = QLabel("Jev 계정 및 API 키")
        title.setObjectName("heading")
        root.addWidget(title)
        intro = QLabel(
            "TypeSafe Console에서 직접 발급한 키를 등록하세요. 계정 정보와 키는 이 Windows "
            "사용자의 암호화 저장소에 보관됩니다. 로그인 비밀번호는 받지 않습니다.\n"
            "ON 시 현재 입력과 최근 대화 일부가 TypeSafe로 전송됩니다. 기본 대화 모델은 유지됩니다."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.link = QLabel()
        self.link.setOpenExternalLinks(True)
        self.link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        root.addWidget(self.link)
        form = QFormLayout()
        self.account_name = QLineEdit()
        self.account_name.setMaxLength(80)
        self.account_name.setPlaceholderText("예: 개인 계정")
        form.addRow("계정 이름", self.account_name)
        self.account_id = QLineEdit()
        self.account_id.setMaxLength(200)
        self.account_id.setPlaceholderText("이메일 또는 계정 식별자 (선택)")
        form.addRow("식별자", self.account_id)
        self.api_key = QLineEdit()
        self.api_key.setMaxLength(4096)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setInputMethodHints(
            Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText
            | Qt.InputMethodHint.ImhNoAutoUppercase)
        self.api_key.setPlaceholderText("API 키를 입력하세요")
        form.addRow("API 키", self.api_key)
        root.addLayout(form)
        controls = QHBoxLayout()
        self.save_button = QPushButton("저장 후 연결 확인")
        self.save_button.clicked.connect(self._save)
        controls.addWidget(self.save_button)
        self.clear_button = QPushButton("새 계정 입력")
        self.clear_button.clicked.connect(self._clear_form)
        controls.addWidget(self.clear_button)
        controls.addStretch()
        root.addLayout(controls)
        self.account_list = QListWidget()
        self.account_list.setAccessibleName("암호화 저장된 Jev 계정 목록")
        self.account_list.itemDoubleClicked.connect(lambda _item: self._load_selected())
        root.addWidget(self.account_list, 1)
        actions = QHBoxLayout()
        self.edit_button = QPushButton("수정")
        self.edit_button.clicked.connect(self._load_selected)
        self.remove_button = QPushButton("제거")
        self.remove_button.clicked.connect(self._remove_selected)
        self.verify_button = QPushButton("연결 확인")
        self.verify_button.clicked.connect(self._verify)
        self.use_button = QPushButton("이 계정 사용")
        self.use_button.clicked.connect(self._activate)
        for button in (self.edit_button, self.remove_button, self.verify_button, self.use_button):
            actions.addWidget(button)
        root.addLayout(actions)
        footer = QHBoxLayout()
        self.cancel_button = QPushButton("확인 취소")
        self.cancel_button.clicked.connect(self._cancel)
        footer.addWidget(self.cancel_button)
        footer.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        footer.addWidget(close)
        root.addLayout(footer)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        set_widget_style(self, "")
        theme_manager().changed.connect(self._theme)
        self._theme()
        self._refresh()

    def _theme(self, *_):
        color = theme_manager().colors["accent"]
        self.link.setText(f'<a style="color: {color}" href="{JEV_CONSOLE_URL}">TypeSafe Console에서 Jev API 키 발급하기 ↗</a>')

    def _selected_id(self):
        item = self.account_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _refresh(self, selected=None):
        selected = selected or self._selected_id()
        try:
            self._records = self.service.list()
        except JevError as exc:
            self.status.setText(str(exc))
            self._records = []
        self.account_list.clear()
        for record in self._records:
            state = ("적용 계정 · " if record["active"] else "") + (
                "인증 확인됨" if record["verified_at"] else "연결 미확인")
            item = QListWidgetItem(
                f"{record['name']} · {state}\n"
                f"{record['identifier'] or '식별자 없음'} · API Key {record['masked_key']}")
            # Only a non-secret ID is stored in Qt item data.
            item.setData(Qt.ItemDataRole.UserRole, record["id"])
            self.account_list.addItem(item)
            if record["id"] == selected:
                self.account_list.setCurrentItem(item)
        self._update_controls()

    def _update_controls(self):
        busy = self._worker is not None
        for widget in (self.account_name, self.account_id, self.api_key, self.save_button,
                       self.clear_button, self.account_list, self.edit_button,
                       self.remove_button, self.verify_button, self.use_button):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def _save(self):
        if self._worker is not None:
            return
        try:
            account_id = self.service.save(self.account_name.text(), self.account_id.text(),
                                           self.api_key.text(), self._editing_id)
        except JevError as exc:
            self.status.setText(str(exc))
            return
        finally:
            self.api_key.clear()
        self._clear_form()
        self._refresh(account_id)
        self.records_changed.emit()
        self._verify()

    def _verify(self):
        account_id = self._selected_id()
        if self._worker is not None:
            return
        if not account_id:
            self.status.setText("먼저 연결을 확인할 계정을 선택하세요.")
            return
        worker = _ConnectionCheck(self.service, account_id)
        self._worker = worker
        _ACTIVE_CHECKS.add(worker)
        worker.finished.connect(self._check_finished)
        worker.finished.connect(lambda: _release_worker(worker))
        self.destroyed.connect(worker.cancel)
        self.status.setText("Jev 인증 확인 중 · 모델 목록만 조회하며 분류 요청은 보내지 않습니다.")
        self._update_controls()
        worker.start()

    def _check_finished(self):
        worker, self._worker = self._worker, None
        if worker is None:
            return
        self.destroyed.disconnect(worker.cancel)
        self._refresh(worker.account_id)
        self.status.setText("연결 확인을 취소했습니다." if worker.cancelled.is_set() else worker.message)
        self.records_changed.emit()

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status.setText("연결 확인을 취소하고 있습니다.")

    def _activate(self):
        if self._worker is not None:
            return
        try:
            self.service.activate(self._selected_id())
            self._refresh()
            self.records_changed.emit()
            self.status.setText("선택한 계정을 적용했습니다. 모델 사용 여부는 채팅바 ON/OFF에서 설정합니다.")
        except JevError as exc:
            self.status.setText(str(exc))

    def _load_selected(self):
        if self._worker is not None:
            return
        record = next((r for r in self._records if r["id"] == self._selected_id()), None)
        if record is None:
            self.status.setText("먼저 수정할 계정을 선택하세요.")
            return
        self._editing_id = record["id"]
        self.account_name.setText(record["name"])
        self.account_id.setText(record["identifier"])
        self.api_key.clear()
        self.api_key.setPlaceholderText("기존 키 유지 · 교체할 때만 새 키 입력")
        self.status.setText("기존 API 키는 다시 표시하지 않습니다.")

    def _clear_form(self):
        self._editing_id = None
        self.account_name.clear()
        self.account_id.clear()
        self.api_key.clear()
        self.api_key.setPlaceholderText("API 키를 입력하세요")

    def _remove_selected(self):
        account_id = self._selected_id()
        if self._worker is not None:
            return
        if not account_id:
            self.status.setText("먼저 제거할 계정을 선택하세요.")
            return
        if QMessageBox.question(
                self, "로컬 Jev 계정 제거",
                "아니스에 저장된 이 계정과 API 키만 제거합니다.\n"
                "TypeSafe의 키 자체는 Console에서 별도로 폐기해야 합니다. 계속할까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.delete(account_id)
            self._clear_form()
            self._refresh()
            self.records_changed.emit()
            self.status.setText("로컬 계정과 키를 제거했습니다. TypeSafe의 키 자체는 유지됩니다.")
        except JevError as exc:
            self.status.setText(str(exc))

    def showEvent(self, event):
        self._refresh()
        super().showEvent(event)

    def done(self, result):
        self.api_key.clear()
        self._cancel()
        super().done(result)

    def closeEvent(self, event):
        self.api_key.clear()
        self._cancel()
        super().closeEvent(event)
