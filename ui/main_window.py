import sys
import math
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel, 
                             QFrame, QHBoxLayout, QLineEdit)
from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QTimer
from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QFont, QPen, QRadialGradient
from .visualizer import AudioVisualizer
from core.state_machine import State

class DragTab(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(50)
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

class JarvisMainWindow(QWidget):
    command_triggered = pyqtSignal(str)
    text_submitted = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self._allow_close = True
        self.arc_angle = 0
        self.pulse_value = 0
        self.current_state = State.IDLE
        self.init_ui()
        self.start_animations()
    
    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | 
                           Qt.WindowType.WindowStaysOnTopHint | 
                           Qt.WindowType.Tool)
        
        screen = QApplication.primaryScreen().geometry()
        window_width = 700
        window_height = 600
        x = (screen.width() - window_width) // 2
        y = (screen.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)
        
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # DragTab
        self.drag_tab = DragTab(self)
        tab_layout = QHBoxLayout(self.drag_tab)
        tab_layout.setContentsMargins(20, 10, 20, 0)
        
        title_label = QLabel("JARVIS")
        title_font = QFont("Orbitron", 20, QFont.Weight.Bold)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #00d4ff; letter-spacing: 8px;")
        tab_layout.addWidget(title_label)
        
        tab_layout.addStretch()
        
        main_layout.addWidget(self.drag_tab)
        
        # 중앙 컨텐츠 영역
        center_widget = QWidget()
        center_layout = QVBoxLayout(center_widget)
        center_layout.setContentsMargins(30, 20, 30, 20)
        
        # 상태 표시 라벨
        self.status_label = QLabel("SYSTEM READY")
        status_font = QFont("Orbitron", 12)
        self.status_label.setFont(status_font)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #00d4ff; letter-spacing: 3px;")
        
        # 사용자 텍스트 라벨
        self.user_text_label = QLabel("")
        user_font = QFont("Consolas", 11)
        self.user_text_label.setFont(user_font)
        self.user_text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.user_text_label.setStyleSheet("color: #00a8cc; padding: 10px;")
        self.user_text_label.setWordWrap(True)
        
        # 어시스턴트 텍스트 라벨
        self.assistant_text_label = QLabel("")
        assistant_font = QFont("Consolas", 11)
        self.assistant_text_label.setFont(assistant_font)
        self.assistant_text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.assistant_text_label.setStyleSheet("color: #9945ff; padding: 10px;")
        self.assistant_text_label.setWordWrap(True)
        
        # 텍스트 입력 필드
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("SPEAK OR TYPE YOUR COMMAND...")
        self.text_input.setStyleSheet("""
            QLineEdit {
                background: rgba(0, 20, 40, 200);
                color: #00d4ff;
                border: 2px solid #00d4ff;
                border-radius: 20px;
                padding: 15px 20px;
                font-size: 14px;
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
        
        main_layout.addWidget(center_widget, 1)
        
        self.setLayout(main_layout)
        self.show()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # 상태에 따라 색상 변경
        if self.current_state in [State.PROCESSING, State.EXECUTING, State.RESPONDING]:
            # 바이올렛 색상
            gradient = QRadialGradient(self.width()/2, self.height()/2, self.width()/2)
            gradient.setColorAt(0.0, QColor(40, 0, 60))
            gradient.setColorAt(0.5, QColor(20, 0, 40))
            gradient.setColorAt(1.0, QColor(5, 0, 10))
            painter.fillRect(self.rect(), gradient)
            
            # 원형 테두리 색상 변경
            center_x = self.width() // 2
            center_y = self.height() // 2
            base_radius = min(self.width(), self.height()) // 3
            
            # 외부 원
            pen = QPen(QColor(153, 69, 255, 100), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 50, base_radius + 50)
            
            # 중간 원
            pen = QPen(QColor(153, 69, 255, 150), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 30, base_radius + 30)
            
            # 내부 원 - 회전하는 아크
            pen = QPen(QColor(190, 120, 255), 3)
            painter.setPen(pen)
            rect = [center_x - base_radius, center_y - base_radius, 
                    base_radius * 2, base_radius * 2]
            
            # 회전하는 아크 그리기
            start_angle = self.arc_angle * 16
            span_angle = 90 * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle), int(span_angle))
            
            # 반대 방향 아크
            start_angle2 = (self.arc_angle + 180) * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle2), int(span_angle))
            
            # 중앙 코어 - 펄싱 효과 (바이올렛)
            core_radius = 30 + self.pulse_value * 10
            core_gradient = QRadialGradient(center_x, center_y, core_radius)
            core_gradient.setColorAt(0.0, QColor(190, 120, 255, 200))
            core_gradient.setColorAt(0.5, QColor(153, 69, 255, 100))
            core_gradient.setColorAt(1.0, QColor(153, 69, 255, 0))
            painter.setBrush(core_gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPoint(center_x, center_y), int(core_radius), int(core_radius))
            
            # 스캔 라인 효과 (바이올렛)
            scan_y = center_y + math.sin(self.arc_angle * math.pi / 180) * 100
            pen = QPen(QColor(153, 69, 255, 50), 1)
            painter.setPen(pen)
            painter.drawLine(0, int(scan_y), self.width(), int(scan_y))
        else:
            # 기본 색상 (시안)
            gradient = QRadialGradient(self.width()/2, self.height()/2, self.width()/2)
            gradient.setColorAt(0.0, QColor(0, 30, 60))
            gradient.setColorAt(0.5, QColor(0, 15, 30))
            gradient.setColorAt(1.0, QColor(0, 5, 10))
            painter.fillRect(self.rect(), gradient)
            
            # 원형 테두리 그리기
            center_x = self.width() // 2
            center_y = self.height() // 2
            base_radius = min(self.width(), self.height()) // 3
            
            # 외부 원
            pen = QPen(QColor(0, 212, 255, 100), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 50, base_radius + 50)
            
            # 중간 원
            pen = QPen(QColor(0, 212, 255, 150), 2)
            painter.setPen(pen)
            painter.drawEllipse(QPoint(center_x, center_y), base_radius + 30, base_radius + 30)
            
            # 내부 원 - 회전하는 아크
            pen = QPen(QColor(0, 255, 204), 3)
            painter.setPen(pen)
            rect = [center_x - base_radius, center_y - base_radius, 
                    base_radius * 2, base_radius * 2]
            
            # 회전하는 아크 그리기
            start_angle = self.arc_angle * 16
            span_angle = 90 * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle), int(span_angle))
            
            # 반대 방향 아크
            start_angle2 = (self.arc_angle + 180) * 16
            painter.drawArc(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]), 
                           int(start_angle2), int(span_angle))
            
            # 중앙 코어 - 펄싱 효과
            core_radius = 30 + self.pulse_value * 10
            core_gradient = QRadialGradient(center_x, center_y, core_radius)
            core_gradient.setColorAt(0.0, QColor(0, 255, 204, 200))
            core_gradient.setColorAt(0.5, QColor(0, 212, 255, 100))
            core_gradient.setColorAt(1.0, QColor(0, 212, 255, 0))
            painter.setBrush(core_gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPoint(center_x, center_y), int(core_radius), int(core_radius))
            
            # 스캔 라인 효과
            scan_y = center_y + math.sin(self.arc_angle * math.pi / 180) * 100
            pen = QPen(QColor(0, 212, 255, 50), 1)
            painter.setPen(pen)
            painter.drawLine(0, int(scan_y), self.width(), int(scan_y))
    
    def start_animations(self):
        # 아크 회전 애니메이션
        self.arc_timer = QTimer()
        self.arc_timer.timeout.connect(self.update_arc)
        self.arc_timer.start(20)
        
        # 펄스 애니메이션
        self.pulse_timer = QTimer()
        self.pulse_timer.timeout.connect(self.update_pulse)
        self.pulse_timer.start(100)
    
    def update_arc(self):
        self.arc_angle = (self.arc_angle + 2) % 360
        self.update()
    
    def update_pulse(self):
        self.pulse_value = (self.pulse_value + 0.1) % (2 * math.pi)
        self.update()
    
    def update_state(self, state: State):
        self.current_state = state
        state_texts = {
            State.IDLE: "SYSTEM READY",
            State.LISTENING: "LISTENING...",
            State.PROCESSING: "PROCESSING...",
            State.EXECUTING: "EXECUTING...",
            State.RESPONDING: "RESPONDING...",
            State.ERROR: "SYSTEM ERROR"
        }
        self.status_label.setText(state_texts.get(state, "SYSTEM READY"))
        # 상태에 따라 상태 라벨 색상 변경
        if state in [State.PROCESSING, State.EXECUTING, State.RESPONDING]:
            self.status_label.setStyleSheet("color: #9945ff; letter-spacing: 3px;")
        else:
            self.status_label.setStyleSheet("color: #00d4ff; letter-spacing: 3px;")
        self.update()  # UI 업데이트
    
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
