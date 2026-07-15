import sys
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLineEdit, QLabel
from PyQt6.QtCore import pyqtSignal

class TestWindow(QWidget):
    text_submitted = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Test Input")
        self.resize(400, 200)
        
        layout = QVBoxLayout()
        
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Enter text and press enter...")
        self.input_field.returnPressed.connect(self._on_submit)
        
        self.label = QLabel("Waiting for input...")
        
        layout.addWidget(self.input_field)
        layout.addWidget(self.label)
        
        self.setLayout(layout)
        
        self.text_submitted.connect(self._on_text_received)
    
    def _on_submit(self):
        print("[DEBUG] TestWindow._on_submit called!")
        text = self.input_field.text().strip()
        if text:
            print(f"[DEBUG] Emitting text_submitted with: {text}")
            self.text_submitted.emit(text)
            self.input_field.clear()
    
    def _on_text_received(self, text):
        print(f"[DEBUG] TestWindow._on_text_received called with: {text}")
        self.label.setText(f"Received: {text}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TestWindow()
    window.show()
    sys.exit(app.exec())
