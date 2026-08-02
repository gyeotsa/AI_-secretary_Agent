"""Role-focused desktop workspaces layered over the shared Jarvis agent runtime."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QPushButton, QSplitter, QTextEdit,
    QVBoxLayout, QWidget,
)

from core.specialist_workspaces import SpecialistWorkspaceSpec


STYLE = """
QMainWindow, QDialog, QWidget { background: #080d18; color: #d9f7ff; }
QFrame#panel, QListWidget, QTextEdit { background: #0d1728; border: 1px solid #24445b; border-radius: 8px; }
QPushButton { color: #67e8f9; background: #10243a; border: 1px solid #28728d; border-radius: 6px; padding: 7px 11px; }
QPushButton:hover { background: #173a50; }
QLabel#heading { color: #67e8f9; font-size: 18px; font-weight: bold; }
QLabel#muted { color: #87a6b7; }
"""


class SpecialistHubDialog(QDialog):
    workspace_requested = pyqtSignal(str)

    def __init__(self, specs, parent=None):
        super().__init__(parent)
        self.setWindowTitle("전문가 작업공간")
        self.resize(620, 420)
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        title = QLabel("전문가 작업공간")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("작업 종류에 맞는 모델과 도구, 결과 화면을 한곳에서 사용합니다."))
        self.list = QListWidget()
        for spec in specs:
            item = QListWidgetItem(f"{spec.title}\n{spec.description}")
            item.setData(Qt.ItemDataRole.UserRole, spec.key)
            item.setSizeHint(item.sizeHint().expandedTo(item.sizeHint()))
            self.list.addItem(item)
        layout.addWidget(self.list)
        open_button = QPushButton("선택한 작업공간 열기")
        open_button.clicked.connect(self._open)
        layout.addWidget(open_button)
        self.list.itemDoubleClicked.connect(lambda _item: self._open())
        if self.list.count():
            self.list.setCurrentRow(0)

    def _open(self):
        item = self.list.currentItem()
        if item:
            self.workspace_requested.emit(item.data(Qt.ItemDataRole.UserRole))
            self.accept()


class SpecialistWorkspaceWindow(QMainWindow):
    prompt_submitted = pyqtSignal(str)

    def __init__(self, spec: SpecialistWorkspaceSpec, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.setWindowTitle(f"JARVIS · {spec.title}")
        self.resize(1180, 760)
        self.setStyleSheet(STYLE)
        self._build()

    def _build(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        header = QHBoxLayout()
        heading = QLabel(self.spec.title)
        heading.setObjectName("heading")
        header.addWidget(heading)
        role = QLabel(f"전문 모델: {self.spec.model_role}")
        role.setObjectName("muted")
        header.addStretch()
        header.addWidget(role)
        layout.addLayout(header)
        layout.addWidget(QLabel(self.spec.description))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_asset_panel())
        splitter.addWidget(self._build_canvas_panel())
        splitter.addWidget(self._build_result_panel())
        splitter.setSizes([250, 600, 330])
        layout.addWidget(splitter, 1)

        command_row = QHBoxLayout()
        self.command = QTextEdit()
        self.command.setMaximumHeight(76)
        self.command.setPlaceholderText(f"{self.spec.title}에게 작업을 지시하세요. 기존 음성·채팅 문맥과 같은 실행기로 전달됩니다.")
        send = QPushButton("전문가에게 요청")
        send.clicked.connect(self._submit)
        command_row.addWidget(self.command, 1)
        command_row.addWidget(send)
        layout.addLayout(command_row)
        self.setCentralWidget(root)

    def _build_asset_panel(self):
        panel = QFrame(); panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("파일 및 도구"))
        self.assets = QListWidget()
        layout.addWidget(self.assets, 1)
        open_button = QPushButton("파일 열기")
        open_button.clicked.connect(self._choose_file)
        layout.addWidget(open_button)
        if self.spec.key == "document":
            for app in ("Word", "Excel", "한글", "PowerPoint", "PDF"):
                button = QPushButton(app)
                button.clicked.connect(lambda _checked=False, name=app: self._quick_prompt(name))
                layout.addWidget(button)
        else:
            status = QPushButton("Photoshop 연결 상태 확인")
            status.clicked.connect(lambda: self.prompt_submitted.emit("Photoshop 연결 상태를 확인해줘"))
            layout.addWidget(status)
        return panel

    def _build_canvas_panel(self):
        panel = QFrame(); panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("작업 미리보기"))
        if self.spec.key == "photoshop":
            self.preview = QLabel("이미지를 열면 이곳에 미리보기가 표시됩니다.")
            self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.preview.setWordWrap(True)
            layout.addWidget(self.preview, 1)
        else:
            self.editor = QTextEdit()
            self.editor.setPlaceholderText("텍스트 문서는 직접 검토·수정할 수 있습니다. Office 원본은 전문가 요청 결과와 Artifact를 통해 갱신됩니다.")
            layout.addWidget(self.editor, 1)
            save = QPushButton("텍스트 편집 내용 저장")
            save.clicked.connect(self._save_text)
            layout.addWidget(save)
        return panel

    def _build_result_panel(self):
        panel = QFrame(); panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("전문가 실행 결과"))
        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setPlaceholderText("진행 상황, 검증 결과와 생성된 파일이 여기에 표시됩니다.")
        layout.addWidget(self.results, 1)
        return panel

    def _submit(self):
        text = self.command.toPlainText().strip()
        if text:
            self.command.clear()
            self.results.append(f"나 > {text}")
            self.prompt_submitted.emit(text)

    def _quick_prompt(self, app: str):
        self.command.setPlainText(f"{app} 문서 작업을 시작할게. 필요한 내용을 먼저 물어봐줘")
        self.command.setFocus()

    def _choose_file(self):
        filters = "모든 파일 (*.*)"
        if self.spec.key == "document":
            filters = "문서 (*.docx *.xlsx *.pptx *.hwp *.hwpx *.pdf *.txt *.md *.csv);;모든 파일 (*.*)"
        elif self.spec.key == "photoshop":
            filters = "이미지 (*.psd *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;모든 파일 (*.*)"
        filename, _ = QFileDialog.getOpenFileName(self, "작업 파일 열기", "", filters)
        if filename:
            self.open_asset(filename)

    def open_asset(self, filename: str):
        path = Path(filename)
        item = QListWidgetItem(path.name)
        item.setToolTip(str(path))
        self.assets.addItem(item)
        if self.spec.key == "photoshop" and path.suffix.casefold() != ".psd":
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.preview.setPixmap(pixmap.scaled(560, 560, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        elif self.spec.key == "document" and path.suffix.casefold() in {".txt", ".md", ".csv", ".py", ".json"}:
            try:
                self.editor.setPlainText(path.read_text(encoding="utf-8"))
                self.editor.setProperty("source_path", str(path))
            except (OSError, UnicodeError) as exc:
                self.show_result(f"파일을 읽지 못했습니다: {exc}")

    def _save_text(self):
        source = self.editor.property("source_path")
        filename = source or QFileDialog.getSaveFileName(self, "텍스트 저장", "", "텍스트 (*.txt *.md *.csv);;모든 파일 (*.*)")[0]
        if filename:
            try:
                Path(filename).write_text(self.editor.toPlainText(), encoding="utf-8")
                self.show_result(f"저장 완료: {filename}")
            except OSError as exc:
                self.show_result(f"저장 실패: {exc}")

    def show_result(self, text: str):
        self.results.append(f"{self.spec.title} > {text}")
