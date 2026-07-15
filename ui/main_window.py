import sys
import math
import random
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel, 
                             QFrame, QHBoxLayout, QLineEdit, QPushButton)
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
        self.setFixedSize(145, 25)  # 크기 조금 키움
        self.bar_count = 12
        self.bar_heights = [0] * self.bar_count
        self.is_speaking = False  # 자비스가 말하는 중인지 여부
        self.is_active = False  # 사운드가 활성화된 상태인지 (입력/출력 있을 때)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_bars)
        self.timer.start(50)
        
    def set_speaking(self, speaking):
        self.is_speaking = speaking
        self.is_active = speaking  # 자비스가 말할 때는 활성화
        
    def set_audio_level(self, level):
        # 실제 오디오 레벨을 받아서 바 높이를 설정 (0-100 사이 값)
        self.is_active = True
        for i in range(self.bar_count):
            # 레벨에 따라 바 높이를 계산 (가운데 바가 더 높게)
            base_height = (level / 100) * 25
            offset = random.randint(-2, 2)
            self.bar_heights[i] = max(0, min(28, int(base_height + offset)))
        self.update()
        
    def reset(self):
        # 사운드가 없을 때 초기 상태로
        self.is_active = False
        self.bar_heights = [0] * self.bar_count
        self.update()
        
    def update_bars(self):
        # 활성화 상태일 때만 애니메이션
        if not self.is_active:
            return
            
        if self.is_speaking:
            # 자비스가 말할 때는 더 큰 움직임
            for i in range(self.bar_count):
                self.bar_heights[i] = random.randint(10, 28)
        else:
            # 사용자 입력 시뮬레이션 (실제로는 set_audio_level로 대체)
            for i in range(self.bar_count):
                self.bar_heights[i] = random.randint(5, 20)
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
                # 자비스 음성은 바이올렛
                gradient.setColorAt(0.0, QColor(153, 69, 255))
                gradient.setColorAt(1.0, QColor(80, 20, 120))
            else:
                # 사용자 입력은 시안
                gradient.setColorAt(0.0, QColor(0, 212, 255))
                gradient.setColorAt(1.0, QColor(0, 100, 150))
            
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            if height > 0:  # 높이가 0보다 클 때만 그리기
                painter.drawRoundedRect(x, y, bar_width, height, 2, 2)


class CircularSoundBarWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.bar_count = 60
        self.bar_heights = [0] * self.bar_count
        self.is_speaking = False
        self.is_active = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_bars)
        self.timer.start(50)
        
    def set_speaking(self, speaking):
        self.is_speaking = speaking
        self.is_active = speaking
        
    def set_audio_level(self, level):
        self.is_active = True
        for i in range(self.bar_count):
            base_height = (level / 100) * 40
            offset = random.randint(-3, 3)
            self.bar_heights[i] = max(0, min(45, int(base_height + offset)))
        self.update()
        
    def reset(self):
        self.is_active = False
        self.bar_heights = [0] * self.bar_count
        self.update()
        
    def update_bars(self):
        if not self.is_active:
            return
            
        if self.is_speaking:
            for i in range(self.bar_count):
                self.bar_heights[i] = random.randint(15, 45)
        else:
            for i in range(self.bar_count):
                self.bar_heights[i] = random.randint(8, 30)
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        center_x = self.width() // 2
        center_y = self.height() // 2
        base_radius = min(self.width(), self.height()) // 4
        
        for i in range(self.bar_count):
            angle = (i / self.bar_count) * 2 * math.pi
            bar_width = 3
            gap = 2
            
            # 바의 안쪽 끝점
            inner_radius = base_radius
            inner_x = center_x + inner_radius * math.cos(angle)
            inner_y = center_y + inner_radius * math.sin(angle)
            
            # 바의 바깥쪽 끝점
            height = self.bar_heights[i]
            outer_radius = inner_radius + height
            outer_x = center_x + outer_radius * math.cos(angle)
            outer_y = center_y + outer_radius * math.sin(angle)
            
            # 바 그리기
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

