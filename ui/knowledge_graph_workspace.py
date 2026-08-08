"""Interactive, offline Knowledge Graph workspace backed by the Obsidian Vault."""
from __future__ import annotations

import html
import math
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QTimer
from PyQt6.QtGui import QColor, QBrush, QPen, QPainter
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSlider, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
    QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsScene, QGraphicsSimpleTextItem,
    QGraphicsView,
)
from PyQt6.QtCore import Qt

from core.obsidian_vault import ObsidianVault, get_obsidian_vault
from ui.specialist_workspaces import STYLE

NODE_COLORS = {
    "preference": "#f59eeb", "project": "#5eead4", "task": "#fbbf66",
    "fact": "#67b9ff", "case": "#c4a7ff", "conversation_pattern": "#fb7185",
    "topics_map": "#45d5e8", "actions_map": "#7dd3fc", "index": "#91a4b7",
}


class GraphNodeItem(QGraphicsEllipseItem):
    def __init__(self, node: dict, owner, x: float, y: float):
        size = 24 + min(28, node.get("importance", .5) * 20 + node.get("degree", 0) * 1.4)
        super().__init__(-size / 2, -size / 2, size, size)
        self.node, self.owner = node, owner
        self.setPos(x, y); self.setZValue(2); self.setFlag(self.GraphicsItemFlag.ItemIsSelectable)
        self.setBrush(QBrush(QColor(NODE_COLORS.get(node.get("type"), "#7693aa"))))
        self.setPen(QPen(QColor("#142b3e"), 2)); self.setToolTip(node.get("label", ""))
        label = QGraphicsSimpleTextItem(str(node.get("label", ""))[:26], self)
        label.setBrush(QBrush(QColor("#dcecf7"))); label.setPos(-label.boundingRect().width() / 2, size / 2 + 5)

    def mousePressEvent(self, event):
        self.owner.show_note(self.node["relative_path"])
        self.setPen(QPen(QColor("#e9fbff"), 3)); super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.owner.open_note(self.node["relative_path"]); super().mouseDoubleClickEvent(event)


