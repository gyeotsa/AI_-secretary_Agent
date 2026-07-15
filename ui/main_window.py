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

class SoundBarWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 30)
        self.bar_count = 12
        self.bar_heights = [0] * self.bar_count
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_bars)
        self.timer.start(50)
        
    def update_bars(self):
        for i in range(self.bar_count):
            self.bar_heights[i] = random.randint(5, 25)
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
            gradient.setColorAt(0.0, QColor(0, 212, 255))
            gradient.setColorAt(1.0, QColor(0, 100, 150))
            
            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, y, bar_width, height, 2, 2)

class JarvisMainWindow(QWidget):
    command_triggered = pyqtSignal(str)
    text_submitted = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self._allow_close = True
        self.arc_angle = 0
        self.pulse_value = 0
        self.current_state = State.IDLE
        self.window_mode = "normal"
        self.normal_geometry = None
        self.init_ui()
        self.start_animations()
    
    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | 
                           Qt.WindowType.WindowStaysOnTopHint | 
                           Qt.WindowType.Tool)
        
        screen = QApplication.primaryScreen().geometry()
        window_width = 500
        window_height = 450
        x = (screen.width() - window_width) // 2
        y = (screen.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)
        self.normal_geometry = self.geometry()
        
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
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
        
        self.size_btn = QPushButton("◇")
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
        self.close_btn.clicked.connect(self.close)
        tab_layout.addWidget(self.close_btn)
        
        main_layout.addWidget(self.drag_tab)
        
        self.center_widget = QWidget()
        center_layout = QVBoxLayout(self.center_widget)
        center_layout.setContentsMargins(30, 20, 30, 20)
        
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
        center_layout.addWidget(self.status_label)
        center_layout.addSpacing(20)
        center_layout.addWidget(self.user_text_label)
        center_layout.addWidget(self.assistant_text_label)
        center_layout.addStretch()
        center_layout.addWidget(self.text_input)
        
        main_layout.addWidget(self.center_widget, 1)
        
        self.setLayout(main_layout)
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
            self.update()
    
    def toggle_window_mode(self):
        if self.window_mode == "normal":
            self.window_mode = "mini"
            self.size_btn.setText("□")
            self.normal_geometry = self.geometry()
            screen = QApplication.primaryScreen().geometry()
            mini_width = 220
            mini_height = 50
            x = screen.width() - mini_width - 20
            y = 20
            self.setGeometry(x, y, mini_width, mini_height)
            self.center_widget.hide()
            self.title_label.hide()
            self.sound_bar.show()
        elif self.window_mode == "mini":
            self.window_mode = "maximized"
            self.size_btn.setText("◇")
            self.sound_bar.hide()
            self.showMaximized()
            self.title_label.show()
            self.center_widget.show()
        else:
            self.window_mode = "normal"
            self.size_btn.setText("◇")
            self.showNormal()
            self.setGeometry(self.normal_geometry)
        self.update()
    
    def show_user_text(self, text: str):
        self.user_text_label.setText(f"> {text}")
    
    def show_assistant_text(self, text: str):
        self.assistant_text_label.setText(text)
    
    def closeEvent(self, event):
        if not self._allow_close:
            event.ignore()
        else:
            event.accept()
    
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
    
    def _on_text_submitted(self):
        text = self.text_input.text().strip()
        if text:
            self.text_submitted.emit(text)
            self.text_input.clear()
