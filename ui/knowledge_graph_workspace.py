"""Interactive, offline Knowledge Graph workspace backed by the Obsidian Vault."""
from __future__ import annotations
from .theme import set_widget_style, theme_manager, theme_color

import html
import math
import random
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QPointF, QRectF, QTimer
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
        self.edges = []
        self.dragging = False
        self.setPos(x, y); self.setZValue(2)
        self.setFlags(self.GraphicsItemFlag.ItemIsSelectable | self.GraphicsItemFlag.ItemIsMovable |
                      self.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setBrush(QBrush(QColor(NODE_COLORS.get(node.get("type"), "#7693aa"))))
        self.setPen(QPen(theme_color("border"), 2)); self.setToolTip(node.get("label", ""))
        label = QGraphicsSimpleTextItem(str(node.get("label", ""))[:26], self)
        label.setBrush(QBrush(theme_color("text"))); label.setPos(-label.boundingRect().width() / 2, size / 2 + 5)

    def mousePressEvent(self, event):
        self.dragging = True; self.owner.wake_simulation(0.55)
        self.owner.show_note(self.node["relative_path"])
        self.owner.highlight_neighborhood(self.node["id"])
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.dragging = False
        self.owner.velocities[self.node["id"]] = QPointF()
        self.owner.wake_simulation(0.72); super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.owner.open_note(self.node["relative_path"]); super().mouseDoubleClickEvent(event)

    def hoverEnterEvent(self, event):
        self.owner.highlight_neighborhood(self.node["id"]); super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if not self.isSelected(): self.owner.clear_highlight()
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == self.GraphicsItemChange.ItemPositionHasChanged:
            for edge in self.edges: edge.update_position()
        return super().itemChange(change, value)


class GraphEdgeItem(QGraphicsLineItem):
    def __init__(self, source: GraphNodeItem, target: GraphNodeItem):
        super().__init__(); self.source, self.target = source, target
        self.setPen(QPen(theme_color("border"), 1.15)); self.setZValue(0)
        source.edges.append(self); target.edges.append(self); self.update_position()

    def update_position(self):
        self.setLine(self.source.x(), self.source.y(), self.target.x(), self.target.y())

    def set_emphasis(self, active: bool, faded: bool = False):
        color = theme_color("accent" if active else "border")
        color.setAlpha(235 if active else 35 if faded else 175)
        self.setPen(QPen(color, 2.15 if active else 1.15))


class NativeGraphView(QGraphicsView):
    def __init__(self, owner):
        super().__init__(); self.owner = owner; self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QBrush(theme_color("window")))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.nodes, self.edges, self.velocities, self.adjacency = {}, [], {}, {}
        self.alpha = 0.0; self.motion_enabled = True; self._frame = 0
        self.physics_timer = QTimer(self); self.physics_timer.setInterval(24)
        self.physics_timer.timeout.connect(self._physics_step)
        theme_manager().changed.connect(self._apply_theme)

    def _apply_theme(self, _mode):
        self.setBackgroundBrush(QBrush(theme_color("window")))
        for item in self.scene().items():
            if isinstance(item, QGraphicsSimpleTextItem):
                item.setBrush(QBrush(theme_color("text")))
            elif hasattr(item, 'setDefaultTextColor'):
                item.setDefaultTextColor(theme_color("muted"))
            elif isinstance(item, GraphEdgeItem):
                pen = item.pen()
                pen.setColor(theme_color("border"))
                item.setPen(pen)
            elif isinstance(item, GraphNodeItem):
                pen = item.pen()
                pen.setColor(theme_color("accent" if item.isSelected() else "border"))
                item.setPen(pen)
        self.viewport().update()

    def show_note(self, relative_path: str):
        self.owner.show_note(relative_path)

    def open_note(self, relative_path: str):
        self.owner.open_note(relative_path)

    @staticmethod
    def _seeded_position(node_id: str, index: int, count: int) -> QPointF:
        seed = sum((offset + 1) * ord(char) for offset, char in enumerate(node_id))
        rng = random.Random(seed)
        golden = math.pi * (3 - math.sqrt(5))
        radius = 32 * math.sqrt(index + 1)
        angle = index * golden + rng.uniform(-.18, .18)
        return QPointF(math.cos(angle) * radius, math.sin(angle) * radius)

    def set_graph(self, payload: dict):
        self.physics_timer.stop()
        scene = self.scene(); scene.clear(); nodes = payload["nodes"]
        self.nodes, self.edges, self.velocities = {}, [], {}
        self.adjacency = {node["id"]: set() for node in nodes}
        if not nodes:
            text = scene.addText("표시할 기억이 없습니다. 필터를 조정해 보세요.")
            text.setDefaultTextColor(theme_color("muted")); return
        ordered = sorted(nodes, key=lambda n: (-n.get("degree", 0), -n.get("importance", 0)))
        for index, node in enumerate(ordered):
            position = self._seeded_position(node["id"], index, len(ordered))
            item = GraphNodeItem(node, self, position.x(), position.y())
            scene.addItem(item); self.nodes[node["id"]] = item
            self.velocities[node["id"]] = QPointF()
        for edge in payload["edges"]:
            source, target = self.nodes.get(edge["source"]), self.nodes.get(edge["target"])
            if source and target:
                line = GraphEdgeItem(source, target); scene.addItem(line); self.edges.append(line)
                self.adjacency[source.node["id"]].add(target.node["id"])
                self.adjacency[target.node["id"]].add(source.node["id"])
        scene.setSceneRect(QRectF(-1600, -1200, 3200, 2400))
        self.fitInView(scene.itemsBoundingRect().adjusted(-110, -110, 110, 110), Qt.AspectRatioMode.KeepAspectRatio)
        self.wake_simulation(1.0)

    def set_motion_enabled(self, enabled: bool):
        self.motion_enabled = bool(enabled)
        if enabled: self.wake_simulation(max(self.alpha, .45))
        else: self.physics_timer.stop()

    def wake_simulation(self, strength: float = .55):
        self.alpha = max(self.alpha, float(strength))
        if self.motion_enabled and self.nodes and not self.physics_timer.isActive(): self.physics_timer.start()

    def restart_layout(self):
        ordered = sorted(self.nodes.values(), key=lambda item: (-item.node.get("degree", 0), item.node["id"]))
        for index, item in enumerate(ordered):
            point = self._seeded_position(item.node["id"], index, len(ordered))
            item.setPos(point); self.velocities[item.node["id"]] = QPointF()
        self.wake_simulation(1.0)

    def _physics_step(self):
        if not self.motion_enabled or not self.nodes:
            self.physics_timer.stop(); return
        items = list(self.nodes.values()); forces = {item.node["id"]: QPointF() for item in items}
        alpha = self.alpha
        # Pairwise repulsion is bounded at 300 nodes and only runs while the layout is active.
        for left_index, left in enumerate(items):
            for right in items[left_index + 1:]:
                delta = left.pos() - right.pos(); distance_sq = max(80.0, delta.x() ** 2 + delta.y() ** 2)
                if distance_sq > 170000: continue
                distance = math.sqrt(distance_sq)
                magnitude = min(13.0, 5600.0 / distance_sq) * alpha
                push = QPointF(delta.x() / distance * magnitude, delta.y() / distance * magnitude)
                forces[left.node["id"]] += push; forces[right.node["id"]] -= push
        # Links behave like springs; highly connected notes settle closer together.
        for edge in self.edges:
            delta = edge.target.pos() - edge.source.pos(); distance = max(1.0, math.hypot(delta.x(), delta.y()))
            desired = 105.0 + 10.0 / max(1, min(edge.source.node.get("degree", 1), edge.target.node.get("degree", 1)))
            magnitude = (distance - desired) * .0068 * alpha
            spring = QPointF(delta.x() / distance * magnitude, delta.y() / distance * magnitude)
            forces[edge.source.node["id"]] += spring; forces[edge.target.node["id"]] -= spring
        for item in items:
            node_id = item.node["id"]
            if item.dragging: self.velocities[node_id] = QPointF(); continue
            position = item.pos()
            importance = float(item.node.get("importance", .5))
            forces[node_id] += QPointF(-position.x() * (.0007 + importance * .00015) * alpha,
                                       -position.y() * (.0007 + importance * .00015) * alpha)
            velocity = self.velocities[node_id] * .84 + forces[node_id]
            speed = math.hypot(velocity.x(), velocity.y())
            if speed > 18: velocity *= 18 / speed
            self.velocities[node_id] = velocity; item.setPos(position + velocity)
        self.alpha *= .982; self._frame += 1
        if self._frame % 12 == 0:
            bounds = self.scene().itemsBoundingRect().adjusted(-180, -180, 180, 180)
            self.scene().setSceneRect(self.scene().sceneRect().united(bounds))
        if self.alpha < .018 and not any(item.dragging for item in items): self.physics_timer.stop()

    def highlight_neighborhood(self, node_id: str):
        related = self.adjacency.get(node_id, set()) | {node_id}
        for current_id, item in self.nodes.items():
            active = current_id in related
            item.setOpacity(1.0 if active else .18)
            item.setPen(QPen(theme_color("accent" if current_id == node_id else "border"),
                             3 if current_id == node_id else 2))
        for edge in self.edges:
            active = edge.source.node["id"] == node_id or edge.target.node["id"] == node_id
            edge.set_emphasis(active, faded=not active)

    def clear_highlight(self):
        for item in self.nodes.values(): item.setOpacity(1.0); item.setPen(QPen(theme_color("border"), 2))
        for edge in self.edges: edge.set_emphasis(False)

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
        set_widget_style(self, STYLE)
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
        self.motion_toggle = QPushButton("움직임 일시정지"); self.motion_toggle.setCheckable(True)
        self.motion_toggle.toggled.connect(self._motion_toggled)
        relayout = QPushButton("다시 배치"); relayout.clicked.connect(lambda: self.graph_view.restart_layout())
        refresh = QPushButton("새로고침"); refresh.clicked.connect(self.refresh_graph)
        controls.insertWidget(0, self.search, 2); controls.addWidget(self.importance_label)
        controls.addWidget(self.importance); controls.addWidget(self.local_toggle)
        controls.addWidget(self.motion_toggle); controls.addWidget(relayout); controls.addWidget(refresh)
        outer.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        graph_frame = QFrame(); graph_frame.setObjectName("panel")
        graph_layout = QVBoxLayout(graph_frame); graph_layout.setContentsMargins(1, 1, 1, 1)
        self.graph_view = NativeGraphView(self); graph_layout.addWidget(self.graph_view)
        splitter.addWidget(graph_frame)

        side = QFrame(); side.setObjectName("panel"); side_layout = QVBoxLayout(side)
        title = QLabel("기억 미리보기"); title.setObjectName("section"); side_layout.addWidget(title)
        self.preview = QTextBrowser(); self.preview.setOpenExternalLinks(False)
        self.preview.setHtml("<p>노드를 선택하면 원문과 메타데이터가 표시됩니다.</p>")
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

    def _motion_toggled(self, paused: bool):
        self.motion_toggle.setText("움직임 재개" if paused else "움직임 일시정지")
        self.graph_view.set_motion_enabled(not paused)

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
            f"<h2>{html.escape(note['title'])}</h2><p>{html.escape(note['relative_path'])}</p>"
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
