import sys
import math
import random
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel, 
                             QFrame, QHBoxLayout, QLineEdit, QPushButton, 
                             QFileDialog, QDialog, QMessageBox, QScrollArea,
                             QCheckBox, QListWidget, QListWidgetItem)
from PyQt6.QtWidgets import QTextEdit, QInputDialog
from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QTimer, QRect
from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QFont, QPen, QRadialGradient, QBrush
from .visualizer import AudioVisualizer
from core.state_machine import State

class DragTab(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.drag_position = None
        self.setStyleSheet("""
            QFrame {
                background: transparent;
            }
        """)
    
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self.drag_position:
            self.window().move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

class MiniControlBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)  # 높이 조금 키움
        self.drag_position = None  # 드래그 위치 저장
        self.init_ui()
    
    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 0, 3, 0)  # 여백 조금 키움
        layout.setSpacing(2)
        
        button_style = """
            QPushButton {
                background-color: transparent;
                color: #888888;
                border: none;
                font-size: 11px;  # 글씨 크기 조금 키움
                font-weight: bold;
                padding: 1px;
            }
            QPushButton:hover {
                background-color: rgba(136, 136, 136, 30);
                color: #ffffff;
            }
            QPushButton:pressed {
                background-color: rgba(136, 136, 136, 60);
            }
        """
        
        self.minimize_btn = QPushButton("─")
        self.minimize_btn.setStyleSheet(button_style)
        self.minimize_btn.setFixedSize(17, 17)  # 버튼 크기 조금 키움
        
        self.size_btn = QPushButton("□")
        self.size_btn.setStyleSheet(button_style)
        self.size_btn.setFixedSize(17, 17)
        
        self.close_btn = QPushButton("✕")
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #888888;
                border: none;
                font-size: 11px;
                font-weight: bold;
                padding: 1px;
            }
            QPushButton:hover {
                background-color: rgba(255, 68, 68, 30);
                color: #ff4444;
            }
            QPushButton:pressed {
                background-color: rgba(255, 68, 68, 60);
            }
        """)
        self.close_btn.setFixedSize(17, 17)
        
        layout.addStretch()
        layout.addWidget(self.minimize_btn)
        layout.addWidget(self.size_btn)
        layout.addWidget(self.close_btn)
    
    def mousePressEvent(self, event):
        # 미니 모드에서도 드래그로 창 이동 가능하게
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self.drag_position:
            self.window().move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

class SoundBarWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(145, 25)
        self.bar_count = 12
        self.bar_heights = [0] * self.bar_count
        self.is_speaking = False
        self.is_active = False
        self.freq_bands = [0.0, 0.0, 0.0]  # 저음, 중음, 고음
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_bars)
        self.timer.start(50)
        
    def set_speaking(self, speaking):
        self.is_speaking = speaking
        self.is_active = speaking
        
    def set_audio_data(self, amplitude: float, freq_bands: list[float]):
        """실제 오디오 데이터로 사운드바 업데이트"""
        self.is_active = True
        self.freq_bands = freq_bands
        
        bar_per_band = self.bar_count // len(freq_bands)
        for i in range(self.bar_count):
            band_idx = min(i // bar_per_band, len(freq_bands) - 1)
            band_energy = freq_bands[band_idx]
            height = (amplitude / 100) * 25 * (band_energy / 100 * 2)
            offset = random.randint(-1, 1)
            self.bar_heights[i] = max(0, min(28, int(height + offset)))
        self.update()
        
    def set_audio_level(self, level):
        """호환성 유지용 메서드: 단일 레벨로 사운드바 업데이트"""
        freq_bands = [level, level, level]
        self.set_audio_data(level, freq_bands)
        
    def reset(self):
        self.is_active = False
        self.bar_heights = [0] * self.bar_count
        self.update()
        
    def update_bars(self):
        if not self.is_active:
            return
            
        if self.is_speaking:
            for i in range(self.bar_count):
                band_idx = min(i // 4, len(self.freq_bands) - 1)
                band_energy = self.freq_bands[band_idx]
                height = (band_energy / 100) * 25
                offset = random.randint(-2, 2)
                self.bar_heights[i] = max(0, min(28, int(height + offset)))
        else:
            for i in range(self.bar_count):
                band_idx = min(i // 4, len(self.freq_bands) - 1)
                band_energy = self.freq_bands[band_idx]
                height = (band_energy / 100) * 20
                offset = random.randint(-1, 1)
                self.bar_heights[i] = max(0, min(20, int(height + offset)))
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        bar_width = (self.width() - 20) // self.bar_count
        gap = 2
        
        for i in range(self.bar_count):
            height = self.bar_heights[i]
            x = 10 + i * (bar_width + gap)
            y = self.height() - height - 5
            
            gradient = QLinearGradient(x, y, x, y + height)
            if self.is_speaking:
                gradient.setColorAt(0.0, QColor(153, 69, 255))
                gradient.setColorAt(1.0, QColor(80, 20, 120))
            else:
                gradient.setColorAt(0.0, QColor(0, 212, 255))
                gradient.setColorAt(1.0, QColor(0, 100, 150))
            
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            if height > 0:
                painter.drawRoundedRect(x, y, bar_width, height, 2, 2)


class CircularSoundBarWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 배경 투명으로 설정
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.bar_count = 80
        self.bar_heights = [0] * self.bar_count
        self.is_speaking = False
        self.is_active = False
        self.freq_bands = [0.0, 0.0, 0.0]
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_bars)
        self.timer.start(50)
        
    def set_speaking(self, speaking):
        self.is_speaking = speaking
        self.is_active = speaking
        
    def set_audio_data(self, amplitude: float, freq_bands: list[float]):
        """실제 오디오 데이터로 원형 사운드바 업데이트"""
        self.is_active = True
        self.freq_bands = freq_bands
        
        bar_per_band = self.bar_count // len(freq_bands)
        for i in range(self.bar_count):
            band_idx = min(i // bar_per_band, len(freq_bands) - 1)
            band_energy = freq_bands[band_idx]
            height = (amplitude / 100) * 40 * (band_energy / 100 * 2)
            offset = random.randint(-2, 2)
            self.bar_heights[i] = max(0, min(40, int(height + offset)))
        self.update()
        
    def set_audio_level(self, level):
        """호환성 유지용 메서드: 단일 레벨로 원형 사운드바 업데이트"""
        freq_bands = [level, level, level]
        self.set_audio_data(level, freq_bands)
        
    def reset(self):
        self.is_active = False
        self.bar_heights = [0] * self.bar_count
        self.update()
        
    def update_bars(self):
        if not self.is_active:
            return
            
        if self.is_speaking:
            for i in range(self.bar_count):
                band_idx = min(i // (self.bar_count // len(self.freq_bands)), len(self.freq_bands) - 1)
                band_energy = self.freq_bands[band_idx]
                height = (band_energy / 100) * 40
                offset = random.randint(-3, 3)
                self.bar_heights[i] = max(0, min(40, int(height + offset)))
        else:
            for i in range(self.bar_count):
                band_idx = min(i // (self.bar_count // len(self.freq_bands)), len(self.freq_bands) - 1)
                band_energy = self.freq_bands[band_idx]
                height = (band_energy / 100) * 25
                offset = random.randint(-2, 2)
                self.bar_heights[i] = max(0, min(25, int(height + offset)))
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        center_x = self.width() // 2
        center_y = self.height() // 2
        # Arc Reactor의 base_radius (min(width, height) //3) 와 맞춤
        base_radius = min(self.width(), self.height()) // 3
        # 가장 외곽 원의 반지름 (base_radius +40)
        outermost_radius = base_radius + 40
        
        for i in range(self.bar_count):
            angle = (i / self.bar_count) * 2 * math.pi
            bar_width = 3
            
            # 가장 외곽 원에서 안쪽으로 사운드바 그리기
            outer_radius = outermost_radius
            outer_x = center_x + outer_radius * math.cos(angle)
            outer_y = center_y + outer_radius * math.sin(angle)
            
            height = self.bar_heights[i]
            inner_radius = outer_radius - height
            inner_x = center_x + inner_radius * math.cos(angle)
            inner_y = center_y + inner_radius * math.sin(angle)
            
            gradient = QLinearGradient(inner_x, inner_y, outer_x, outer_y)
            if self.is_speaking:
                gradient.setColorAt(0.0, QColor(153, 69, 255))
                gradient.setColorAt(1.0, QColor(80, 20, 120))
            else:
                gradient.setColorAt(0.0, QColor(0, 212, 255))
                gradient.setColorAt(1.0, QColor(0, 100, 150))
            
            painter.setPen(QPen(QBrush(gradient), bar_width))
            if height > 0:
                painter.drawLine(int(inner_x), int(inner_y), int(outer_x), int(outer_y))

class PermissionRequestDialog(QDialog):
    def __init__(self, permission_name: str, permission_description: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("JARVIS 권한 요청")
        self.setFixedSize(450, 200)
        self.result_value = False
        
        # UI 스타일
        self.setStyleSheet("""
            QDialog {
                background-color: #0a0a1a;
                border: 2px solid #00d4ff;
            }
            QLabel {
                color: #00d4ff;
                font-family: Consolas;
            }
            QPushButton {
                background-color: rgba(0, 212, 255, 20);
                color: #00d4ff;
                border: 2px solid #00d4ff;
                border-radius: 8px;
                padding: 8px 20px;
                font-family: Consolas;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(0, 212, 255, 40);
            }
            QPushButton:pressed {
                background-color: rgba(0, 212, 255, 60);
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)
        
        # 권한 이름 라벨
        title_label = QLabel(f"⚠️  권한 요청: {permission_name}")
        title_font = QFont("Orbitron", 14, QFont.Weight.Bold)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        
        # 권한 설명 라벨
        desc_label = QLabel(f"{permission_description}\n선택한 결과는 권한 설정에 영구 저장됩니다.")
        desc_label.setWordWrap(True)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc_label)
        
        # 버튼 레이아웃
        button_layout = QHBoxLayout()
        button_layout.setSpacing(20)
        
        deny_btn = QPushButton("거부")
        deny_btn.clicked.connect(self._on_deny)
        button_layout.addWidget(deny_btn)
        
        allow_btn = QPushButton("허용")
        allow_btn.clicked.connect(self._on_allow)
        button_layout.addWidget(allow_btn)
        
        layout.addLayout(button_layout)
    
    def _on_allow(self):
        self.result_value = True
        self.accept()
    
    def _on_deny(self):
        self.result_value = False
        self.reject()


