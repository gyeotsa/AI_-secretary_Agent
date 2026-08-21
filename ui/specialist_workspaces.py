"""Role-focused desktop workspaces layered over the shared Jarvis agent runtime."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import uuid
import threading

from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QPushButton, QSplitter, QTextEdit,
    QVBoxLayout, QWidget, QInputDialog, QMessageBox, QLineEdit, QComboBox,
    QSlider, QScrollArea, QTabWidget, QFontComboBox, QSpinBox, QCheckBox,
    QColorDialog,
)

from core.specialist_workspaces import SpecialistWorkspaceSpec
from core.mockup_design import MockupDesignRuntime


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


class ImageDropList(QListWidget):
    """Image list accepting file drops and reporting normalized local paths."""
    paths_dropped = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        valid = [str(path.resolve()) for path in paths if path.suffix.casefold() in IMAGE_SUFFIXES and path.is_file()]
        if valid:
            self.paths_dropped.emit(valid); event.acceptProposedAction()
        else: super().dropEvent(event)


class ZoomableImageView(QScrollArea):
    """Centered fit-to-view preview with zoom and click-drag panning."""
    def __init__(self, parent=None):
        super().__init__(parent); self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel("학습용 시안을 추가하고 분석을 시작하세요.")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setWidget(self.label); self._source = QPixmap(); self._zoom = 1.0
        self._fit_to_view = True; self._pan_origin = None; self._pan_scroll = (0, 0)

    def set_preview(self, pixmap: QPixmap):
        self._source = QPixmap(pixmap); self._fit_to_view = True
        self.fit_to_view(); QTimer.singleShot(0, self.fit_to_view)

    def setText(self, text: str):
        self._source = QPixmap(); self.label.setPixmap(QPixmap()); self.label.setText(text)
        self._fit_to_view = True; self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def zoom_by(self, factor: float):
        if self._source.isNull(): return
        self._fit_to_view = False
        self._zoom = min(5.0, max(.05, self._zoom * factor)); self._refresh()

    def reset_zoom(self):
        self.fit_to_view()

    def fit_to_view(self):
        if self._source.isNull(): return
        viewport = self.viewport().size()
        available_w = max(1, viewport.width() - 16)
        available_h = max(1, viewport.height() - 16)
        self._zoom = min(available_w / self._source.width(),
                         available_h / self._source.height())
        self._fit_to_view = True; self._refresh()
        self.horizontalScrollBar().setValue(0); self.verticalScrollBar().setValue(0)

    def _refresh(self):
        if self._source.isNull(): return
        size = self._source.size() * self._zoom
        self.label.setPixmap(self._source.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation))
        self.label.resize(size); self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit_to_view and not self._source.isNull(): self.fit_to_view()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._source.isNull():
            self._pan_origin = event.position().toPoint()
            self._pan_scroll = (self.horizontalScrollBar().value(),
                                self.verticalScrollBar().value())
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.position().toPoint() - self._pan_origin
            self.horizontalScrollBar().setValue(self._pan_scroll[0] - delta.x())
            self.verticalScrollBar().setValue(self._pan_scroll[1] - delta.y())
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pan_origin is not None:
            self._pan_origin = None; self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept(); return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15); event.accept(); return
        super().wheelEvent(event)


class SketchCanvas(QWidget):
    """Small raster annotation canvas used as visual guidance, not final artwork."""
    def __init__(self, parent=None):
        super().__init__(parent); self.setMinimumSize(480, 300)
        self.image = QImage(1000, 700, QImage.Format.Format_ARGB32); self.image.fill(Qt.GlobalColor.white)
        self.last_point = QPoint(); self.pen_color = QColor("#ef4444"); self.pen_width = 8

    def clear(self): self.image.fill(Qt.GlobalColor.white); self.update()
    def has_ink(self) -> bool:
        sample = self.image.scaled(100, 70, Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.FastTransformation)
        return any(QColor(sample.pixel(x, y)).lightness() < 245
                   for x in range(sample.width()) for y in range(sample.height()))
    def set_color(self, color: QColor): self.pen_color = color
    def set_width(self, width: int): self.pen_width = width

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton: self.last_point = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if not event.buttons() & Qt.MouseButton.LeftButton: return
        point = event.position().toPoint(); sx = self.image.width() / max(1, self.width()); sy = self.image.height() / max(1, self.height())
        painter = QPainter(self.image); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.pen_color, self.pen_width, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawLine(QPoint(int(self.last_point.x()*sx), int(self.last_point.y()*sy)),
                         QPoint(int(point.x()*sx), int(point.y()*sy)))
        painter.end(); self.last_point = point; self.update()

    def paintEvent(self, _event):
        painter = QPainter(self); painter.fillRect(self.rect(), Qt.GlobalColor.white)
        painter.drawImage(self.rect(), self.image)

    def save_guidance(self) -> str:
        # The vision runtime applies the same allowed-path policy as file tools.
        # Keep transient sketches inside the project instead of the OS temp dir.
        root = Path(__file__).resolve().parents[1] / "data" / "cache" / "mockup_guidance"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"sketch_{uuid.uuid4().hex}.png"
        return str(target) if self.image.save(str(target), "PNG") else ""


STYLE = """
QMainWindow, QDialog { background: #080d16; }
QWidget { color: #dce8f5; font-family: "Segoe UI"; font-size: 12px; }
QFrame#panel {
    background: #0d1624; border: 1px solid #203247; border-radius: 12px;
}
QListWidget, QTextEdit, QLineEdit, QComboBox {
    color: #e8f2fb; background: #0a121e; border: 1px solid #253a51;
    border-radius: 9px; padding: 7px; selection-background-color: #1f6178;
}
QListWidget:focus, QTextEdit:focus, QLineEdit:focus, QComboBox:focus { border-color: #4bc7de; }
QListWidget::item { padding: 7px; border-radius: 5px; }
QListWidget::item:selected { background: #18394b; color: #f3fbff; }
QPushButton {
    color: #cbeaf2; background: #132338; border: 1px solid #29455f;
    border-radius: 8px; padding: 8px 12px; font-weight: 600;
}
QPushButton:hover { color: #ffffff; background: #19314a; border-color: #4bbbd0; }
QPushButton:pressed { background: #0e1b2b; }
QLabel#heading { color: #eaf8ff; font-size: 19px; font-weight: 650; }
QLabel#section { color: #70d8e9; font-size: 13px; font-weight: 650; }
QLabel#muted { color: #7f96aa; }
QSplitter::handle { background: transparent; width: 8px; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #2b4258; min-height: 28px; border-radius: 4px; }
QToolTip { color: #eaf7ff; background: #101d2d; border: 1px solid #29445f; padding: 5px; }
QTabWidget::pane { border: 1px solid #29445f; border-radius: 8px; top: -1px; }
QTabBar::tab {
    color: #9bb4c9; background: #101b2a; border: 1px solid #29445f;
    padding: 8px 16px; margin-right: 3px; min-width: 96px;
}
QTabBar::tab:selected { color: #ffffff; background: #17667c; border-color: #57d3e7; font-weight: 700; }
QTabBar::tab:hover:!selected { color: #eaf8ff; background: #183047; }
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
    prompt_submitted = pyqtSignal(object)

    def __init__(self, spec: SpecialistWorkspaceSpec, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.setWindowTitle(f"JARVIS · {spec.title}")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.resize(1180, 760)
        self.setStyleSheet(STYLE)
        self._build()

    def _build(self):
        root = QWidget()
        layout = QVBoxLayout(root); layout.setContentsMargins(22, 20, 22, 20); layout.setSpacing(14)
        header = QHBoxLayout()
        heading = QLabel(self.spec.title)
        heading.setObjectName("heading")
        header.addWidget(heading)
        role = QLabel(f"전문 모델: {self.spec.model_role}")
        role.setObjectName("muted")
        header.addStretch()
        header.addWidget(role)
        layout.addLayout(header)
        description = QLabel(self.spec.description); description.setObjectName("muted")
        description.setWordWrap(True); layout.addWidget(description)

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
        section = QLabel("파일 및 도구"); section.setObjectName("section"); layout.addWidget(section)
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
        elif self.spec.key == "photoshop":
            status = QPushButton("Photoshop 연결 상태 확인")
            status.clicked.connect(
                lambda: self.prompt_submitted.emit(self._request_payload("Photoshop 연결 상태를 확인해줘"))
            )
            layout.addWidget(status)
        elif self.spec.key == "coding":
            for label, prompt in (
                ("프로젝트 구조 분석", "첨부한 프로젝트의 구조와 현재 구현 상태를 분석하고 근거를 정리해줘"),
                ("테스트 실행·진단", "첨부한 프로젝트에서 관련 테스트를 실행하고 실패 원인을 진단해줘"),
                ("변경 검토", "현재 변경 사항을 검토하고 버그·회귀 위험·누락된 검증을 찾아줘"),
            ):
                button = QPushButton(label)
                button.clicked.connect(lambda _checked=False, value=prompt: self._set_prompt(value))
                layout.addWidget(button)
        elif self.spec.key == "research":
            for label, prompt in (
                ("최신 정보 조사", "이 주제를 웹에서 최신 정보까지 조사하고 출처와 확인 시각을 함께 정리해줘"),
                ("출처 교차 검증", "첨부 자료의 핵심 주장을 신뢰할 수 있는 출처로 교차 검증해줘"),
                ("근거 보고서", "조사 결과를 주장·근거·불확실성·출처로 나눈 보고서로 작성해줘"),
            ):
                button = QPushButton(label)
                button.clicked.connect(lambda _checked=False, value=prompt: self._set_prompt(value))
                layout.addWidget(button)
        return panel

    def _build_canvas_panel(self):
        panel = QFrame(); panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        section = QLabel("작업 미리보기"); section.setObjectName("section"); layout.addWidget(section)
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
        section = QLabel("전문가 실행 결과"); section.setObjectName("section"); layout.addWidget(section)
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
            self.prompt_submitted.emit(self._request_payload(text))

    def _request_payload(self, text: str) -> dict:
        return {
            "workspace": self.spec.key,
            "instruction": str(text).strip(),
            "attachments": [
                self.assets.item(index).toolTip()
                for index in range(self.assets.count())
                if self.assets.item(index).toolTip()
            ],
        }

    def _quick_prompt(self, app: str):
        self.command.setPlainText(f"{app} 문서 작업을 시작할게. 필요한 내용을 먼저 물어봐줘")
        self.command.setFocus()

    def _set_prompt(self, text: str):
        self.command.setPlainText(text)
        self.command.setFocus()

    def _choose_file(self):
        filters = "모든 파일 (*.*)"
        if self.spec.key == "document":
            filters = "문서 (*.docx *.xlsx *.pptx *.hwp *.hwpx *.pdf *.txt *.md *.csv);;모든 파일 (*.*)"
        elif self.spec.key == "photoshop":
            filters = "이미지 (*.psd *.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;모든 파일 (*.*)"
        elif self.spec.key == "coding":
            filters = "소스 코드 (*.py *.js *.ts *.tsx *.jsx *.java *.kt *.cpp *.c *.h *.cs *.go *.rs *.toml *.yaml *.yml *.json *.md);;모든 파일 (*.*)"
        elif self.spec.key == "research":
            filters = "조사 자료 (*.pdf *.txt *.md *.csv *.json *.html *.htm);;모든 파일 (*.*)"
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
        elif self.spec.key in {"document", "coding", "research"} and path.suffix.casefold() in {
            ".txt", ".md", ".csv", ".py", ".json", ".js", ".ts", ".tsx", ".jsx",
            ".java", ".kt", ".cpp", ".c", ".h", ".cs", ".go", ".rs", ".toml",
            ".yaml", ".yml", ".html", ".htm",
        }:
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


class MockupWorkspaceWindow(QMainWindow):
    """Dedicated two-stage workspace: learn references, then render production assets."""
    prompt_submitted = pyqtSignal(str)
    analysis_done = pyqtSignal(object)
    render_done = pyqtSignal(object)
    model_status_done = pyqtSignal(object)
    progress_message = pyqtSignal(str)
    operation_failed = pyqtSignal(str)
    adjustment_done = pyqtSignal(object)

    def __init__(self, spec: SpecialistWorkspaceSpec, parent=None, runtime=None, team_runtime=None):
        super().__init__(parent)
        self.spec = spec
        self.runtime = runtime or MockupDesignRuntime()
        self.team_runtime = team_runtime
        if self.team_runtime is not None:
            self.runtime.team_runtime = self.team_runtime
        self.reference_paths, self.production_paths = [], []
        self.edit_attachment_paths = []
        self.active_profile_id = ""
        self.preview_history = []
        self.preview_index = -1
        self.preview_metadata = {}
        self._adjustment_serial = 0
        self._ai_edit_serial = 0
        self.setWindowTitle("JARVIS · 시안 제작 전문가")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.resize(1320, 820)
        self.setStyleSheet(STYLE)
        self.analysis_done.connect(self._on_analysis_done)
        self.render_done.connect(self._on_render_done)
        self.operation_failed.connect(self._on_failed)
        self.model_status_done.connect(self._on_model_status)
        self.progress_message.connect(lambda message: self.details.append(f"\n{message}"))
        self.adjustment_done.connect(self._on_adjustment_done)
        self._build()
        self._reload_profiles()
        self._on_model_status(self.runtime.generation_status())

    def _build(self):
        root = QWidget(); outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 20, 22, 20); outer.setSpacing(14)
        heading = QLabel("시안 제작 전문가"); heading.setObjectName("heading")
        outer.addWidget(heading)
        guide = QLabel("① 학습용 시안에서 디자인 형식을 분석한 뒤  ② 제작용 사진에 그 스타일을 적용합니다. 두 자료는 서로 섞이지 않습니다.")
        guide.setWordWrap(True); guide.setObjectName("muted"); outer.addWidget(guide)
        if self.team_runtime is not None:
            team_status = QLabel(self.team_runtime.describe_team("mockup"))
            team_status.setWordWrap(True); team_status.setObjectName("muted")
            team_status.setToolTip(
                "여러 역할이 같은 작업 기억을 공유하되, 로컬 메모리를 아끼기 위해 "
                "Vision과 추론 모델을 순서대로 사용합니다."
            )
            outer.addWidget(team_status)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._upload_panel("학습용 시안", True))
        splitter.addWidget(self._upload_panel("제작용 사진", False))
        splitter.addWidget(self._preview_panel())
        splitter.setSizes([340, 340, 640]); outer.addWidget(splitter, 1)
        self.setCentralWidget(root)

    def _upload_panel(self, title: str, learning: bool):
        panel = QFrame(); panel.setObjectName("panel"); layout = QVBoxLayout(panel)
        label = QLabel(("1. " if learning else "2. ") + title); label.setObjectName("section"); layout.addWidget(label)
        help_text = ("완성된 기존 시안 여러 장을 추가하세요. 원본 사진이 아니라 참고할 디자인 결과물입니다."
                     if learning else "새 시안에 실제로 사용할 제품·인물·배경 사진을 추가하세요.")
        help_label = QLabel(help_text); help_label.setWordWrap(True); help_label.setObjectName("muted"); layout.addWidget(help_label)
        listing = ImageDropList(); listing.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection); layout.addWidget(listing, 1)
        listing.paths_dropped.connect(lambda paths, flag=learning: self._append_image_paths(paths, flag))
        add = QPushButton("여러 장 추가"); remove = QPushButton("선택 제거")
        add.clicked.connect(lambda: self._add_images(learning)); remove.clicked.connect(lambda: self._remove_images(learning))
        buttons = QHBoxLayout(); buttons.addWidget(add); buttons.addWidget(remove); layout.addLayout(buttons)
        if learning:
            self.reference_list = listing
            self.profile_list = QListWidget(); self.profile_list.setMaximumHeight(150)
            self.profile_list.currentItemChanged.connect(self._select_profile)
            self.active_profile_card = QFrame(); self.active_profile_card.setObjectName("activeProfileCard")
            active_layout = QVBoxLayout(self.active_profile_card); active_layout.setContentsMargins(12, 9, 12, 9)
            active_caption = QLabel("현재 적용할 스타일"); active_caption.setObjectName("muted")
            self.active_profile_label = QLabel("선택되지 않음")
            self.active_profile_label.setStyleSheet("font-size: 15px; font-weight: 700; color: #f59e0b;")
            self.active_profile_meta = QLabel("아래 목록에서 스타일을 클릭해 주세요.")
            self.active_profile_meta.setObjectName("muted"); self.active_profile_meta.setWordWrap(True)
            active_layout.addWidget(active_caption); active_layout.addWidget(self.active_profile_label)
            active_layout.addWidget(self.active_profile_meta); layout.addWidget(self.active_profile_card)
            profile_row = QHBoxLayout()
            profile_row.addWidget(QLabel("저장된 스타일 프로필"), 1)
            delete_profile = QPushButton("선택 프로필 삭제")
            delete_profile.clicked.connect(self._delete_profile)
            profile_row.addWidget(delete_profile)
            layout.addLayout(profile_row); layout.addWidget(self.profile_list)
            analyze = QPushButton("참고 시안 분석·학습")
            analyze.clicked.connect(self._learn_style); layout.addWidget(analyze)
        else:
            self.production_list = listing
            self.instruction = QTextEdit(); self.instruction.setMaximumHeight(105)
            self.instruction.setPlaceholderText(
                "원하는 구성·분위기·색감·배치 등 제작 지시를 자유롭게 입력하세요. "
                "이 문장은 이미지에 출력되지 않으며 생성 지시로만 사용됩니다."
            )
            layout.addWidget(self.instruction)
            self.visible_copy = QLineEdit()
            self.visible_copy.setPlaceholderText("이미지에 실제로 표시할 문구 (선택 사항 · 비워두면 글자를 넣지 않음)")
            layout.addWidget(self.visible_copy)
            output_row = QHBoxLayout(); self.output_dir = QLineEdit(str(Path("data/mockup_outputs").resolve()))
            choose = QPushButton("출력 폴더"); choose.clicked.connect(self._choose_output_dir)
            output_row.addWidget(self.output_dir, 1); output_row.addWidget(choose); layout.addLayout(output_row)
            self.backend_selector = QComboBox()
            self.backend_selector.addItem("권장 · AI 설계 + 원본 보존 렌더링", "auto")
            self.backend_selector.addItem("실험적 · 생성형 스타일 배경 (참고 인물 재생성 위험)", "generative")
            self.backend_selector.addItem("고품질 · SDXL 배경 + 정확한 SVG 문구 (별도 모델 준비)", "generative_sdxl")
            self.backend_selector.addItem("AI 설계 + 원본 보존 렌더링", "local")
            layout.addWidget(self.backend_selector)
            model_row = QHBoxLayout()
            self.model_status = QLabel("생성형 모델 상태 확인 중…")
            self.model_status.setWordWrap(True); self.model_status.setObjectName("muted")
            prepare = QPushButton("생성형 모델 준비")
            prepare.clicked.connect(self._prepare_models)
            model_row.addWidget(self.model_status, 1); model_row.addWidget(prepare)
            layout.addLayout(model_row)
            self.render_button = QPushButton("스타일을 먼저 선택해 주세요")
            self.render_button.setEnabled(False)
            self.render_button.clicked.connect(self._render); layout.addWidget(self.render_button)
        return panel

    def _preview_panel(self):
        panel = QFrame(); panel.setObjectName("panel"); layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("분석 및 결과 미리보기"))
        tabs = QTabWidget(); self.preview = ZoomableImageView()
        tabs.addTab(self.preview, "결과 미리보기")
        sketch_panel = QWidget(); sketch_layout = QVBoxLayout(sketch_panel)
        self.sketch_canvas = SketchCanvas(); sketch_layout.addWidget(self.sketch_canvas, 1)
        sketch_tools = QHBoxLayout()
        clear_sketch = QPushButton("스케치 지우기"); clear_sketch.clicked.connect(self.sketch_canvas.clear)
        sketch_color = QPushButton("펜 색상"); sketch_color.clicked.connect(self._choose_sketch_color)
        self.sketch_width = QSlider(Qt.Orientation.Horizontal); self.sketch_width.setRange(2, 30); self.sketch_width.setValue(8)
        self.sketch_width.valueChanged.connect(self.sketch_canvas.set_width)
        sketch_tools.addWidget(sketch_color); sketch_tools.addWidget(QLabel("굵기")); sketch_tools.addWidget(self.sketch_width, 1); sketch_tools.addWidget(clear_sketch)
        sketch_layout.addLayout(sketch_tools); tabs.addTab(sketch_panel, "설명 스케치")
        layout.addWidget(tabs, 3)
        zoom_row = QHBoxLayout()
        for label, factor in (("축소", .8), ("확대", 1.25)):
            button = QPushButton(label); button.clicked.connect(lambda _checked=False, value=factor: self.preview.zoom_by(value)); zoom_row.addWidget(button)
        fit = QPushButton("전체 맞춤"); fit.clicked.connect(self.preview.reset_zoom); zoom_row.addWidget(fit); zoom_row.addStretch()
        layout.addLayout(zoom_row)
        self.details = QTextEdit(); self.details.setReadOnly(True); layout.addWidget(self.details, 2)
        edit_row = QHBoxLayout()
        self.edit_instruction = QLineEdit()
        self.edit_instruction.setPlaceholderText("현재 시안을 어떻게 바꿀지 AI에게 지시하세요")
        self.ai_edit_button = QPushButton("AI 수정")
        self.ai_edit_button.clicked.connect(self._edit_with_ai)
        edit_row.addWidget(self.edit_instruction, 1); edit_row.addWidget(self.ai_edit_button)
        layout.addLayout(edit_row)
        attachment_row = QHBoxLayout()
        self.edit_attachments = ImageDropList(); self.edit_attachments.setMaximumHeight(75)
        self.edit_attachments.paths_dropped.connect(self._append_edit_attachments)
        attach = QPushButton("수정 참고사진 첨부"); attach.clicked.connect(self._choose_edit_attachments)
        clear_attach = QPushButton("첨부 비우기"); clear_attach.clicked.connect(self._clear_edit_attachments)
        attachment_row.addWidget(self.edit_attachments, 1); attachment_row.addWidget(attach); attachment_row.addWidget(clear_attach)
        layout.addLayout(attachment_row)
        font_row = QHBoxLayout(); self.font_family = QFontComboBox(); self.font_size = QSpinBox()
        self.font_size.setRange(12, 240); self.font_size.setValue(64)
        self.font_bold = QCheckBox("굵게"); apply_font = QPushButton("글꼴 적용")
        apply_font.clicked.connect(self._apply_font_controls)
        font_row.addWidget(QLabel("글꼴")); font_row.addWidget(self.font_family, 1)
        font_row.addWidget(QLabel("크기")); font_row.addWidget(self.font_size); font_row.addWidget(self.font_bold); font_row.addWidget(apply_font)
        layout.addLayout(font_row)
        tools = QHBoxLayout()
        for label, operation in (("↶", "rotate_left"), ("↷", "rotate_right"),
                                 ("좌우 반전", "flip_horizontal"), ("상하 반전", "flip_vertical")):
            button = QPushButton(label); button.clicked.connect(lambda _checked=False, op=operation: self._manual_edit(op))
            tools.addWidget(button)
        self.undo_button = QPushButton("실행 취소"); self.undo_button.clicked.connect(self._undo_preview)
        self.redo_button = QPushButton("다시 실행"); self.redo_button.clicked.connect(self._redo_preview)
        tools.addWidget(self.undo_button); tools.addWidget(self.redo_button); layout.addLayout(tools)
        self.adjustment_sliders, self.adjustment_labels = {}, {}
        for title, operation in (("밝기", "brightness"), ("대비", "contrast"),
                                 ("채도", "saturation"), ("선명도", "sharpness")):
            row = QHBoxLayout(); name = QLabel(title); name.setFixedWidth(42)
            slider = QSlider(Qt.Orientation.Horizontal); slider.setRange(0, 100); slider.setValue(50)
            value_label = QLabel("50%"); value_label.setObjectName("muted"); value_label.setFixedWidth(34)
            self.adjustment_sliders[operation] = slider; self.adjustment_labels[operation] = value_label
            slider.valueChanged.connect(lambda value, op=operation: self._schedule_live_adjustment(op, value))
            row.addWidget(name); row.addWidget(slider, 1); row.addWidget(value_label); layout.addLayout(row)
        self.adjustment_timer = QTimer(self); self.adjustment_timer.setSingleShot(True)
        self.adjustment_timer.setInterval(120); self.adjustment_timer.timeout.connect(self._apply_live_adjustments)
        save_row = QHBoxLayout(); save_row.addStretch(1)
        self.save_preview_button = QPushButton("미리보기 저장")
        self.save_preview_button.setEnabled(False); self.save_preview_button.clicked.connect(self._save_preview)
        save_row.addWidget(self.save_preview_button); layout.addLayout(save_row)
        return panel

    def _delete_profile(self):
        item = self.profile_list.currentItem()
        if item is None:
            QMessageBox.information(self, "스타일 삭제", "삭제할 스타일 프로필을 선택해 주세요."); return
        profile_id = item.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(self, "스타일 삭제", "선택한 스타일 프로필을 삭제할까요?\n원본 이미지는 삭제하지 않습니다.") != QMessageBox.StandardButton.Yes:
            return
        self.runtime.delete_profile(profile_id)
        if self.active_profile_id == profile_id: self.active_profile_id = ""
        self._reload_profiles(); self.details.append("\n선택한 스타일 프로필을 삭제했습니다.")

    def _add_images(self, learning: bool):
        files, _ = QFileDialog.getOpenFileNames(
            self, "학습용 시안 선택" if learning else "제작용 사진 선택", "",
            "이미지 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        self._append_image_paths(files, learning)

    def _append_image_paths(self, files, learning: bool):
        paths, listing = ((self.reference_paths, self.reference_list) if learning
                          else (self.production_paths, self.production_list))
        for filename in files or ():
            resolved = str(Path(filename).resolve())
            if Path(resolved).is_file() and Path(resolved).suffix.casefold() in IMAGE_SUFFIXES and resolved not in paths:
                paths.append(resolved); item = QListWidgetItem(Path(resolved).name)
                item.setToolTip(resolved); listing.addItem(item)

    def _choose_edit_attachments(self):
        files, _ = QFileDialog.getOpenFileNames(self, "수정 참고사진 선택", "",
                                                "이미지 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)")
        self._append_edit_attachments(files)

    def _append_edit_attachments(self, files):
        for filename in files or ():
            resolved = str(Path(filename).resolve())
            if Path(resolved).is_file() and Path(resolved).suffix.casefold() in IMAGE_SUFFIXES and resolved not in self.edit_attachment_paths:
                self.edit_attachment_paths.append(resolved)
                item = QListWidgetItem(Path(resolved).name); item.setToolTip(resolved); self.edit_attachments.addItem(item)

    def _clear_edit_attachments(self):
        self.edit_attachment_paths.clear(); self.edit_attachments.clear()

    def _choose_sketch_color(self):
        color = QColorDialog.getColor(self.sketch_canvas.pen_color, self, "스케치 펜 색상")
        if color.isValid(): self.sketch_canvas.set_color(color)

    def _apply_font_controls(self):
        if self.preview_index < 0:
            QMessageBox.information(self, "글꼴", "먼저 시안 미리보기를 만들어 주세요."); return
        family = self.font_family.currentFont().family()
        weight = "굵게" if self.font_bold.isChecked() else "보통"
        self.edit_instruction.setText(
            f"문구 글꼴을 '{family}'로 바꾸고 글자 크기를 {self.font_size.value()}픽셀, 굵기는 {weight}로 설정해줘."
        )
        self._edit_with_ai()

    def _remove_images(self, learning: bool):
        paths, listing = ((self.reference_paths, self.reference_list) if learning
                          else (self.production_paths, self.production_list))
        for item in list(listing.selectedItems()):
            row = listing.row(item); listing.takeItem(row); paths.pop(row)

    def _learn_style(self):
        if not self.reference_paths:
            QMessageBox.information(self, "시안 학습", "학습용 시안을 먼저 추가해 주세요."); return
        name, accepted = QInputDialog.getText(self, "스타일 이름", "분석한 스타일의 이름:", text="새 시안 스타일")
        if not accepted: return
        self.details.setPlainText("참고 시안을 분석하고 있습니다…")
        threading.Thread(target=self._learn_worker, args=(list(self.reference_paths), name), daemon=True).start()

    def _learn_worker(self, paths, name):
        try: self.analysis_done.emit(self.runtime.learn_style(paths, name=name))
        except Exception as exc: self.operation_failed.emit(str(exc))

    def _on_analysis_done(self, profile):
        self.active_profile_id = profile.profile_id; self._reload_profiles(profile.profile_id)
        self.details.setPlainText(
            f"스타일: {profile.name}\n참고 이미지: {len(profile.reference_paths)}장\n"
            f"방향: {profile.orientation}\n대표 비율: {profile.median_aspect_ratio:.3f}\n"
            f"색상: {', '.join(profile.palette)}\n"
            "생성 방식: AI 학습 기반 가변 장면 설계\n\n"
            f"구조·Vision 분석\n{profile.vision_analysis}"
        )
        self.preview.setText("스타일 분석을 완료했습니다. 이제 제작용 사진을 추가해 시안을 만들 수 있습니다.")

    def _reload_profiles(self, selected_id=""):
        self.profile_list.clear()
        for profile in self.runtime.list_profiles():
            item = QListWidgetItem(f"{profile.name} · 참고 {len(profile.reference_paths)}장")
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_id); self.profile_list.addItem(item)
            if profile.profile_id == selected_id: self.profile_list.setCurrentItem(item)
        if not self.active_profile_id:
            self._update_active_profile_card(None)

    def _select_profile(self, current, _previous=None):
        if current:
            self.active_profile_id = current.data(Qt.ItemDataRole.UserRole)
            try: profile = self.runtime.load_profile(self.active_profile_id)
            except Exception: profile = None
            self._update_active_profile_card(profile)
        else:
            self.active_profile_id = ""; self._update_active_profile_card(None)

    def _update_active_profile_card(self, profile):
        selected = profile is not None
        self.active_profile_label.setText(profile.name if selected else "선택되지 않음")
        self.active_profile_label.setStyleSheet(
            "font-size: 15px; font-weight: 700; color: #67e8f9;" if selected else
            "font-size: 15px; font-weight: 700; color: #f59e0b;"
        )
        self.active_profile_meta.setText(
            f"참고 이미지 {len(profile.reference_paths)}장 · 클릭한 이 스타일이 다음 생성에 사용됩니다."
            if selected else "아래 목록에서 스타일을 클릭해 주세요."
        )
        self.active_profile_card.setStyleSheet(
            "QFrame#activeProfileCard { background: #0d2633; border: 1px solid #22d3ee; border-radius: 10px; }"
            if selected else
            "QFrame#activeProfileCard { background: #211b12; border: 1px solid #a16207; border-radius: 10px; }"
        )
        if hasattr(self, "render_button"):
            self.render_button.setEnabled(selected)
            self.render_button.setText("선택한 스타일로 시안 제작" if selected else "스타일을 먼저 선택해 주세요")

    def _choose_output_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "시안 출력 폴더", self.output_dir.text())
        if directory: self.output_dir.setText(directory)

    def _prepare_models(self):
        self.details.append("\n생성형 모델 준비를 시작합니다. 최초 실행은 다운로드에 시간이 걸릴 수 있습니다.")
        threading.Thread(target=self._prepare_models_worker, daemon=True).start()

    def _prepare_models_worker(self):
        try:
            status = self.runtime.prepare_generation_models(self.progress_message.emit)
            self.model_status_done.emit(status)
        except Exception as exc:
            self.operation_failed.emit(str(exc))

    def _on_model_status(self, status):
        if status.get("ready"):
            self.model_status.setText(f"생성형 준비 완료 · CUDA · VRAM {status.get('vram_mb', 0)}MB")
        else:
            missing = []
            if not status.get("base_ready"): missing.append("SD1.5")
            if not status.get("adapter_ready"): missing.append("IP-Adapter")
            if not status.get("cuda"): missing.append("CUDA")
            self.model_status.setText("생성형 미준비 · " + ", ".join(missing))

    def _render(self):
        if not self.active_profile_id:
            QMessageBox.information(self, "시안 제작", "먼저 학습된 스타일을 선택해 주세요."); return
        if not self.production_paths:
            QMessageBox.information(self, "시안 제작", "제작용 사진을 먼저 추가해 주세요."); return
        self.details.append("\n시안을 렌더링하고 있습니다…")
        sketch = self.sketch_canvas.save_guidance() if self.sketch_canvas.has_ink() else ""
        memory_context = self.team_runtime.recall("mockup", self.instruction.toPlainText()).as_prompt() if self.team_runtime else ""
        args = (
            self.active_profile_id, list(self.production_paths),
            self.instruction.toPlainText(), self.visible_copy.text(),
            self.output_dir.text(), self.backend_selector.currentData(), [sketch] if sketch else [], memory_context,
        )
        threading.Thread(target=self._render_worker, args=args, daemon=True).start()

    def _render_worker(self, profile_id, production_paths, instruction, visible_copy, output_dir, backend,
                       guidance_paths, memory_context):
        try:
            result = self.runtime.render(
                profile_id, production_paths, instruction=instruction, output_dir=output_dir,
                visible_copy=visible_copy, backend=backend, preview_only=True,
                guidance_paths=guidance_paths, memory_context=memory_context,
            )
            self.render_done.emit(result)
        except Exception as exc: self.operation_failed.emit(str(exc))

    def _on_render_done(self, result):
        edit_serial = result.pop("_ai_edit_serial", None)
        if edit_serial is not None and int(edit_serial) != self._ai_edit_serial:
            return
        edit_base_output = result.pop("_ai_edit_base_output", None)
        if edit_base_output is not None:
            active = (self.preview_history[self.preview_index]
                      if 0 <= self.preview_index < len(self.preview_history) else {})
            if str(active.get("output", "")) != str(edit_base_output):
                self._ai_edit_in_progress = False
                self.ai_edit_button.setEnabled(True)
                self.save_preview_button.setEnabled(self.preview_index >= 0)
                self.details.append("\n현재 미리보기가 바뀌어 이전 화면을 기준으로 끝난 AI 수정 결과를 폐기했습니다.")
                return
        self._ai_edit_in_progress = False
        if hasattr(self, "ai_edit_button"): self.ai_edit_button.setEnabled(True)
        if result.get("already_satisfied"):
            self.preview_metadata = dict(result)
            if 0 <= self.preview_index < len(self.preview_history):
                self.preview_history[self.preview_index] = dict(result)
            self.details.append(
                "\n요청한 수정 사항은 현재 미리보기에 이미 적용되어 있습니다. "
                "이미지를 중복 생성하지 않고 현재 결과를 유지했습니다."
            )
            self.save_preview_button.setEnabled(True)
            return
        self._push_preview(result)

    def _push_preview(self, result):
        self.preview_history = self.preview_history[:self.preview_index + 1]
        self.preview_history.append(dict(result)); self.preview_index = len(self.preview_history) - 1
        self.preview_metadata = dict(result)
        pixmap = QPixmap(result["output"])
        if not pixmap.isNull():
            self.preview.set_preview(pixmap)
        fallback = (f"생성형 자동 대체 사유: {result['generation_fallback_reason']}\n"
                    if result.get("generation_fallback_reason") else "")
        applied = result.get("applied_edit_fields") or []
        edit_note = ("AI가 수정 명령을 구조화 패치로 변환하고 요청한 항목만 변경했습니다.\n"
                     if result.get("renderer") == "ai-scene-patch-v5" else
                     (f"적용된 수정 항목: {', '.join(applied)}\n" if applied else ""))
        self.details.append(
            f"\n임시 미리보기 생성 완료\n{result['width']}×{result['height']}\n"
            f"렌더러: {result.get('render_engine') or result['renderer']}\n{fallback}{edit_note}"
            "아직 최종 폴더에 저장되지 않았습니다. 결과를 확인한 뒤 저장 버튼을 눌러 주세요."
        )
        self.save_preview_button.setEnabled(True)
        self._update_history_buttons()
        if hasattr(self, "adjustment_sliders") and any(slider.value() != 50 for slider in self.adjustment_sliders.values()):
            self._adjustment_serial += 1; self.adjustment_timer.start()

    def _edit_with_ai(self):
        if self.preview_index < 0: QMessageBox.information(self, "AI 수정", "먼저 시안 미리보기를 만들어 주세요."); return
        instruction = self.edit_instruction.text().strip()
        if not instruction: QMessageBox.information(self, "AI 수정", "수정 지시를 입력해 주세요."); return
        self._ai_edit_in_progress = True
        self._ai_edit_serial += 1
        serial = self._ai_edit_serial
        base = deepcopy(self.preview_history[self.preview_index])
        base_output = str(base.get("output", ""))
        self.ai_edit_button.setEnabled(False)
        self.save_preview_button.setEnabled(False)
        self.details.append("\nAI가 현재 미리보기를 수정하고 있습니다…")
        sketch = self.sketch_canvas.save_guidance() if self.sketch_canvas.has_ink() else ""
        guidance = [*self.edit_attachment_paths, *([sketch] if sketch else [])]
        memory_context = self.team_runtime.recall("mockup", instruction).as_prompt() if self.team_runtime else ""
        threading.Thread(target=self._ai_edit_worker,
                         args=(base, instruction, serial, base_output, guidance, memory_context),
                         daemon=True).start()

    def _ai_edit_worker(self, metadata, instruction, serial, base_output, guidance_paths, memory_context):
        try:
            result = self.runtime.edit_preview(metadata, instruction, guidance_paths=guidance_paths,
                                               memory_context=memory_context)
            result["_ai_edit_serial"] = serial
            result["_ai_edit_base_output"] = base_output
            self.render_done.emit(result)
        except Exception as exc: self.operation_failed.emit(str(exc))

    def _manual_edit(self, operation, value=1.0):
        if self.preview_index < 0: return
        try:
            current = dict(self.preview_history[self.preview_index])
            result = self.runtime.transform_preview(current["output"], operation, value)
            result = {**current, **result, "adjustment_base": result["output"], "adjustments": {}}
            self._push_preview(result)
        except Exception as exc: self._on_failed(str(exc))

    def _schedule_live_adjustment(self, operation, value):
        self.adjustment_labels[operation].setText(f"{value}%")
        if self.preview_index >= 0:
            self._adjustment_serial += 1
            self.adjustment_timer.start()

    def _adjustment_values(self):
        return {name: slider.value() / 50.0 for name, slider in self.adjustment_sliders.items()}

    def _apply_live_adjustments(self):
        if self.preview_index < 0: return
        current = dict(self.preview_history[self.preview_index])
        base = current.get("adjustment_base") or current["output"]
        serial, values = self._adjustment_serial, self._adjustment_values()
        threading.Thread(target=self._adjustment_worker,
                         args=(base, values, current, serial), daemon=True).start()

    def _adjustment_worker(self, base, values, metadata, serial):
        try:
            adjusted = self.runtime.adjust_preview(base, values)
            self.adjustment_done.emit({**metadata, **adjusted, "_serial": serial})
        except Exception as exc: self.operation_failed.emit(str(exc))

    def _on_adjustment_done(self, result):
        if int(result.pop("_serial", -1)) != self._adjustment_serial: return
        current = self.preview_history[self.preview_index] if self.preview_index >= 0 else {}
        same_adjustment = (current.get("renderer") == "pillow-live-adjustment-v2"
                           and current.get("adjustment_base") == result.get("adjustment_base"))
        if same_adjustment:
            self.preview_history[self.preview_index] = dict(result)
        else:
            self.preview_history = self.preview_history[:self.preview_index + 1]
            self.preview_history.append(dict(result)); self.preview_index = len(self.preview_history) - 1
        self.preview_metadata = dict(result)
        pixmap = QPixmap(result["output"])
        if not pixmap.isNull():
            self.preview.set_preview(pixmap)
        self.save_preview_button.setEnabled(True); self._update_history_buttons()

    def _undo_preview(self):
        if self.preview_index > 0:
            self.preview_index -= 1; self._show_history_preview()

    def _redo_preview(self):
        if self.preview_index + 1 < len(self.preview_history):
            self.preview_index += 1; self._show_history_preview()

    def _show_history_preview(self):
        result = self.preview_history[self.preview_index]; self.preview_metadata = dict(result)
        pixmap = QPixmap(result["output"])
        self.preview.set_preview(pixmap)
        self._update_history_buttons()

    def _update_history_buttons(self):
        self.undo_button.setEnabled(self.preview_index > 0)
        self.redo_button.setEnabled(self.preview_index + 1 < len(self.preview_history))

    def _save_preview(self):
        if self.preview_index < 0: return
        default_dir = Path(self.output_dir.text()).expanduser()
        default_dir.mkdir(parents=True, exist_ok=True)
        filename, _ = QFileDialog.getSaveFileName(self, "시안 저장", str(default_dir / "mockup.png"), "PNG 이미지 (*.png)")
        if not filename: return
        try:
            current = dict(self.preview_history[self.preview_index])
            result = self.runtime.save_preview(current["output"], filename, current)
            self.details.append(f"\n최종 저장 완료: {result['output']}")
            if self.team_runtime:
                instruction = str(current.get("edit_instruction") or current.get("instruction") or "")
                self.team_runtime.remember_success("mockup", instruction=instruction, result=result, approved=True)
        except Exception as exc: self._on_failed(str(exc))

    def _on_failed(self, message: str):
        if getattr(self, "_ai_edit_in_progress", False):
            self._ai_edit_in_progress = False
            if hasattr(self, "ai_edit_button"): self.ai_edit_button.setEnabled(True)
            self.details.append("\n수정에 실패해 이전 미리보기를 그대로 유지했습니다. 저장하면 수정 전 결과가 저장됩니다.")
        self.save_preview_button.setEnabled(self.preview_index >= 0)
        self.details.append(f"\n오류: {message}")
        QMessageBox.warning(self, "시안 제작", message)

    def show_result(self, text: str):
        self.details.append(f"\n아니스 > {text}")
