"""Manual interactive Qt input harness; no window opens during collection."""

from __future__ import annotations

import sys


def build_diagnostic_window_class():
    from PyQt6.QtCore import pyqtSignal
    from PyQt6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

    class InputDiagnosticWindow(QWidget):
        text_submitted = pyqtSignal(str)

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Input Diagnostic")
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

        def _on_submit(self) -> None:
            text = self.input_field.text().strip()
            if text:
                self.text_submitted.emit(text)
                self.input_field.clear()

        def _on_text_received(self, text: str) -> None:
            self.label.setText(f"Received: {text}")

    return InputDiagnosticWindow


def main() -> int:
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    window_class = build_diagnostic_window_class()
    window = window_class()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