class PermissionSettingsDialog(QDialog):
    """저장된 권한을 한 화면에서 확인하고 영구 허용/차단하는 대화상자."""

    def __init__(self, permission_manager, parent=None):
        super().__init__(parent)
        self.permission_manager = permission_manager
        self.setWindowTitle("JARVIS 권한 관리")
        self.resize(560, 520)
        self.setStyleSheet("""
            QDialog, QScrollArea, QWidget { background-color: #0a0a1a; }
            QLabel { color: #c7f7ff; }
            QCheckBox { color: #00d4ff; font-weight: bold; spacing: 10px; }
            QPushButton { color: #00d4ff; border: 1px solid #00d4ff;
                          border-radius: 6px; padding: 7px 16px; }
        """)
        layout = QVBoxLayout(self)
        intro = QLabel("허용된 권한은 다시 묻지 않고 실행하며, 차단된 권한은 요청창 없이 거부합니다.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        rows = QVBoxLayout(content)
        for permission in self.permission_manager.get_all_permissions():
            row = QFrame()
            row_layout = QHBoxLayout(row)
            labels = QVBoxLayout()
            name = QLabel(f"{permission.name}  ({permission.id})")
            name.setStyleSheet("font-weight: bold; color: #ffffff;")
            description = QLabel(permission.description)
            description.setStyleSheet("color: #8db8c0;")
            labels.addWidget(name)
            labels.addWidget(description)
            row_layout.addLayout(labels, 1)
            toggle = QCheckBox("허용")
            toggle.setChecked(self.permission_manager.check_permission(permission.id))
            if permission.level.value == "safe":
                toggle.setEnabled(False)
                toggle.setToolTip("안전 권한은 항상 허용됩니다.")
            else:
                toggle.toggled.connect(
                    lambda checked, permission_id=permission.id:
                    self._set_permission(permission_id, checked)
                )
            row_layout.addWidget(toggle)
            rows.addWidget(row)
        rows.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)

    def _set_permission(self, permission_id: str, allowed: bool):
        if allowed:
            self.permission_manager.grant_permission(permission_id)
        else:
            self.permission_manager.revoke_permission(permission_id)


