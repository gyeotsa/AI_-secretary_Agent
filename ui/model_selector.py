"""Two-step composer picker: model list, then the selected model's usage switch."""
from PyQt6.QtCore import QPoint, QThread, Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from core import auxiliary_models
from config import Config
from core.codex_client import CodexRuntimeError, get_codex_runtime
from core.jev_client import JEV_CONSOLE_URL, configuration_status as jev_status
from core.model_registry import get_model_registry
from ui.theme import theme_manager


_ACTIVE_CODEX_CHECKS: set[QThread] = set()


class _CodexCheck(QThread):
    def __init__(self, runtime, action, model=None):
        super().__init__()
        self.runtime, self.action, self.model = runtime, action, model
        self.result = None
        self.message = ""

    def run(self):
        try:
            self.result = (self.runtime.set_model(self.model) if self.action == "model"
                           else getattr(self.runtime, self.action)())
        except CodexRuntimeError as exc:
            self.message = str(exc)
        except Exception:
            self.message = "Codex 연결 실패 · 설치 상태와 ChatGPT 로그인을 확인하세요."


def _release_codex_worker(worker):
    _ACTIVE_CODEX_CHECKS.discard(worker)
    worker.deleteLater()


class ModelSelector(QPushButton):
    def __init__(self, parent=None, *, codex_runtime=None):
        super().__init__(parent)
        self._codex = codex_runtime if codex_runtime is not None else get_codex_runtime()
        self._codex_worker = None
        self._codex_data = {}
        self._codex_message = ""
        self._codex_verified = False
        self._codex_activate = False
        self._selected_model = auxiliary_models.main_selection()[0]
        self.setObjectName("auxiliaryModelButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("메인 및 보조 모델 선택")
        self.setToolTip("메인 모델은 하나만 사용하며 Jev 보조 모델은 함께 켤 수 있습니다")
        # K3 remains in the catalogue, but is deferred at the user's request.
        if auxiliary_models.is_enabled("kimi_k3"):
            auxiliary_models.configure("kimi_k3", False)
            self._selected_model = "default"
        self.popup = QFrame(self, Qt.WindowType.Popup)
        self.popup.setObjectName("auxiliaryModelPopup")
        self.popup.setFixedWidth(360)
        root = QVBoxLayout(self.popup)
        root.setContentsMargins(14, 12, 14, 12)

        self.list_page = QWidget()
        listing = QVBoxLayout(self.list_page)
        listing.setContentsMargins(0, 0, 0, 0)
        listing.addWidget(QLabel("메인 모델 1개 · Jev 보조 모델은 함께 사용"))
        self.models = QListWidget()
        self.models.setAccessibleName("메인 모델과 Jev 보조 모델")
        self.models.setFixedHeight(80)
        for key, label in auxiliary_models.AUXILIARY_MODELS.items():
            self.models.addItem(label)
            self.models.item(self.models.count() - 1).setData(Qt.ItemDataRole.UserRole, key)
        self.models.itemClicked.connect(self._select)
        self.models.itemActivated.connect(self._select)
        listing.addWidget(self.models)
        root.addWidget(self.list_page)

        self.detail_page = QWidget()
        layout = QVBoxLayout(self.detail_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.jev_link = QLabel(f'<a href="{JEV_CONSOLE_URL}">Jev API 키 발급 페이지 열기 ↗</a>')
        self.jev_link.setOpenExternalLinks(True)
        self.jev_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(self.jev_link)
        header = QHBoxLayout()
        self.back_button = QPushButton("‹ 모델")
        self.back_button.setAccessibleName("모델 목록으로 돌아가기")
        self.back_button.clicked.connect(self._show_list)
        header.addWidget(self.back_button)
        self.model_title = QLabel()
        self.model_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.model_title, 1)
        self.state_label = QLabel()
        header.addWidget(self.state_label)
        layout.addLayout(header)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setObjectName("auxiliaryModelUsage")
        self.slider.setRange(0, 1)
        self.slider.setFixedHeight(34)
        self.slider.setAccessibleName("선택한 모델 사용 OFF 또는 ON")
        self.slider.setToolTip("왼쪽 OFF · 오른쪽 ON (추론 강도가 아닌 사용 여부)")
        self.slider.valueChanged.connect(self._toggle)
        layout.addWidget(self.slider)
        ends = QHBoxLayout()
        ends.addWidget(QLabel("OFF"))
        ends.addStretch()
        ends.addWidget(QLabel("ON"))
        layout.addLayout(ends)
        self.description = QLabel()
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail)
        self.manage_jev_button = QPushButton("Jev 계정/API 키 관리")
        self.manage_jev_button.clicked.connect(self.open_jev_accounts)
        layout.addWidget(self.manage_jev_button)
        self.codex_model = QComboBox()
        self.codex_model.setAccessibleName("Codex에서 사용할 GPT 모델")
        self.codex_model.currentIndexChanged.connect(self._codex_model_changed)
        layout.addWidget(self.codex_model)
        self.connect_codex_button = QPushButton("ChatGPT 연결 / 로그인")
        self.connect_codex_button.clicked.connect(lambda: self._start_codex("login"))
        layout.addWidget(self.connect_codex_button)
        self.runtime_status = QLabel()
        self.runtime_status.setWordWrap(True)
        layout.addWidget(self.runtime_status)
        root.addWidget(self.detail_page)
        self._jev_dialog = None
        self.clicked.connect(self._open)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._runtime_status)
        self.timer.start()
        theme_manager().changed.connect(self._theme)
        self.refresh()
        self._show_list()
        if auxiliary_models.is_enabled("gpt"):
            self._start_codex("discover")

    def _default_detail(self):
        if Config.LLM_PROVIDER == "anthropic":
            return "기존 설정 · Anthropic\n사용 모델: " + Config.ANTHROPIC_MODEL
        registry = get_model_registry()
        detail = "기존 설정 · " + Config.LLM_PROVIDER
        for role, label in (("conversation", "대화"), ("code", "코드"), ("vision", "시각")):
            detail += "\n" + label + ": " + registry.resolve(role).model
        if Config.LLM_PROVIDER == "hybrid":
            detail += "\nClaude: " + Config.ANTHROPIC_MODEL
        return detail

    def refresh(self):
        model = self._selected_model
        enabled = auxiliary_models.is_enabled(model)
        ready, reason = (self._codex.status() if model == "gpt" else jev_status()
                         if model == "jev" else (True, self._default_detail()) if model == "default"
                         else (False, "연결 보류 · 설치하거나 실행하지 않습니다."))
        pending = model == "gpt" and not self._codex_verified and not ready
        if enabled and not ready and not pending:
            auxiliary_models.configure(model, False)
            enabled = False
        for index in range(self.models.count()):
            item = self.models.item(index)
            key = item.data(Qt.ItemDataRole.UserRole)
            on = auxiliary_models.is_enabled(key)
            role = "보조" if key == "jev" else "메인"
            item.setText(f"{auxiliary_models.AUXILIARY_MODELS[key]} · {role} · "
                         + ("ON  ✓" if on else "OFF"))
            if key == model:
                self.models.setCurrentRow(index)
        self.slider.blockSignals(True)
        self.slider.setValue(int(enabled and not pending))
        self.slider.blockSignals(False)
        state = "확인 중" if enabled and pending else "ON" if enabled else "OFF"
        self.state_label.setText(state)
        self.model_title.setText(auxiliary_models.AUXILIARY_MODELS[model]
                                 + (" · 보조" if model == "jev" else " · 메인"))
        main, _ = auxiliary_models.main_selection()
        name = auxiliary_models.AUXILIARY_MODELS[main]
        if main == "gpt" and self._codex.selected_model:
            name += " / " + self._codex.selected_model
        main_state = "확인 중" if main == "gpt" and not self._codex_verified and not self._codex.status()[0] else "ON"
        jev_state = "ON" if auxiliary_models.is_enabled("jev") else "OFF"
        self.setText(f"{name} · {main_state} / Jev · {jev_state} ▾")
        self.description.setText(auxiliary_models.MODEL_DESCRIPTIONS[model])
        is_jev = model == "jev"
        is_gpt = model == "gpt"
        busy = self._codex_worker is not None
        self.slider.setEnabled(is_jev or (is_gpt and ready and not busy))
        self.jev_link.setVisible(is_jev)
        self.manage_jev_button.setVisible(is_jev)
        self.codex_model.setVisible(is_gpt)
        self.codex_model.setEnabled(is_gpt and ready and not busy and self.codex_model.count() > 0)
        if not busy:
            self.codex_model.blockSignals(True)
            self.codex_model.setCurrentIndex(self.codex_model.findData(self._codex.selected_model))
            self.codex_model.blockSignals(False)
        self.connect_codex_button.setVisible(is_gpt)
        self.connect_codex_button.setEnabled(not busy)
        if is_gpt and ready:
            account = self._codex_data.get("account_label", "")
            reason += ("\n" + account if account else "")
            reason += "\n사용 모델: " + (self._codex.selected_model or "연결 후 확인")
        self.detail.setText(self._codex_message if is_gpt and self._codex_message else reason)
        self._runtime_status()
        self._theme()

    def _select(self, item):
        selected = item.data(Qt.ItemDataRole.UserRole)
        self._selected_model = selected
        self._codex_activate = False
        if selected != "jev" and selected != auxiliary_models.main_selection()[0]:
            auxiliary_models.configure("default", True)
            self._codex_activate = selected == "gpt"
        self.refresh()
        self.list_page.hide()
        self.detail_page.show()
        if selected == "gpt":
            self._start_codex("discover")
        self._position()
        (self.slider if self.slider.isEnabled() else self.back_button).setFocus()

    def _toggle(self, value):
        model = self._selected_model
        if value:
            available, reason = (self._codex.status() if model == "gpt" else jev_status()
                                 if model == "jev" else (False, "Kimi K3 연결은 보류 중입니다."))
            if model == "gpt" and self._codex_worker is not None:
                available, reason = False, "연결 확인이 끝난 뒤 ON으로 켜세요."
            if not available:
                self.refresh()
                self.detail.setText("ON 전환 불가 · " + reason)
                self._position()
                return
        auxiliary_models.configure(model, bool(value))
        self.refresh()

    def _start_codex(self, action, model=None):
        if self._codex_worker is not None or (self._selected_model != "gpt"
                                             and not auxiliary_models.is_enabled("gpt")):
            return
        worker = _CodexCheck(self._codex, action, model)
        self._codex_worker = worker
        _ACTIVE_CODEX_CHECKS.add(worker)
        worker.finished.connect(self._codex_finished)
        worker.finished.connect(lambda: _release_codex_worker(worker))
        self._codex_message = ("브라우저에서 ChatGPT 로그인을 완료하세요. 인증 후 직접 ON으로 켜세요."
                               if action == "login" else "Codex 모델 설정 중…"
                               if action == "model" else "Codex 연결 및 사용 가능한 모델 확인 중…")
        self.refresh()
        self._position()
        worker.start()

    def _codex_finished(self):
        worker, self._codex_worker = self._codex_worker, None
        if worker is None:
            return
        self._codex_message = worker.message
        if worker.action != "model":
            self._codex_verified = True
            failed = worker.message or not (worker.result or {}).get("authenticated")
            if failed and auxiliary_models.is_enabled("gpt"):
                auxiliary_models.configure("gpt", False)
            elif not failed and self._codex_activate and self._selected_model == "gpt":
                auxiliary_models.configure("gpt", True)
            self._codex_activate = False
        if isinstance(worker.result, dict):
            self._codex_data = worker.result
            self.codex_model.blockSignals(True)
            self.codex_model.clear()
            if worker.result.get("authenticated"):
                for model in worker.result.get("models", []):
                    self.codex_model.addItem(model.get("displayName") or model["model"], model["model"])
                index = self.codex_model.findData(worker.result.get("selected_model"))
                self.codex_model.setCurrentIndex(index)
            self.codex_model.blockSignals(False)
        self.refresh()
        if self.popup.isVisible() and self._selected_model == "gpt":
            self._position()

    def _codex_model_changed(self, index):
        selected = self.codex_model.itemData(index)
        if selected and selected != self._codex.selected_model:
            self._start_codex("model", selected)

    def open_jev_accounts(self):
        from .jev_account_dialog import JevAccountDialog
        self.popup.hide()
        if self._jev_dialog is None:
            self._jev_dialog = JevAccountDialog(parent=self)
            self._jev_dialog.records_changed.connect(self.refresh)
        self._jev_dialog.show()
        self._jev_dialog.raise_()
        self._jev_dialog.activateWindow()

    def _show_list(self):
        self.detail_page.hide()
        self.list_page.show()
        self.models.ensurePolished()
        self.models.setFixedHeight(sum(self.models.sizeHintForRow(i) for i in range(self.models.count())) + 4)
        self.models.scrollToTop()
        self._position()
        self.models.setFocus()

    def _position(self):
        self.popup.adjustSize()
        anchor = self.mapToGlobal(QPoint(self.width(), 0))
        screen = self.screen().availableGeometry()
        x = max(screen.left(), min(anchor.x() - self.popup.width(), screen.right() - self.popup.width()))
        y = max(screen.top(), anchor.y() - self.popup.height() - 8)
        self.popup.move(x, y)

    def _open(self):
        self.refresh()
        self._show_list()
        self.popup.show()
        self._position()
        self.models.setFocus()

    def _runtime_status(self):
        if self.popup.isVisible():
            self.runtime_status.setText(auxiliary_models.status(self._selected_model))

    def _theme(self, *_):
        c = theme_manager().colors
        self.jev_link.setText(f'<a style="color: {c["accent"]}" href="{JEV_CONSOLE_URL}">Jev API 키 발급 페이지 열기 ↗</a>')
        track = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #5945e8, stop:1 #a777fa)"
                 if self.slider.value() else c["border"])
        self.popup.setStyleSheet(f"""
            QFrame#auxiliaryModelPopup {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 14px; }}
            QLabel {{ color: {c['text']}; background: transparent; border: 0; }}
            QPushButton {{ background: {c['input']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 6px; }}
            QPushButton:focus {{ border-color: {c['accent']}; }}
            QComboBox {{ background: {c['input']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 6px; }}
            QComboBox QAbstractItemView {{ background: {c['surface']}; color: {c['text']}; selection-background-color: {c['selected']}; selection-color: {c['selected_text']}; }}
            QListWidget {{ background: {c['surface']}; color: {c['text']}; border: 0; padding: 0; }}
            QListWidget::item {{ padding: 9px; }}
            QListWidget::item:selected {{ background: {c['selected']}; color: {c['selected_text']}; border-radius: 7px; }}
            QSlider::groove:horizontal {{ height: 24px; background: {track}; border-radius: 12px; }}
            QSlider::handle:horizontal {{ background: #ffffff; border: 1px solid {c['border']}; width: 28px; margin: -2px 0; border-radius: 14px; }}
            QSlider:focus {{ border: 1px solid {c['accent']}; border-radius: 12px; }}
        """)
