"""Two-step composer picker: model list, then the selected model's usage switch."""
from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from core import auxiliary_models
from core.jev_client import JEV_CONSOLE_URL, configuration_status as jev_status
from ui.theme import theme_manager


class ModelSelector(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("auxiliaryModelButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("보조 모델 선택")
        self.setToolTip("보조 모델을 선택한 뒤 사용 여부를 설정합니다")
        # K3 remains in the catalogue, but is deferred at the user's request.
        if auxiliary_models.selection() == ("kimi_k3", True):
            auxiliary_models.configure("kimi_k3", False)
        self.popup = QFrame(self, Qt.WindowType.Popup)
        self.popup.setObjectName("auxiliaryModelPopup")
        self.popup.setFixedWidth(304)
        root = QVBoxLayout(self.popup)
        root.setContentsMargins(14, 12, 14, 12)

        self.list_page = QWidget()
        listing = QVBoxLayout(self.list_page)
        listing.setContentsMargins(0, 0, 0, 0)
        listing.addWidget(QLabel("모델 선택"))
        self.models = QListWidget()
        self.models.setAccessibleName("사용 가능한 보조 모델")
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
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail)
        self.manage_jev_button = QPushButton("Jev 계정/API 키 관리")
        self.manage_jev_button.clicked.connect(self.open_jev_accounts)
        layout.addWidget(self.manage_jev_button)
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

    def refresh(self):
        model, enabled = auxiliary_models.selection()
        ready, reason = jev_status() if model == "jev" else (False, "연결 보류 · 설치하거나 실행하지 않습니다.")
        if enabled and not ready:
            auxiliary_models.configure(model, False)
            enabled = False
        for index in range(self.models.count()):
            item = self.models.item(index)
            key = item.data(Qt.ItemDataRole.UserRole)
            item.setText(auxiliary_models.AUXILIARY_MODELS[key] + ("  ✓" if key == model else ""))
            if key == model:
                self.models.setCurrentRow(index)
        self.slider.blockSignals(True)
        self.slider.setValue(int(enabled))
        self.slider.blockSignals(False)
        state = "ON" if enabled else "OFF"
        self.state_label.setText(state)
        self.model_title.setText(auxiliary_models.AUXILIARY_MODELS[model])
        self.setText(f"{auxiliary_models.AUXILIARY_MODELS[model]}  ·  {state}  ▾")
        self.description.setText(auxiliary_models.MODEL_DESCRIPTIONS[model])
        is_jev = model == "jev"
        self.slider.setEnabled(is_jev)
        self.jev_link.setVisible(is_jev)
        self.manage_jev_button.setVisible(is_jev)
        self.detail.setText(reason)
        self._runtime_status()
        self._theme()

    def _select(self, item):
        selected = item.data(Qt.ItemDataRole.UserRole)
        if selected != auxiliary_models.selection()[0]:
            auxiliary_models.configure(selected, False)
        self.refresh()
        self.list_page.hide()
        self.detail_page.show()
        self._position()
        (self.slider if self.slider.isEnabled() else self.back_button).setFocus()

    def _toggle(self, value):
        model, _ = auxiliary_models.selection()
        if value:
            available, reason = jev_status() if model == "jev" else (False, "Kimi K3 연결은 보류 중입니다.")
            if not available:
                self.refresh()
                self.detail.setText("ON 전환 불가 · " + reason)
                self._position()
                return
        auxiliary_models.configure(model, bool(value))
        self.refresh()

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
            self.runtime_status.setText(auxiliary_models.status())

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
            QListWidget {{ background: {c['surface']}; color: {c['text']}; border: 0; padding: 0; }}
            QListWidget::item {{ padding: 9px; }}
            QListWidget::item:selected {{ background: {c['selected']}; color: {c['selected_text']}; border-radius: 7px; }}
            QSlider::groove:horizontal {{ height: 24px; background: {track}; border-radius: 12px; }}
            QSlider::handle:horizontal {{ background: #ffffff; border: 1px solid {c['border']}; width: 28px; margin: -2px 0; border-radius: 14px; }}
            QSlider:focus {{ border: 1px solid {c['accent']}; border-radius: 12px; }}
        """)
