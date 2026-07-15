import math
import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPainter, QColor, QRadialGradient
from core.state_machine import State

class AudioVisualizer(QWidget):
    BASE_RADIUS = 18
    NUM_DOTS = 7
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = State.IDLE
        self.wave_data = np.zeros(self.NUM_DOTS)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_animation)
        self.timer.start(30)
        self.time = 0
    
    def update_state(self, state: State):
        self.state = state
    
    def update_animation(self):
        self.time += 1
        
        if self.state == State.LISTENING:
            for i in range(self.NUM_DOTS):
                self.wave_data[i] = abs(math.sin(self.time * 0.2 + i * 0.5)) * 55
        elif self.state == State.RESPONDING:
            for i in range(self.NUM_DOTS):
                self.wave_data[i] = abs(math.sin(self.time * 0.15 + i * 0.3)) * 45
        elif self.state == State.PROCESSING:
            for i in range(self.NUM_DOTS):
                self.wave_data[i] = abs(math.sin(self.time * 0.1 + i * 0.8)) * 15
        else:
            self.wave_data *= 0.85
        
        self.update()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        width = self.width()
        height = self.height()
        center_y = height // 2
        
        spacing = max(50, width * 0.10)
        total_width = spacing * (self.NUM_DOTS - 1)
        start_x = (width - total_width) // 2
        
        for i in range(self.NUM_DOTS):
            x = start_x + i * spacing
            y = center_y
            
            if self.state in [State.LISTENING, State.RESPONDING]:
                y -= self.wave_data[i]
            elif self.state == State.PROCESSING:
                y -= self.wave_data[i]
            
            # 상태별 색상
            if self.state == State.IDLE:
                base_color = QColor(0, 220, 255, 90)
            elif self.state == State.LISTENING:
                base_color = QColor(0, 255, 255)
            elif self.state == State.PROCESSING:
                base_color = QColor(0, 220, 255)
            elif self.state == State.EXECUTING:
                base_color = QColor(0, 220, 255)
            elif self.state == State.RESPONDING:
                base_color = QColor(220, 80, 255)
            else:
                base_color = QColor(255, 100, 100)
            
            radius = self.BASE_RADIUS
            if self.state == State.IDLE:
                radius = self.BASE_RADIUS + abs(math.sin(self.time * 0.05)) * 1.2
            
            self._draw_glow_dot(painter, x, y, radius, base_color)
    
    def _draw_glow_dot(self, painter: QPainter, x: int, y: int, radius: float, color: QColor):
        # 글로우 효과
        glow_radius = radius * 2.5
        gradient = QRadialGradient(x, y, glow_radius)
        gradient.setColorAt(0, QColor(color.red(), color.green(), color.blue(), 60))
        gradient.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 0))
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(int(x - glow_radius), int(y - glow_radius),
                           int(glow_radius * 2), int(glow_radius * 2))
        
        # 점 본체
        painter.setBrush(color)
        painter.drawEllipse(int(x - radius), int(y - radius),
                           int(radius * 2), int(radius * 2))