class NativeGraphView(QGraphicsView):
    def __init__(self, owner):
        super().__init__(); self.owner = owner; self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QBrush(QColor("#08101b")))
        self.setFrameShape(QFrame.Shape.NoFrame)

    def set_graph(self, payload: dict):
        scene = self.scene(); scene.clear(); nodes = payload["nodes"]
        if not nodes:
            text = scene.addText("표시할 기억이 없습니다. 필터를 조정해 보세요.")
            text.setDefaultTextColor(QColor("#7891a7")); return
        radius = max(180, min(1300, len(nodes) * 16))
        items = {}
        # Stable radial layout keeps important/high-degree memories near the center.
        ordered = sorted(nodes, key=lambda n: (-n.get("degree", 0), -n.get("importance", 0)))
        for index, node in enumerate(ordered):
            ring = int(math.sqrt(index)); ring_start = ring * ring
            count = max(1, (ring + 1) * (ring + 1) - ring_start)
            angle = 2 * math.pi * (index - ring_start) / count
            distance = 0 if index == 0 else 90 + ring * min(100, radius / max(1, math.sqrt(len(nodes))))
            item = GraphNodeItem(node, self.owner, math.cos(angle) * distance, math.sin(angle) * distance)
            scene.addItem(item); items[node["id"]] = item
        for edge in payload["edges"]:
            source, target = items.get(edge["source"]), items.get(edge["target"])
            if source and target:
                line = QGraphicsLineItem(source.x(), source.y(), target.x(), target.y())
                line.setPen(QPen(QColor("#29475c"), 1.2)); line.setZValue(0); scene.addItem(line)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-80, -80, 80, 80)); self.fitInView(scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


class KnowledgeGraphWindow(QMainWindow):
    """Independent graph UI; Obsidian remains an optional external editor."""

    def __init__(self, vault: ObsidianVault | None = None, parent=None):
        super().__init__(parent)
        self.vault = vault or get_obsidian_vault()
        self.current_note = ""
        self._facets_loaded = False
        self.setWindowTitle("JARVIS · Knowledge Graph")
        self.resize(1380, 820)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.setStyleSheet(STYLE)
        self._build()
        self._watch_vault()
        self.refresh_graph()

    def _build(self):
        root = QWidget(); outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 16); outer.setSpacing(10)
        header = QHBoxLayout()
        heading = QLabel("Knowledge Graph"); heading.setObjectName("heading")
        self.stats = QLabel(); self.stats.setObjectName("muted")
        header.addWidget(heading); header.addStretch(); header.addWidget(self.stats)
        outer.addLayout(header)

        controls = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("제목·본문·주제 검색")
        self.type_filter = QComboBox(); self.action_filter = QComboBox(); self.topic_filter = QComboBox()
        for combo, label in ((self.type_filter, "모든 유형"), (self.action_filter, "모든 행동"),
                             (self.topic_filter, "모든 주제")):
            combo.addItem(label, ""); controls.addWidget(combo)
        self.importance = QSlider(Qt.Orientation.Horizontal); self.importance.setRange(0, 100)
        self.importance.setValue(0); self.importance.setMaximumWidth(130)
        self.importance_label = QLabel("중요도 ≥ 0.0")
        self.local_toggle = QPushButton("전역 그래프"); self.local_toggle.setCheckable(True)
        refresh = QPushButton("새로고침"); refresh.clicked.connect(self.refresh_graph)
        controls.insertWidget(0, self.search, 2); controls.addWidget(self.importance_label)
        controls.addWidget(self.importance); controls.addWidget(self.local_toggle); controls.addWidget(refresh)
        outer.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        graph_frame = QFrame(); graph_frame.setObjectName("panel")
        graph_layout = QVBoxLayout(graph_frame); graph_layout.setContentsMargins(1, 1, 1, 1)
        self.graph_view = NativeGraphView(self); graph_layout.addWidget(self.graph_view)
        splitter.addWidget(graph_frame)

        side = QFrame(); side.setObjectName("panel"); side_layout = QVBoxLayout(side)
        title = QLabel("기억 미리보기"); title.setObjectName("section"); side_layout.addWidget(title)
        self.preview = QTextBrowser(); self.preview.setOpenExternalLinks(False)
        self.preview.setHtml("<p style='color:#7f96aa'>노드를 선택하면 원문과 메타데이터가 표시됩니다.</p>")
        side_layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        self.open_button = QPushButton("Obsidian에서 열기"); self.open_button.setEnabled(False)
        self.open_button.clicked.connect(lambda: self.open_note(self.current_note))
        sync = QPushButton("RAG 동기화"); sync.clicked.connect(self.sync_rag)
        lint = QPushButton("구조 검사"); lint.clicked.connect(self.run_lint)
        buttons.addWidget(self.open_button); buttons.addWidget(sync); buttons.addWidget(lint)
        side_layout.addLayout(buttons); splitter.addWidget(side); splitter.setSizes([1000, 360])
        outer.addWidget(splitter, 1); self.setCentralWidget(root)

        self.refresh_timer = QTimer(self); self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(450); self.refresh_timer.timeout.connect(self.refresh_graph)
        self.search.textChanged.connect(lambda: self.refresh_timer.start())
        for combo in (self.type_filter, self.action_filter, self.topic_filter):
            combo.currentIndexChanged.connect(lambda _index: self.refresh_timer.start())
        self.importance.valueChanged.connect(self._importance_changed)
        self.local_toggle.toggled.connect(self._local_changed)

    def _watch_vault(self):
        self.watcher = QFileSystemWatcher(self)
        directories = [str(path) for path in (self.vault.root / "wiki").rglob("*") if path.is_dir()]
        directories.append(str(self.vault.root / "wiki"))
        existing = [path for path in dict.fromkeys(directories) if Path(path).exists()]
        if existing: self.watcher.addPaths(existing)
        self.watcher.directoryChanged.connect(lambda _path: self.refresh_timer.start())

    def _importance_changed(self, value: int):
        self.importance_label.setText(f"중요도 ≥ {value / 100:.1f}"); self.refresh_timer.start()

    def _local_changed(self, enabled: bool):
        self.local_toggle.setText("선택 노드 주변" if enabled else "전역 그래프")
        self.refresh_graph()

    @staticmethod
    def _selected(combo: QComboBox) -> list[str]:
        value = combo.currentData(); return [str(value)] if value else []

    def _load_facets(self, facets: dict):
        if self._facets_loaded: return
        for combo, key in ((self.type_filter, "types"), (self.action_filter, "actions"),
                           (self.topic_filter, "topics")):
            for value in facets.get(key, []): combo.addItem(str(value), str(value))
        self._facets_loaded = True

    def refresh_graph(self):
        center = self.current_note if self.local_toggle.isChecked() and self.current_note else None
        payload = self.vault.build_graph(
            query=self.search.text(), types=self._selected(self.type_filter),
            actions=self._selected(self.action_filter), topics=self._selected(self.topic_filter),
            min_importance=self.importance.value() / 100, center=center, depth=2,
        )
        self._load_facets(payload["facets"])
        stats = payload["stats"]
        self.stats.setText(f"표시 {stats['visible_nodes']}개 · 연결 {stats['visible_edges']}개 · 전체 {stats['total_notes']}개")
        self.graph_view.set_graph(payload)

    def show_note(self, relative_path: str):
        try:
            note = self.vault.read_note(relative_path)
        except Exception as exc:
            QMessageBox.warning(self, "기억 미리보기", str(exc)); return
        self.current_note = note["relative_path"]; self.open_button.setEnabled(True)
        meta = note["metadata"]
        rows = "".join(f"<tr><td><b>{html.escape(str(key))}</b></td><td>{html.escape(str(value))}</td></tr>"
                       for key, value in meta.items())
        self.preview.setHtml(
            f"<h2>{html.escape(note['title'])}</h2><p style='color:#7f96aa'>{html.escape(note['relative_path'])}</p>"
            f"<table cellspacing='7'>{rows}</table><hr><pre style='white-space:pre-wrap'>{html.escape(note['body'])}</pre>"
        )
        if self.local_toggle.isChecked(): self.refresh_graph()

    def open_note(self, relative_path: str):
        if not relative_path: return
        try: self.vault.open_note(relative_path)
        except Exception as exc: QMessageBox.warning(self, "Obsidian 열기", str(exc))

    def sync_rag(self):
        try:
            result = self.vault.sync_to_rag()
            QMessageBox.information(self, "RAG 동기화", f"{result['indexed']}개 문서를 동기화했습니다.\n오류: {len(result['errors'])}개")
        except Exception as exc: QMessageBox.warning(self, "RAG 동기화", str(exc))

    def run_lint(self):
        result = self.vault.lint()
        QMessageBox.information(self, "Vault 구조 검사",
            f"문서 {result['notes']}개\n깨진 링크 {len(result['broken_links'])}개\n"
            f"고아 문서 {len(result['orphans'])}개\n얇은 문서 {len(result['thin_pages'])}개")
