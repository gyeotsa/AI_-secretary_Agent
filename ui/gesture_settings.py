"""User-facing gesture calibration and command-mapping controls."""
from __future__ import annotations
from .theme import set_widget_style

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from core.gesture_runtime import (
    DEFAULT_GESTURE_MAPPING,
    GESTURE_ACTION_LABELS,
    GESTURE_POSE_LABELS,
    normalize_gesture_mapping,
    normalize_gesture_sensitivity,
)


class GestureSettingsDialog(QDialog):
    """Edit live gesture response and safe discrete-command mappings.

    Slider/mapping changes are previewed immediately. Cancel restores the
    configuration that was active when the dialog opened; Save asks the app to
    persist the same values through AssistantSettings.
    """

    preview_changed = pyqtSignal(object)
    settings_saved = pyqtSignal(object)

    def __init__(self, configuration: dict | None = None, status: dict | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("손 제스처 설정")
        self.setMinimumWidth(520)
        source = dict(configuration or {})
        self._original = {
            "sensitivity": normalize_gesture_sensitivity(source.get("sensitivity", 60)),
            "command_gestures_enabled": bool(source.get("command_gestures_enabled", False)),
            "gesture_mapping": normalize_gesture_mapping(source.get("gesture_mapping")),
        }
        self._status = dict(status or {})
        self._mapping_boxes: dict[str, QComboBox] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        set_widget_style(self, """
            QDialog, QFrame { background: #091322; color: #dcecff; }
            QLabel#title { color: #f0f8ff; font-size: 19px; font-weight: 700; }
            QLabel#description { color: #8fa9c2; }
            QLabel#value { color: #65e6ff; font-size: 15px; font-weight: 700; }
            QFrame#panel { background: #0d1c2e; border: 1px solid #233b54; border-radius: 10px; }
            QComboBox { color: #eaf6ff; background: #10263b; border: 1px solid #2a4a67;
                        border-radius: 6px; padding: 6px 9px; min-height: 24px; }
            QCheckBox { color: #dcecff; spacing: 8px; }
            QSlider::groove:horizontal { height: 6px; background: #20364c; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #52d6ef; border-radius: 3px; }
            QSlider::handle:horizontal { width: 17px; margin: -6px 0; border-radius: 8px;
                                         background: #eefaff; border: 2px solid #52d6ef; }
            QPushButton { color: #e9f7ff; background: #12304a; border: 1px solid #31536e;
                          border-radius: 7px; padding: 7px 12px; }
            QPushButton:hover { background: #19415f; border-color: #58cce2; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(13)

        title = QLabel("손 제스처 감도와 동작")
        title.setObjectName("title")
        root.addWidget(title)
        description = QLabel(
            "한 손은 회전·상하좌우 이동, 두 손은 확대·축소에 사용됩니다. "
            "민감도와 명령 제스처는 카메라를 재시작하지 않고 바로 반영됩니다."
        )
        description.setObjectName("description")
        description.setWordWrap(True)
        root.addWidget(description)

        state = "실행 중" if self._status.get("running") else "중지됨"
        backend = str(self._status.get("backend") or "감지기 미연결")
        error = str(self._status.get("error") or "")
        status_label = QLabel(f"카메라: {state}  ·  {backend}" + (f"\n{error}" if error else ""))
        status_label.setObjectName("description")
        status_label.setWordWrap(True)
        root.addWidget(status_label)

        sensitivity_panel = QFrame()
        sensitivity_panel.setObjectName("panel")
        sensitivity_layout = QVBoxLayout(sensitivity_panel)
        sensitivity_layout.setContentsMargins(14, 12, 14, 13)
        value_row = QHBoxLayout()
        value_row.addWidget(QLabel("스와이프·회전 민감도"))
        value_row.addStretch()
        self.sensitivity_value = QLabel()
        self.sensitivity_value.setObjectName("value")
        value_row.addWidget(self.sensitivity_value)
        sensitivity_layout.addLayout(value_row)
        self.sensitivity_slider = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity_slider.setRange(0, 100)
        self.sensitivity_slider.setSingleStep(1)
        self.sensitivity_slider.setPageStep(5)
        self.sensitivity_slider.setValue(self._original["sensitivity"])
        sensitivity_layout.addWidget(self.sensitivity_slider)
        low_high = QHBoxLayout()
        low_high.addWidget(QLabel("안정적"))
        low_high.addStretch()
        low_high.addWidget(QLabel("민감"))
        sensitivity_layout.addLayout(low_high)
        root.addWidget(sensitivity_panel)

        command_panel = QFrame()
        command_panel.setObjectName("panel")
        command_layout = QVBoxLayout(command_panel)
        command_layout.setContentsMargins(14, 12, 14, 13)
        self.command_enabled = QCheckBox("고정 손 모양으로 안전한 UI 명령 실행")
        self.command_enabled.setChecked(self._original["command_gestures_enabled"])
        command_layout.addWidget(self.command_enabled)
        mapping_form = QFormLayout()
        mapping_form.setHorizontalSpacing(18)
        mapping_form.setVerticalSpacing(8)
        for pose, pose_label in GESTURE_POSE_LABELS.items():
            box = QComboBox()
            for action, action_label in GESTURE_ACTION_LABELS.items():
                box.addItem(action_label, action)
            selected = self._original["gesture_mapping"].get(pose, "")
            index = box.findData(selected)
            box.setCurrentIndex(max(0, index))
            self._mapping_boxes[pose] = box
            mapping_form.addRow(pose_label, box)
        command_layout.addLayout(mapping_form)
        root.addWidget(command_panel)

        controls = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        reset_button = QPushButton("기본값")
        controls.addButton(reset_button, QDialogButtonBox.ButtonRole.ResetRole)
        reset_button.clicked.connect(self._reset_defaults)
        controls.accepted.connect(self._save)
        controls.rejected.connect(self.reject)
        root.addWidget(controls)

        self.sensitivity_slider.valueChanged.connect(self._preview)
        self.command_enabled.toggled.connect(self._mapping_enabled_changed)
        for box in self._mapping_boxes.values():
            box.currentIndexChanged.connect(self._preview)
        self._mapping_enabled_changed(self.command_enabled.isChecked(), emit=False)
        self._update_sensitivity_label()

    def configuration(self) -> dict:
        return {
            "sensitivity": normalize_gesture_sensitivity(self.sensitivity_slider.value()),
            "command_gestures_enabled": self.command_enabled.isChecked(),
            "gesture_mapping": normalize_gesture_mapping({
                pose: str(box.currentData() or "")
                for pose, box in self._mapping_boxes.items()
            }),
        }

    def _update_sensitivity_label(self) -> None:
        value = self.sensitivity_slider.value()
        qualifier = "민감" if value >= 70 else "안정적" if value <= 35 else "균형"
        self.sensitivity_value.setText(f"{value}% · {qualifier}")

    def _mapping_enabled_changed(self, enabled: bool, *, emit: bool = True) -> None:
        for box in self._mapping_boxes.values():
            box.setEnabled(bool(enabled))
        if emit:
            self._preview()

    def _preview(self, *_args) -> None:
        self._update_sensitivity_label()
        self.preview_changed.emit(self.configuration())

    def _reset_defaults(self) -> None:
        self.sensitivity_slider.setValue(60)
        self.command_enabled.setChecked(False)
        for pose, box in self._mapping_boxes.items():
            index = box.findData(DEFAULT_GESTURE_MAPPING[pose])
            box.setCurrentIndex(max(0, index))
        self._preview()

    def _save(self) -> None:
        configuration = self.configuration()
        self.settings_saved.emit(configuration)
        self._original = configuration
        super().accept()

    def reject(self) -> None:
        self.preview_changed.emit(dict(self._original))
        super().reject()