class JarvisMainWindow(QWidget):
    command_triggered = pyqtSignal(str)
    text_submitted = pyqtSignal(str)
    close_requested = pyqtSignal()  # 종료 요청 시그널
    
    def __init__(self):
        super().__init__()
        self._allow_close = True
        self.arc_angle = 0
        self.pulse_value = 0
        self.current_state = State.IDLE
        self.window_mode = "normal"
        self.normal_geometry = None
        self.is_speaking = False  # 자비스가 말하는 중인지 여부
        self.init_ui()
        self.start_animations()
    
    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | 
                           Qt.WindowType.WindowStaysOnTopHint | 
                           Qt.WindowType.Tool)
        
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
        
        self.sound_bar = SoundBarWidget(self)
        self.sound_bar.hide()
        tab_layout.addWidget(self.sound_bar)
        
        tab_layout.addStretch()
        
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
        
        self.minimize_btn = QPushButton("─")
        self.minimize_btn.setStyleSheet(button_style)
        self.minimize_btn.setFixedSize(35, 35)
        self.minimize_btn.clicked.connect(self.showMinimized)
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
        self.mini_control_bar.minimize_btn.clicked.connect(self.showMinimized)
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
        
        # 원형 사운드바 추가
        self.circular_sound_bar = CircularSoundBarWidget(self)
        self.circular_sound_bar.setFixedSize(300, 300)
        self.circular_sound_bar.hide()  # 기본은 숨겨놓고 필요할 때 보여줌
        
        self.status_label = QLabel("SYSTEM READY")
        status_font = QFont("Orbitron", 11)
        self.status_label.setFont(status_font)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #00d4ff; letter-spacing: 3px;")
        
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
        center_layout.addWidget(self.circular_sound_bar, 0, Qt.AlignmentFlag.AlignCenter)
        center_layout.addSpacing(20)
        center_layout.addWidget(self.status_label)
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
    
    def start_animations(self):
        self.arc_timer = QTimer()
        self.arc_timer.timeout.connect(self.update_arc)
        self.arc_timer.start(20)
        
        self.pulse_timer = QTimer()
        self.pulse_timer.timeout.connect(self.update_pulse)
        self.pulse_timer.start(100)
    
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
            
            # LISTENING이나 RESPONDING일 때 원형 사운드바 보여주기
            if state in [State.LISTENING, State.RESPONDING]:
                self.circular_sound_bar.show()
            else:
                self.circular_sound_bar.hide()
                self.circular_sound_bar.reset()
            
            self.update()
    
    def toggle_window_mode(self):
        if self.window_mode == "normal":
            self.window_mode = "mini"
            self.size_btn.setText("□")
            self.normal_geometry = self.geometry()
            screen = QApplication.primaryScreen().geometry()
            mini_width = 160  # 가로 160으로
            mini_height = 50  # 세로 50으로
            x = screen.width() - mini_width - 20
            y = 20
            self.setGeometry(x, y, mini_width, mini_height)
            self.setFixedSize(mini_width, mini_height)  # 크기 강제 고정
            # 미니 모드일 때 레이아웃 여백 완전히 없애기
            self.main_layout.setContentsMargins(0, 0, 0, 0)
            self.main_layout.setSpacing(0)
            self.center_widget.hide()
            self.drag_tab.hide()
            self.mini_control_bar.show()
            self.mini_sound_bar.show()
            # 미니 모드에서 사운드바 초기 상태 (안 움직이게)
            self.mini_sound_bar.reset()
        elif self.window_mode == "mini":
            self.window_mode = "maximized"
            self.size_btn.setText("□")
            self.setMinimumSize(100, 100)  # 최소 크기 해제
            self.setMaximumSize(16777215, 16777215)  # 최대 크기 해제
            self.mini_control_bar.hide()
            self.mini_sound_bar.hide()
            self.showMaximized()
            self.drag_tab.show()
            self.center_widget.show()
        else:
            self.window_mode = "normal"
            self.size_btn.setText("□")
            self.setMinimumSize(100, 100)  # 최소 크기 해제
            self.setMaximumSize(16777215, 16777215)  # 최대 크기 해제
            self.showNormal()
            self.setGeometry(self.normal_geometry)
            self.mini_control_bar.hide()
            self.mini_sound_bar.hide()
            self.drag_tab.show()
            self.center_widget.show()
        self.update()
    
    def show_user_text(self, text: str):
        self.user_text_label.setText(f"> {text}")
    
    def show_assistant_text(self, text: str):
        self.assistant_text_label.setText(text)
    
    def set_soundbar_speaking(self, speaking):
        # 사운드바의 speaking 상태 설정
        self.sound_bar.set_speaking(speaking)
        self.mini_sound_bar.set_speaking(speaking)
        self.circular_sound_bar.set_speaking(speaking)
        
    def reset_soundbar(self):
        # 사운드바 리셋
        self.sound_bar.reset()
        self.mini_sound_bar.reset()
        self.circular_sound_bar.reset()
        
    def set_soundbar_audio_level(self, level):
        # 사운드바에 오디오 레벨 설정 (0-100)
        self.sound_bar.set_audio_level(level)
        self.mini_sound_bar.set_audio_level(level)
        self.circular_sound_bar.set_audio_level(level)
    
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
    
    def _on_text_submitted(self):
        text = self.text_input.text().strip()
        if text:
            self.text_submitted.emit(text)
            self.text_input.clear()