class TTSVoiceDialog(QDialog):
    """Select a voice and configure its user address."""

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.setWindowTitle("JARVIS TTS 목소리")
        self.resize(520, 400)
        self.setStyleSheet("""
            QDialog, QListWidget { background-color: #0a0a1a; color: #c7f7ff; }
            QLabel { color: #00d4ff; }
            QListWidget { border: 1px solid #00d4ff; border-radius: 6px; }
            QListWidget::item { padding: 10px; }
            QListWidget::item:selected { background-color: #16495a; color: #ffffff; }
            QPushButton { color: #00d4ff; border: 1px solid #00d4ff;
                          border-radius: 6px; padding: 7px 16px; }
        """)
        layout = QVBoxLayout(self)
        guide = QLabel("사용할 목소리를 클릭하고, 음성별 사용자 호칭을 설정하세요.")
        layout.addWidget(guide)
        self.voice_list = QListWidget()
        voices = self.settings_manager.list_voices(refresh=True)
        for index, voice in enumerate(voices):
            language = f" · {voice.languages}" if voice.languages else ""
            provider = {
                "edge": "온라인",
                "gpt-sovits": "커스텀",
            }.get(voice.provider, "Windows")
            item = QListWidgetItem(f"[{provider}] {voice.name}{language}")
            item.setData(Qt.ItemDataRole.UserRole, (voice.id, voice.name))
            self.voice_list.addItem(item)
            if voice.id == self.settings_manager.selected_voice_id:
                self.voice_list.setCurrentRow(index)
        self.voice_list.itemClicked.connect(self._select_voice)
        layout.addWidget(self.voice_list)
        self.status_label = QLabel(
            f"현재 목소리: {self.settings_manager.selected_voice_name or '한국어 기본 음성'}"
        )
        layout.addWidget(self.status_label)
        address_row = QHBoxLayout()
        address_row.addWidget(QLabel("선택 음성의 호칭"))
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("예: 보스, 지휘관님, 주인님")
        self.address_input.setMaxLength(30)
        address_row.addWidget(self.address_input, 1)
        self.save_address_button = QPushButton("호칭 저장")
        self.save_address_button.clicked.connect(self._save_address)
        address_row.addWidget(self.save_address_button)
        layout.addLayout(address_row)
        self.address_status = QLabel("")
        layout.addWidget(self.address_status)
        self._load_selected_address()
        if not voices:
            self.status_label.setText("사용 가능한 Windows TTS 음성을 찾지 못했습니다.")
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)

    def _select_voice(self, item):
        voice_id, voice_name = item.data(Qt.ItemDataRole.UserRole)
        if self.settings_manager.select_voice(voice_id, voice_name):
            self.status_label.setText(f"현재 목소리: {voice_name}")
            self._load_selected_address()
        else:
            self.status_label.setText("목소리 설정에 실패했습니다.")

    def _selected_voice_id(self):
        item = self.voice_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole)[0] if item else ""

    def _load_selected_address(self):
        voice_id = self._selected_voice_id()
        self.address_input.setText(
            self.settings_manager.get_voice_address(voice_id) if voice_id else ""
        )
        self.address_status.setText("")

    def _save_address(self):
        voice_id = self._selected_voice_id()
        if not voice_id:
            self.address_status.setText("먼저 목소리를 선택하세요.")
            return
        if self.settings_manager.set_voice_address(voice_id, self.address_input.text()):
            self.address_input.setText(self.settings_manager.get_voice_address(voice_id))
            self.address_status.setText("이 음성의 호칭을 저장했습니다.")
        else:
            self.address_status.setText("빈 호칭은 저장할 수 없습니다.")


