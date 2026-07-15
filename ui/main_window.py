import sys
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel, QPushButton,
                             QFrame, QHBoxLayout, QFileDialog, QLineEdit)
from PyQt6.QtCore import Qt, QPoint, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QFont
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
                border-bottom: 1px solid rgba(0, 220, 255, 22);
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

class DexterMainWindow(QWidget):
    command_triggered = pyqtSignal(str)
    text_submitted = pyqtSignal(str)  # 텍스트 제출 시그널 추가
    
    def __init__(self):
        super().__init__()
        self._allow_close = True
        self.init_ui()
    
    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | 
                           Qt.WindowType.WindowStaysOnTopHint | 
                           Qt.WindowType.Tool)
        
        screen = QApplication.primaryScreen().geometry()
        # 창 크기를 적절히 조절 (전체 화면이 아니라 중앙에 작게)
        window_width = 600
        window_height = 500
        x = (screen.width() - window_width) // 2
        y = (screen.height() - window_height) // 2
        self.setGeometry(x, y, window_width, window_height)
        
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # DragTab
        self.drag_tab = DragTab(self)
        tab_layout = QHBoxLayout(self.drag_tab)
        tab_layout.setContentsMargins(10, 0, 10, 0)
        
        title_label = QLabel("DEXTER")
        title_font = QFont("Consolas", 14, QFont.Weight.Bold)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: rgba(0, 220, 255, 100); letter-spacing: 4px;")
        tab_layout.addWidget(title_label)
        
        pill_handle = QFrame()
        pill_handle.setFixedSize(64, 4)
        pill_handle.setStyleSheet("""
            QFrame {
                background: rgba(0, 220, 255, 55);
                border-radius: 2px;
            }
        """)
        tab_layout.addStretch()
        tab_layout.addWidget(pill_handle)
        tab_layout.addStretch()
        
        main_layout.addWidget(self.drag_tab)
        
        # Visualizer
        self.visualizer = AudioVisualizer(self)
        main_layout.addWidget(self.visualizer, 1)
        
        # Info Panel
        info_panel = QFrame()
        info_panel.setFixedHeight(130)  # 높이 조절
        info_layout = QVBoxLayout(info_panel)
        
        self.state_label = QLabel("상태: IDLE")
        self.user_text_label = QLabel("사용자 발화: ")
        self.assistant_text_label = QLabel("응답: ")
        
        # 텍스트 입력 필드 추가
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("자비스에게 질문하세요...")
        self.text_input.setStyleSheet("""
            QLineEdit {
                background: rgba(0, 0, 0, 150);
                color: rgba(0, 220, 255, 220);
                border: 1px solid rgba(0, 220, 255, 100);
                border-radius: 5px;
                padding: 8px;
                font-size: 12px;
            }
        """)
        self.text_input.returnPressed.connect(self._on_text_submitted)
        
        button_layout = QHBoxLayout()
        self.work_button = QPushButton("WORK")
        self.game_button = QPushButton("GAME")
        self.listen_toggle = QPushButton("🔴 마이크 OFF")  # 토글 버튼으로 변경
        self.listen_toggle.setCheckable(True)  # 체크 가능하게
        self.speak_button = QPushButton("🔊 응답 다시 듣기")
        self.add_doc_button = QPushButton("📄 문서 추가")
        self.view_profile_button = QPushButton("👤 프로필 보기")
        
        self.work_button.clicked.connect(lambda: self.command_triggered.emit("WORK"))
        self.game_button.clicked.connect(lambda: self.command_triggered.emit("GAME"))
        self.listen_toggle.toggled.connect(self._on_listen_toggled)  # 토글 시그널 연결
        self.speak_button.clicked.connect(lambda: self.command_triggered.emit("SPEAK"))
        self.add_doc_button.clicked.connect(lambda: self._open_file_dialog())
        self.view_profile_button.clicked.connect(lambda: self.command_triggered.emit("VIEW_PROFILE"))
        
        button_layout.addWidget(self.work_button)
        button_layout.addWidget(self.game_button)
        button_layout.addWidget(self.listen_toggle)
        button_layout.addWidget(self.speak_button)
        
        # 두 번째 버튼 레이아웃
        button_layout2 = QHBoxLayout()
        button_layout2.addWidget(self.add_doc_button)
        button_layout2.addWidget(self.view_profile_button)
        
        info_layout.addWidget(self.state_label)
        info_layout.addWidget(self.user_text_label)
        info_layout.addWidget(self.assistant_text_label)
        info_layout.addWidget(self.text_input)
        info_layout.addLayout(button_layout)
        info_layout.addLayout(button_layout2)
        
        main_layout.addWidget(info_panel)
        
        self.setLayout(main_layout)
        self.show()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor(2, 4, 12))
        gradient.setColorAt(0.45, QColor(6, 12, 30))
        gradient.setColorAt(0.85, QColor(4, 8, 20))
        gradient.setColorAt(1.0, QColor(1, 2, 8))
        painter.fillRect(self.rect(), gradient)
    
    def update_state(self, state: State):
        self.state_label.setText(f"상태: {state.value}")
        self.visualizer.update_state(state)
    
    def show_user_text(self, text: str):
        self.user_text_label.setText(f"사용자 발화: {text}")
    
    def show_assistant_text(self, text: str):
        self.assistant_text_label.setText(f"응답: {text}")
    
    def closeEvent(self, event):
        if not self._allow_close:
            event.ignore()
        else:
            event.accept()
    
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
    
    def _open_file_dialog(self):
        # 파일 다이얼로그 열기
        file_path, _ = QFileDialog.getOpenFileName(
            self, "문서 선택", "", "텍스트 파일 (*.txt);;모든 파일 (*)"
        )
        if file_path:
            self.command_triggered.emit(f"ADD_DOC:{file_path}")
    
    def _on_text_submitted(self):
        # 텍스트 입력 제출 시
        print("[DEBUG] ui/main_window.py: _on_text_submitted called!")
        text = self.text_input.text().strip()
        print(f"[DEBUG] ui/main_window.py: Input text: '{text}'")
        if text:
            print(f"[DEBUG] ui/main_window.py: Emitting text_submitted signal!")
            self.text_submitted.emit(text)
            self.text_input.clear()
    
    def _on_listen_toggled(self, checked: bool):
        # 토글 버튼 상태 변경 시
        if checked:
            self.listen_toggle.setText("🟢 마이크 ON")
            self.command_triggered.emit("LISTEN_START")
        else:
            self.listen_toggle.setText("🔴 마이크 OFF")
            self.command_triggered.emit("LISTEN_STOP")
