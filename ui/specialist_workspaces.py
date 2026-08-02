"""Role-focused desktop workspaces layered over the shared Jarvis agent runtime."""
from __future__ import annotations

from pathlib import Path
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QPushButton, QSplitter, QTextEdit,
    QVBoxLayout, QWidget, QInputDialog, QMessageBox, QLineEdit, QComboBox,
)

from core.specialist_workspaces import SpecialistWorkspaceSpec
from core.mockup_design import MockupDesignRuntime


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
        else:
            status = QPushButton("Photoshop 연결 상태 확인")
            status.clicked.connect(lambda: self.prompt_submitted.emit("Photoshop 연결 상태를 확인해줘"))
            layout.addWidget(status)
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


class MockupWorkspaceWindow(QMainWindow):
    """Dedicated two-stage workspace: learn references, then render production assets."""
    prompt_submitted = pyqtSignal(str)
    analysis_done = pyqtSignal(object)
    render_done = pyqtSignal(object)
    model_status_done = pyqtSignal(object)
    progress_message = pyqtSignal(str)
    operation_failed = pyqtSignal(str)

    def __init__(self, spec: SpecialistWorkspaceSpec, parent=None, runtime=None):
        super().__init__(parent)
        self.spec = spec
        self.runtime = runtime or MockupDesignRuntime()
        self.reference_paths, self.production_paths = [], []
        self.active_profile_id = ""
        self.setWindowTitle("JARVIS · 시안 제작 전문가")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.resize(1320, 820)
        self.setStyleSheet(STYLE)
        self.analysis_done.connect(self._on_analysis_done)
        self.render_done.connect(self._on_render_done)
        self.operation_failed.connect(self._on_failed)
        self.model_status_done.connect(self._on_model_status)
        self.progress_message.connect(lambda message: self.details.append(f"\n{message}"))
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
        listing = QListWidget(); listing.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection); layout.addWidget(listing, 1)
        add = QPushButton("여러 장 추가"); remove = QPushButton("선택 제거")
        add.clicked.connect(lambda: self._add_images(learning)); remove.clicked.connect(lambda: self._remove_images(learning))
        buttons = QHBoxLayout(); buttons.addWidget(add); buttons.addWidget(remove); layout.addLayout(buttons)
        if learning:
            self.reference_list = listing
            self.profile_list = QListWidget(); self.profile_list.setMaximumHeight(150)
            self.profile_list.currentItemChanged.connect(self._select_profile)
            layout.addWidget(QLabel("저장된 스타일 프로필")); layout.addWidget(self.profile_list)
            analyze = QPushButton("참고 시안 분석·학습")
            analyze.clicked.connect(self._learn_style); layout.addWidget(analyze)
        else:
            self.production_list = listing
            self.instruction = QTextEdit(); self.instruction.setMaximumHeight(105)
            self.instruction.setPlaceholderText("시안 제목과 구체적인 제작 지시를 입력하세요. 첫 줄은 결과 이미지 제목으로 사용됩니다.")
            layout.addWidget(self.instruction)
            output_row = QHBoxLayout(); self.output_dir = QLineEdit(str(Path("data/mockup_outputs").resolve()))
            choose = QPushButton("출력 폴더"); choose.clicked.connect(self._choose_output_dir)
            output_row.addWidget(self.output_dir, 1); output_row.addWidget(choose); layout.addLayout(output_row)
            self.backend_selector = QComboBox()
            self.backend_selector.addItem("자동 · 생성형 우선", "auto")
            self.backend_selector.addItem("생성형 · SD1.5 + IP-Adapter Plus", "generative")
            self.backend_selector.addItem("빠른 로컬 합성", "local")
            layout.addWidget(self.backend_selector)
            model_row = QHBoxLayout()
            self.model_status = QLabel("생성형 모델 상태 확인 중…")
            self.model_status.setWordWrap(True); self.model_status.setObjectName("muted")
            prepare = QPushButton("생성형 모델 준비")
            prepare.clicked.connect(self._prepare_models)
            model_row.addWidget(self.model_status, 1); model_row.addWidget(prepare)
            layout.addLayout(model_row)
            render = QPushButton("학습 스타일로 시안 제작")
            render.clicked.connect(self._render); layout.addWidget(render)
        return panel

    def _preview_panel(self):
        panel = QFrame(); panel.setObjectName("panel"); layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("분석 및 결과 미리보기"))
        self.preview = QLabel("학습용 시안을 추가하고 분석을 시작하세요.")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview.setWordWrap(True)
        layout.addWidget(self.preview, 3)
        self.details = QTextEdit(); self.details.setReadOnly(True); layout.addWidget(self.details, 2)
        return panel

    def _add_images(self, learning: bool):
        files, _ = QFileDialog.getOpenFileNames(
            self, "학습용 시안 선택" if learning else "제작용 사진 선택", "",
            "이미지 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)",
        )
        paths, listing = ((self.reference_paths, self.reference_list) if learning
                          else (self.production_paths, self.production_list))
        for filename in files:
            resolved = str(Path(filename).resolve())
            if resolved not in paths:
                paths.append(resolved); item = QListWidgetItem(Path(resolved).name)
                item.setToolTip(resolved); listing.addItem(item)

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
            f"색상: {', '.join(profile.palette)}\n\nVision 분석\n{profile.vision_analysis}"
        )
        self.preview.setText("스타일 분석을 완료했습니다. 이제 제작용 사진을 추가해 시안을 만들 수 있습니다.")

    def _reload_profiles(self, selected_id=""):
        self.profile_list.clear()
        for profile in self.runtime.list_profiles():
            item = QListWidgetItem(f"{profile.name} · 참고 {len(profile.reference_paths)}장")
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_id); self.profile_list.addItem(item)
            if profile.profile_id == selected_id: self.profile_list.setCurrentItem(item)

    def _select_profile(self, current, _previous=None):
        if current: self.active_profile_id = current.data(Qt.ItemDataRole.UserRole)

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
        args = (
            self.active_profile_id, list(self.production_paths),
            self.instruction.toPlainText(), self.output_dir.text(), self.backend_selector.currentData(),
        )
        threading.Thread(target=self._render_worker, args=args, daemon=True).start()

    def _render_worker(self, profile_id, production_paths, instruction, output_dir, backend):
        try:
            result = self.runtime.render(
                profile_id, production_paths, instruction=instruction, output_dir=output_dir,
                backend=backend,
            )
            self.render_done.emit(result)
        except Exception as exc: self.operation_failed.emit(str(exc))

    def _on_render_done(self, result):
        pixmap = QPixmap(result["output"])
        if not pixmap.isNull():
            self.preview.setPixmap(pixmap.scaled(610, 510, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        fallback = (f"생성형 자동 대체 사유: {result['generation_fallback_reason']}\n"
                    if result.get("generation_fallback_reason") else "")
        self.details.append(
            f"\n생성 완료\n{result['output']}\n{result['width']}×{result['height']}\n"
            f"렌더러: {result['renderer']}\n{fallback}"
            "입력 해시와 스타일 프로필이 같은 이름의 JSON에 기록되었습니다."
        )

    def _on_failed(self, message: str):
        self.details.append(f"\n오류: {message}")
        QMessageBox.warning(self, "시안 제작", message)

    def show_result(self, text: str):
        self.details.append(f"\n아니스 > {text}")