class SessionManagerDialog(QDialog):
    session_selected = pyqtSignal(str)
    session_created = pyqtSignal(str)
    session_deleted = pyqtSignal(str)
    session_reset = pyqtSignal(str)

    def __init__(self, memory_manager, current_session_id: str, parent=None):
        super().__init__(parent)
        self.memory_manager = memory_manager
        self.current_session_id = current_session_id
        self.setWindowTitle("JARVIS 대화 세션")
        self.resize(760, 560)
        self.setStyleSheet("""
            QDialog, QListWidget, QTextEdit { background-color: #0a0a1a; color: #d8faff; }
            QLabel { color: #00d4ff; }
            QListWidget, QTextEdit { border: 1px solid #26677a; border-radius: 6px; }
            QListWidget::item { padding: 9px; }
            QListWidget::item:selected { background-color: #16495a; }
            QPushButton { color: #00d4ff; border: 1px solid #00d4ff;
                          border-radius: 6px; padding: 7px 12px; }
        """)
        layout = QVBoxLayout(self)
        content = QHBoxLayout()
        self.session_list = QListWidget()
        self.session_list.setMinimumWidth(275)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        content.addWidget(self.session_list, 1)
        content.addWidget(self.history, 2)
        layout.addLayout(content)

        buttons = QHBoxLayout()
        new_button = QPushButton("새 세션")
        select_button = QPushButton("선택")
        reset_button = QPushButton("대화 리셋")
        delete_button = QPushButton("삭제")
        close_button = QPushButton("닫기")
        new_button.clicked.connect(self._create_session)
        select_button.clicked.connect(self._select_session)
        reset_button.clicked.connect(self._reset_session)
        delete_button.clicked.connect(self._delete_session)
        close_button.clicked.connect(self.accept)
        for button in (new_button, select_button, reset_button, delete_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.session_list.currentItemChanged.connect(self._show_history)
        self.session_list.itemDoubleClicked.connect(lambda _item: self._select_session())
        self._load_sessions()

    def _load_sessions(self):
        self.session_list.clear()
        current_row = 0
        for index, session in enumerate(self.memory_manager.list_session_details()):
            marker = "현재 · " if session["session_id"] == self.current_session_id else ""
            item = QListWidgetItem(
                f"{marker}{session['title']}\n{session['message_count']}개 메시지 · {session['end_time'][:16]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, session["session_id"])
            self.session_list.addItem(item)
            if session["session_id"] == self.current_session_id:
                current_row = index
        if self.session_list.count():
            self.session_list.setCurrentRow(current_row)

    def _selected_id(self):
        item = self.session_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _show_history(self, current, _previous=None):
        if current is None:
            self.history.clear()
            return
        session_id = current.data(Qt.ItemDataRole.UserRole)
        messages = self.memory_manager.load_session(session_id)
        lines = []
        for message in messages:
            speaker = "나" if message["role"] == "user" else "자비스"
            lines.append(f"{speaker}\n{message['content']}")
        self.history.setPlainText("\n\n".join(lines) or "아직 대화 내용이 없습니다.")

    def _create_session(self):
        title, accepted = QInputDialog.getText(self, "새 세션", "세션 이름:", text="새 대화")
        if accepted and title.strip():
            self.session_created.emit(title.strip())
            self.accept()

    def _select_session(self):
        session_id = self._selected_id()
        if session_id:
            self.session_selected.emit(session_id)
            self.accept()

    def _reset_session(self):
        session_id = self._selected_id()
        if session_id and QMessageBox.question(
            self, "대화 리셋", "이 세션의 대화 내용과 대기 작업을 모두 지울까요?"
        ) == QMessageBox.StandardButton.Yes:
            self.session_reset.emit(session_id)
            self.accept()

    def _delete_session(self):
        session_id = self._selected_id()
        if session_id and QMessageBox.question(
            self, "세션 삭제", "선택한 세션을 영구 삭제할까요?"
        ) == QMessageBox.StandardButton.Yes:
            self.session_deleted.emit(session_id)
            self.accept()

class JarvisMainWindow(QWidget):
    command_triggered = pyqtSignal(str)
    text_submitted = pyqtSignal(str)
    close_requested = pyqtSignal()  # 종료 요청 시그널
    workspace_selected = pyqtSignal(str)  # Workspace 선택 시그널
    # 권한 요청 시그널: (permission_name, permission_description) -> return bool
    permission_requested = pyqtSignal(str, str)
    session_selected = pyqtSignal(str)
    session_created = pyqtSignal(str)
    session_deleted = pyqtSignal(str)
    session_reset = pyqtSignal(str)
    
    def __init__(self, audio_processor=None):
        super().__init__()
        self._allow_close = True
        self.arc_angle = 0
        self.pulse_value = 0
        self.current_state = State.IDLE
        self.window_mode = "normal"
        self.normal_geometry = None
        self.is_speaking = False
        self.audio_processor = audio_processor
        self.current_workspace_path = ""
        self.current_workspace_name = ""
        self.permission_manager = None
        self.tts_settings_manager = None
        self.memory_manager = None
        self.current_session_id = ""
        
        # 원형 사운드바 상태 변수
        self.soundbar_bar_count = 80
        self.soundbar_bar_heights = [0] * self.soundbar_bar_count
        self.soundbar_is_active = False
        self.soundbar_freq_bands = [0.0, 0.0, 0.0]
        
        self.init_ui()
        self.start_animations()
        
        if self.audio_processor:
            self.audio_processor.audio_update.connect(self._on_audio_update)
    
    def init_ui(self):
        # 일반 앱처럼 작업 표시줄에 표시하고 다른 창의 앞뒤로 이동할 수 있게 한다.
        self.setWindowFlags(Qt.WindowType.Window |
                           Qt.WindowType.FramelessWindowHint)
        
        screen = QApplication.primaryScreen().geometry()
        window_width = 800
        window_height = 700
        x = (screen.width() - window_width) // 2
        y = (screen.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)
        self.normal_geometry = self.geometry()
        
        self.main_layout = QVBoxLayout()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        # 일반 모드용 드래그 탭
        self.drag_tab = DragTab(self)
        tab_layout = QHBoxLayout(self.drag_tab)
        tab_layout.setContentsMargins(15, 5, 10, 0)
        
        self.title_label = QLabel("JARVIS")
        title_font = QFont("Orbitron", 16, QFont.Weight.Bold)
        self.title_label.setFont(title_font)
        self.title_label.setStyleSheet("color: #00d4ff; letter-spacing: 6px;")
        tab_layout.addWidget(self.title_label)
        
        # 버튼 스타일 정의 (먼저 정의!)
        button_style = """
            QPushButton {
                background-color: transparent;
                color: #888888;
                border: none;
                font-size: 18px;
                font-weight: bold;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: rgba(136, 136, 136, 30);
                color: #ffffff;
            }
            QPushButton:pressed {
                background-color: rgba(136, 136, 136, 60);
            }
        """
        
        # Workspace 선택 버튼
        self.workspace_btn = QPushButton("📁")
        self.workspace_btn.setStyleSheet(button_style)
        self.workspace_btn.setFixedSize(35, 35)
        self.workspace_btn.setToolTip("작업 폴더 선택")
        self.workspace_btn.clicked.connect(self._select_workspace)
        tab_layout.addWidget(self.workspace_btn)

        self.permission_btn = QPushButton("🔐")
        self.permission_btn.setStyleSheet(button_style)
        self.permission_btn.setFixedSize(35, 35)
        self.permission_btn.setToolTip("권한 관리")
        self.permission_btn.clicked.connect(self.show_permission_settings)
        tab_layout.addWidget(self.permission_btn)

        self.voice_btn = QPushButton("🔊")
        self.voice_btn.setStyleSheet(button_style)
        self.voice_btn.setFixedSize(35, 35)
        self.voice_btn.setToolTip("TTS 목소리 및 호칭 설정")
        self.voice_btn.clicked.connect(self.show_tts_voice_settings)
        tab_layout.addWidget(self.voice_btn)

        self.session_btn = QPushButton("💬")
        self.session_btn.setStyleSheet(button_style)
        self.session_btn.setFixedSize(35, 35)
        self.session_btn.setToolTip("대화 세션 관리")
        self.session_btn.clicked.connect(self.show_session_manager)
        tab_layout.addWidget(self.session_btn)
        
        self.sound_bar = SoundBarWidget(self)
        self.sound_bar.hide()
        tab_layout.addWidget(self.sound_bar)
        
        tab_layout.addStretch()
        
        self.minimize_btn = QPushButton("─")
        self.minimize_btn.setStyleSheet(button_style)
        self.minimize_btn.setFixedSize(35, 35)
        self.minimize_btn.clicked.connect(self.minimize_window)
        tab_layout.addWidget(self.minimize_btn)
        
        self.size_btn = QPushButton("□")
        self.size_btn.setStyleSheet(button_style)
        self.size_btn.setFixedSize(35, 35)
        self.size_btn.clicked.connect(self.toggle_window_mode)
        tab_layout.addWidget(self.size_btn)
        
        self.close_btn = QPushButton("✕")
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #888888;
                border: none;
                font-size: 18px;
                font-weight: bold;
                padding: 5px 10px;
            }
            QPushButton:hover {
                background-color: rgba(255, 68, 68, 30);
                color: #ff4444;
            }
            QPushButton:pressed {
                background-color: rgba(255, 68, 68, 60);
            }
        """)
        self.close_btn.setFixedSize(35, 35)
        self.close_btn.clicked.connect(self.close_requested.emit)  # 종료 시그널 보내기
        tab_layout.addWidget(self.close_btn)
        
        self.main_layout.addWidget(self.drag_tab)
        
        # 미니 모드용 컨트롤 바
        self.mini_control_bar = MiniControlBar(self)
        self.mini_control_bar.hide()
        self.mini_control_bar.minimize_btn.clicked.connect(self.minimize_window)
        self.mini_control_bar.size_btn.clicked.connect(self.toggle_window_mode)
        self.mini_control_bar.close_btn.clicked.connect(self.close_requested.emit)  # 종료 시그널 보내기
        self.main_layout.addWidget(self.mini_control_bar)
        
        # 미니 모드용 사운드바 (다른 위치에 배치)
        self.mini_sound_bar = SoundBarWidget(self)
        self.mini_sound_bar.hide()
        self.main_layout.addWidget(self.mini_sound_bar)
        
        self.center_widget = QWidget()
        center_layout = QVBoxLayout(self.center_widget)
        center_layout.setContentsMargins(30, 20, 30, 20)
        
        self.status_label = QLabel("SYSTEM READY")
        status_font = QFont("Orbitron", 11)
        self.status_label.setFont(status_font)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #00d4ff; letter-spacing: 3px;")
        
        # Workspace 정보 라벨
        self.workspace_label = QLabel("Workspace: 없음")
        workspace_font = QFont("Consolas", 9)
        self.workspace_label.setFont(workspace_font)
        self.workspace_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.workspace_label.setStyleSheet("color: #00a8cc; padding: 5px;")
        
        self.user_text_label = QLabel("")
        user_font = QFont("Consolas", 10)
        self.user_text_label.setFont(user_font)
        self.user_text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.user_text_label.setStyleSheet("color: #00a8cc; padding: 10px;")
        self.user_text_label.setWordWrap(True)
        
        self.assistant_text_label = QLabel("")
        assistant_font = QFont("Consolas", 10)
        self.assistant_text_label.setFont(assistant_font)
        self.assistant_text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.assistant_text_label.setStyleSheet("color: #9945ff; padding: 10px;")
        self.assistant_text_label.setWordWrap(True)
        
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("SPEAK OR TYPE YOUR COMMAND...")
        self.text_input.setStyleSheet("""
            QLineEdit {
                background: rgba(0, 20, 40, 200);
                color: #00d4ff;
                border: 2px solid #00d4ff;
                border-radius: 15px;
                padding: 12px 18px;
                font-size: 13px;
                font-family: Consolas;
            }
            QLineEdit:focus {
                border: 2px solid #00ffcc;
            }
        """)
        self.text_input.returnPressed.connect(self._on_text_submitted)
        
        center_layout.addStretch()
        center_layout.addSpacing(20)
        center_layout.addWidget(self.status_label)
        center_layout.addWidget(self.workspace_label)
        center_layout.addSpacing(20)
        center_layout.addWidget(self.user_text_label)
        center_layout.addWidget(self.assistant_text_label)
        center_layout.addStretch()
        center_layout.addWidget(self.text_input)
        
        self.main_layout.addWidget(self.center_widget, 1)
        
        self.setLayout(self.main_layout)
        self.show()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        if self.window_mode == "mini":
            gradient = QRadialGradient(self.width()/2, self.height()/2, self.width()/2)
            gradient.setColorAt(0.0, QColor(20, 20, 30))
            gradient.setColorAt(1.0, QColor(5, 5, 10))
            painter.fillRect(self.rect(), gradient)
            return
        
        if self.current_state in [State.PROCESSING, State.EXECUTING, State.RESPONDING]:
            gradient = QRadialGradient(self.width()/2, self.height()/2, self.width()/2)
            gradient.setColorAt(0.0, QColor(40, 0, 60))
            gradient.setColorAt(0.5, QColor(20, 0, 40))
            gradient.setColorAt(1.0, QColor(5, 0, 10))
            painter.fillRect(self.rect(), gradient)
            
            center_x = self.width() // 2
            center_y = self.height() // 2
            base_radius = min(self.width(), self.height()) // 3
            
            pen = QPen(QColor(153, 69, 255, 100), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 40, base_radius + 40)
            
            pen = QPen(QColor(153, 69, 255, 150), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 25, base_radius + 25)
            
            pen = QPen(QColor(190, 120, 255), 3)
            painter.setPen(pen)
            rect = [center_x - base_radius, center_y - base_radius, 
                    base_radius * 2, base_radius * 2]
            
            start_angle = self.arc_angle * 16
            span_angle = 90 * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle), int(span_angle))
            
            start_angle2 = (self.arc_angle + 180) * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle2), int(span_angle))
            
            core_radius = 25 + self.pulse_value * 8
            core_gradient = QRadialGradient(center_x, center_y, core_radius)
            core_gradient.setColorAt(0.0, QColor(190, 120, 255, 200))
            core_gradient.setColorAt(0.5, QColor(153, 69, 255, 100))
            core_gradient.setColorAt(1.0, QColor(153, 69, 255, 0))
            painter.setBrush(core_gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPoint(center_x, center_y), int(core_radius), int(core_radius))
            
            scan_y = center_y + math.sin(self.arc_angle * math.pi / 180) * 80
            pen = QPen(QColor(153, 69, 255, 50), 1)
            painter.setPen(pen)
            painter.drawLine(0, int(scan_y), self.width(), int(scan_y))
            
            # 원형 사운드바 그리기 (가장 외곽 원에서 안쪽으로) - 보라색 버전
            if self.soundbar_is_active:
                bar_width = 3
                for i in range(self.soundbar_bar_count):
                    angle = (i / self.soundbar_bar_count) * 2 * math.pi
                    outermost_radius = base_radius + 40
                    outer_x = center_x + outermost_radius * math.cos(angle)
                    outer_y = center_y + outermost_radius * math.sin(angle)
                    
                    height = self.soundbar_bar_heights[i]
                    inner_radius = outermost_radius - height
                    inner_x = center_x + inner_radius * math.cos(angle)
                    inner_y = center_y + inner_radius * math.sin(angle)
                    
                    gradient = QLinearGradient(inner_x, inner_y, outer_x, outer_y)
                    if self.is_speaking:
                        gradient.setColorAt(0.0, QColor(153, 69, 255))
                        gradient.setColorAt(1.0, QColor(80, 20, 120))
                    else:
                        gradient.setColorAt(0.0, QColor(0, 212, 255))
                        gradient.setColorAt(1.0, QColor(0, 100, 150))
                    
                    painter.setPen(QPen(QBrush(gradient), bar_width))
                    if height > 0:
                        painter.drawLine(int(inner_x), int(inner_y), int(outer_x), int(outer_y))
        else:
            gradient = QRadialGradient(self.width()/2, self.height()/2, self.width()/2)
            gradient.setColorAt(0.0, QColor(0, 30, 60))
            gradient.setColorAt(0.5, QColor(0, 15, 30))
            gradient.setColorAt(1.0, QColor(0, 5, 10))
            painter.fillRect(self.rect(), gradient)
            
            center_x = self.width() // 2
            center_y = self.height() // 2
            base_radius = min(self.width(), self.height()) // 3
            
            pen = QPen(QColor(0, 212, 255, 100), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 40, base_radius + 40)
            
            pen = QPen(QColor(0, 212, 255, 150), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 25, base_radius + 25)
            
            pen = QPen(QColor(0, 255, 204), 3)
            painter.setPen(pen)
            rect = [center_x - base_radius, center_y - base_radius, 
                    base_radius * 2, base_radius * 2]
            
            start_angle = self.arc_angle * 16
            span_angle = 90 * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle), int(span_angle))
            
            start_angle2 = (self.arc_angle + 180) * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle2), int(span_angle))
            
            core_radius = 25 + self.pulse_value * 8
            core_gradient = QRadialGradient(center_x, center_y, core_radius)
            core_gradient.setColorAt(0.0, QColor(0, 255, 204, 200))
            core_gradient.setColorAt(0.5, QColor(0, 212, 255, 100))
            core_gradient.setColorAt(1.0, QColor(0, 212, 255, 0))
            painter.setBrush(core_gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPoint(center_x, center_y), int(core_radius), int(core_radius))
            
            scan_y = center_y + math.sin(self.arc_angle * math.pi / 180) * 80
            pen = QPen(QColor(0, 212, 255, 50), 1)
            painter.setPen(pen)
            painter.drawLine(0, int(scan_y), self.width(), int(scan_y))
            
            # 원형 사운드바 그리기 (가장 외곽 원에서 안쪽으로)
            if self.soundbar_is_active:
                bar_width = 3
                for i in range(self.soundbar_bar_count):
                    angle = (i / self.soundbar_bar_count) * 2 * math.pi
                    outermost_radius = base_radius + 40
                    outer_x = center_x + outermost_radius * math.cos(angle)
                    outer_y = center_y + outermost_radius * math.sin(angle)
                    
                    height = self.soundbar_bar_heights[i]
                    inner_radius = outermost_radius - height
                    inner_x = center_x + inner_radius * math.cos(angle)
                    inner_y = center_y + inner_radius * math.sin(angle)
                    
                    gradient = QLinearGradient(inner_x, inner_y, outer_x, outer_y)
                    if self.is_speaking:
                        gradient.setColorAt(0.0, QColor(153, 69, 255))
                        gradient.setColorAt(1.0, QColor(80, 20, 120))
                    else:
                        gradient.setColorAt(0.0, QColor(0, 212, 255))
                        gradient.setColorAt(1.0, QColor(0, 100, 150))
                    
                    painter.setPen(QPen(QBrush(gradient), bar_width))
                    if height > 0:
                        painter.drawLine(int(inner_x), int(inner_y), int(outer_x), int(outer_y))
    
    def start_animations(self):
        self.arc_timer = QTimer()
        self.arc_timer.timeout.connect(self.update_arc)
        self.arc_timer.start(20)
        
        self.pulse_timer = QTimer()
        self.pulse_timer.timeout.connect(self.update_pulse)
        self.pulse_timer.start(100)
        
        # 원형 사운드바 애니메이션 타이머
        self.soundbar_timer = QTimer()
        self.soundbar_timer.timeout.connect(self._animate_soundbar)
        self.soundbar_timer.start(50)
    
    def _animate_soundbar(self):
        """원형 사운드바 애니메이션 업데이트"""
        if not self.soundbar_is_active or not self.soundbar_freq_bands:
            return
        
        for i in range(self.soundbar_bar_count):
            band_idx = min(i // 20, len(self.soundbar_freq_bands) - 1)
            band_energy = self.soundbar_freq_bands[band_idx]
            height = (band_energy / 100) * 45 if self.is_speaking else (band_energy / 100) * 30
            offset = random.randint(-2, 2)
            self.soundbar_bar_heights[i] = max(0, min(45, int(height + offset)))
        
        self.update()
    
    def update_arc(self):
        self.arc_angle = (self.arc_angle + 2) % 360
        if self.window_mode != "mini":
            self.update()
    
    def update_pulse(self):
        self.pulse_value = (self.pulse_value + 0.1) % (2 * math.pi)
        if self.window_mode != "mini":
            self.update()
    
    def update_state(self, state: State):
        self.current_state = state
        if self.window_mode != "mini":
            state_texts = {
                State.IDLE: "SYSTEM READY",
                State.LISTENING: "LISTENING...",
                State.PROCESSING: "PROCESSING...",
                State.EXECUTING: "EXECUTING...",
                State.RESPONDING: "RESPONDING...",
                State.ERROR: "SYSTEM ERROR"
            }
            self.status_label.setText(state_texts.get(state, "SYSTEM READY"))
            if state in [State.PROCESSING, State.EXECUTING, State.RESPONDING]:
                self.status_label.setStyleSheet("color: #9945ff; letter-spacing: 3px;")
            else:
                self.status_label.setStyleSheet("color: #00d4ff; letter-spacing: 3px;")
            
            # LISTENING/RESPONDING 상태일 때 사운드바 활성화, IDLE일 때 리셋
            if state in [State.LISTENING, State.RESPONDING]:
                self.is_speaking = (state == State.RESPONDING)
                self.soundbar_is_active = True
                self.soundbar_freq_bands = [60, 60, 60]
                self._update_soundbar_bars(60, self.soundbar_freq_bands)
            else:
                self.soundbar_is_active = False
                self.soundbar_bar_heights = [0] * self.soundbar_bar_count
            
            self.update()
    
    def _update_soundbar_bars(self, amplitude: float, freq_bands: list[float]):
        """사운드바 바 높이 업데이트"""
        if not freq_bands:
            self.soundbar_is_active = False
            self.soundbar_bar_heights = [0] * self.soundbar_bar_count
            self.update()
            return
        bar_per_band = self.soundbar_bar_count // len(freq_bands)
        for i in range(self.soundbar_bar_count):
            band_idx = min(i // bar_per_band, len(freq_bands) - 1)
            band_energy = freq_bands[band_idx]
            height = (amplitude / 100) * 45 * (band_energy / 100 * 2)
            offset = random.randint(-2, 2)
            self.soundbar_bar_heights[i] = max(0, min(45, int(height + offset)))
    
    def _on_audio_update(self, amplitude: float, freq_bands: list[float], is_speaking: bool):
        """실제 오디오 데이터로 모든 사운드바 업데이트"""
        if not freq_bands:
            self.reset_soundbar()
            return
        self.is_speaking = is_speaking
        self.sound_bar.set_speaking(is_speaking)
        self.mini_sound_bar.set_speaking(is_speaking)
        self.soundbar_is_active = True
        self.soundbar_freq_bands = freq_bands
        self._update_soundbar_bars(amplitude, freq_bands)
        
        self.sound_bar.set_audio_data(amplitude, freq_bands)
        self.mini_sound_bar.set_audio_data(amplitude, freq_bands)
    
    def set_soundbar_speaking(self, speaking):
        self.is_speaking = speaking
        self.sound_bar.set_speaking(speaking)
        self.mini_sound_bar.set_speaking(speaking)
        # The circular soundbar is painted by this window, not a child widget.
        self.update()
    
    def reset_soundbar(self):
        self.sound_bar.reset()
        self.mini_sound_bar.reset()
        self.soundbar_is_active = False
        self.soundbar_bar_heights = [0] * self.soundbar_bar_count
        self.update()
    
    def set_soundbar_audio_level(self, level):
        # 기존 메서드 유지 (호환성 위해)
        freq_bands = [level, level, level]
        self.sound_bar.set_audio_data(level, freq_bands)
        self.mini_sound_bar.set_audio_data(level, freq_bands)
        self.soundbar_is_active = True
        self.soundbar_freq_bands = freq_bands
        self._update_soundbar_bars(level, freq_bands)
        self.update()
    
    def toggle_window_mode(self):
        if self.window_mode == "normal":
            self.window_mode = "mini"
            self.size_btn.setText("□")
            self.normal_geometry = self.geometry()
            screen = QApplication.primaryScreen().geometry()
            mini_width = 160  # 가로 160으로 고정
            mini_height = 50  # 세로 50으로 고정
            x = screen.width() - mini_width - 20
            y = 20
            
            # 1. 먼저 미니 모드 위젯 표시/숨김 처리
            self.main_layout.setContentsMargins(0, 0, 0, 0)
            self.main_layout.setSpacing(0)
            self.center_widget.hide()
            self.drag_tab.hide()
            self.mini_control_bar.show()
            self.mini_sound_bar.show()
            
            # 2. 크기를 먼저 강제로 고정 (가장 먼저!)
            self.setMinimumSize(mini_width, mini_height)
            self.setMaximumSize(mini_width, mini_height)
            self.setFixedSize(mini_width, mini_height)
            
            # 3. 레이아웃 강제 재계산
            self.adjustSize()
            self.updateGeometry()
            
            # 4. 마지막으로 위치만 설정
            self.setGeometry(x, y, mini_width, mini_height)
            
            # 현재 상태에 따라 미니 사운드바 상태 유지
            if self.current_state in [State.LISTENING, State.RESPONDING]:
                if self.current_state == State.RESPONDING:
                    self.mini_sound_bar.set_speaking(True)
                else:
                    self.mini_sound_bar.set_speaking(False)
                freq_bands = [60, 60, 60]
                self.mini_sound_bar.set_audio_data(60, freq_bands)
            else:
                self.mini_sound_bar.reset()
                
        elif self.window_mode == "mini":
            self.window_mode = "maximized"
            self.size_btn.setText("□")
            # 크기 제한을 풀고
            self.setMinimumSize(100, 100)
            self.setMaximumSize(16777215, 16777215)
            self.mini_control_bar.hide()
            self.mini_sound_bar.hide()
            self.showMaximized()
            self.drag_tab.show()
            self.center_widget.show()

            # 원형 사운드바 상태 유지
            if self.current_state in [State.LISTENING, State.RESPONDING]:
                if self.current_state == State.RESPONDING:
                    self.is_speaking = True
                else:
                    self.is_speaking = False
                freq_bands = [60, 60, 60]
                self.soundbar_is_active = True
                self.soundbar_freq_bands = freq_bands
                self._update_soundbar_bars(60, freq_bands)
            else:
                self.soundbar_is_active = False
                self.soundbar_bar_heights = [0] * self.soundbar_bar_count
                
        else:
            self.window_mode = "normal"
            self.size_btn.setText("□")
            # 크기 제한을 풀고
            self.setMinimumSize(100, 100)
            self.setMaximumSize(16777215, 16777215)
            self.showNormal()
            self.setGeometry(self.normal_geometry)
            self.mini_control_bar.hide()
            self.mini_sound_bar.hide()
            self.drag_tab.show()
            self.center_widget.show()
            
            # 원형 사운드바 상태 유지
            if self.current_state in [State.LISTENING, State.RESPONDING]:
                if self.current_state == State.RESPONDING:
                    self.is_speaking = True
                else:
                    self.is_speaking = False
                freq_bands = [60, 60, 60]
                self.soundbar_is_active = True
                self.soundbar_freq_bands = freq_bands
                self._update_soundbar_bars(60, freq_bands)
            else:
                self.soundbar_is_active = False
                self.soundbar_bar_heights = [0] * self.soundbar_bar_count
                
        self.update()

    def minimize_window(self):
        """Windows 작업 표시줄로 창을 최소화한다."""
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMinimized)
    
    def show_user_text(self, text: str):
        self.user_text_label.setText(f"> {text}")
    
    def show_assistant_text(self, text: str):
        self.assistant_text_label.setText(text)
    
    def set_soundbar_speaking(self, speaking):
        self.is_speaking = speaking
        self.sound_bar.set_speaking(speaking)
        self.mini_sound_bar.set_speaking(speaking)
        self.update()
        
    def reset_soundbar(self):
        # 사운드바 리셋
        self.sound_bar.reset()
        self.mini_sound_bar.reset()
        self.soundbar_is_active = False
        self.soundbar_bar_heights = [0] * self.soundbar_bar_count
        self.update()
        
    def set_soundbar_audio_level(self, level):
        # 사운드바에 오디오 레벨 설정 (0-100)
        self.sound_bar.set_audio_level(level)
        self.mini_sound_bar.set_audio_level(level)
        freq_bands = [level, level, level]
        self.soundbar_is_active = True
        self.soundbar_freq_bands = freq_bands
        self._update_soundbar_bars(level, freq_bands)
        self.update()
    
    def closeEvent(self, event):
        if not self._allow_close:
            event.ignore()
        else:
            event.accept()
    
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
    
    def mousePressEvent(self, event):
        # 미니 모드일 때 전체 영역에서 드래그 가능하게
        if event.button() == Qt.MouseButton.LeftButton and self.window_mode == "mini":
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        # 미니 모드일 때 전체 영역에서 드래그 가능하게
        if event.buttons() & Qt.MouseButton.LeftButton and self.window_mode == "mini" and hasattr(self, 'drag_position'):
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()
    
    def _select_workspace(self):
        # Workspace 폴더 선택 대화상자 열기
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "작업 폴더 선택",
            "",
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks
        )
        if folder_path:
            self.workspace_selected.emit(folder_path)
    
    def set_workspace_info(self, name: str, path: str = ""):
        # Workspace 정보 UI에 표시
        self.current_workspace_name = name
        self.current_workspace_path = path
        if name:
            self.workspace_label.setText(f"Workspace: {name}")
            self.workspace_label.setStyleSheet("color: #00ffcc; padding: 5px;")
        else:
            self.workspace_label.setText("Workspace: 없음")
            self.workspace_label.setStyleSheet("color: #00a8cc; padding: 5px;")
    
    def request_permission(self, permission_name: str, permission_description: str) -> bool:
        """권한 요청 대화상자를 보여주고 사용자 응답을 반환"""
        dialog = PermissionRequestDialog(permission_name, permission_description, self)
        dialog.exec()
        return dialog.result_value

    def set_permission_manager(self, permission_manager):
        self.permission_manager = permission_manager

    def show_permission_settings(self):
        if self.permission_manager is None:
            QMessageBox.warning(self, "권한 관리", "권한 관리자가 아직 준비되지 않았습니다.")
            return
        PermissionSettingsDialog(self.permission_manager, self).exec()

    def set_tts_settings_manager(self, settings_manager):
        self.tts_settings_manager = settings_manager

    def show_tts_voice_settings(self):
        if self.tts_settings_manager is None:
            QMessageBox.warning(self, "TTS 목소리", "TTS 설정 관리자가 아직 준비되지 않았습니다.")
            return
        TTSVoiceDialog(self.tts_settings_manager, self).exec()

    def set_memory_manager(self, memory_manager):
        self.memory_manager = memory_manager

    def set_current_session(self, session_id: str):
        self.current_session_id = session_id

    def clear_conversation_display(self):
        self.user_text_label.setText("")
        self.assistant_text_label.setText("")

    def show_session_manager(self):
        if self.memory_manager is None:
            QMessageBox.warning(self, "대화 세션", "대화 저장소가 아직 준비되지 않았습니다.")
            return
        dialog = SessionManagerDialog(self.memory_manager, self.current_session_id, self)
        dialog.session_selected.connect(self.session_selected.emit)
        dialog.session_created.connect(self.session_created.emit)
        dialog.session_deleted.connect(self.session_deleted.emit)
        dialog.session_reset.connect(self.session_reset.emit)
        dialog.exec()
    
    def _on_text_submitted(self):
        text = self.text_input.text().strip()
        if text:
            self.text_submitted.emit(text)
            self.text_input.clear()
