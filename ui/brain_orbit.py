"""Low-overhead 2.5D brain, agent and workspace navigator."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import random
import time

from PyQt6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QPointF,
    QRectF,
    QRunnable,
    QThreadPool,
    Qt,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import QWidget


@dataclass(frozen=True)
class OrbitSurface:
    key: str
    label: str
    category: str = "workspace"


@dataclass(frozen=True)
class SpatialKnowledgeNode:
    """One Vault note in the continuous 2.5D knowledge space.

    Coordinates are deterministic for a note id, so refreshing the Vault never
    makes the entire space jump.  User-facing metadata stays on the lightweight
    node instead of requiring a second QGraphicsView or a second graph runtime.
    """

    id: str
    label: str
    relative_path: str
    note_type: str
    action: str
    importance: float
    degree: float
    topics: tuple[str, ...]
    x: float
    y: float
    z: float


class _GraphLoadSignals(QObject):
    loaded = pyqtSignal(object, int)
    failed = pyqtSignal(str, int)


class _GraphLoadTask(QRunnable):
    """Run the potentially expensive Vault graph build outside the GUI thread."""

    def __init__(self, provider, generation: int):
        super().__init__()
        # Keep Qt's default auto-delete semantics.  The widget retains the
        # Python wrapper until the queued result is consumed; QThreadPool owns
        # the native QRunnable lifetime and releases it only after run() exits.
        # Manually disabling auto-delete caused intermittent native aborts when
        # several short-lived graph widgets were exercised in succession.
        self.provider = provider
        self.generation = generation
        self.signals = _GraphLoadSignals()

    @pyqtSlot()
    def run(self):
        try:
            payload = self.provider() or {"nodes": [], "edges": []}
            if not isinstance(payload, dict):
                raise TypeError("그래프 공급자가 dict를 반환하지 않았습니다.")
            self.signals.loaded.emit(payload, self.generation)
        except Exception as exc:  # pragma: no cover - exercised through the signal path
            self.signals.failed.emit(str(exc), self.generation)


class BrainOrbitWidget(QWidget):
    """A truthful navigator: every orbit node is backed by a real UI surface."""

    surface_requested = pyqtSignal(str)
    camera_toggle_requested = pyqtSignal(bool)
    graph_portal_changed = pyqtSignal(bool)
    graph_node_selected = pyqtSignal(object)
    graph_note_open_requested = pyqtSignal(str)

    PORTAL_ENTER_SIGNAL = .82
    PORTAL_ENTER_RESET_SIGNAL = .69
    PORTAL_EXIT_SIGNAL = .25
    PORTAL_EXIT_RESET_SIGNAL = .38
    PORTAL_ENTER_DWELL_SECONDS = .38
    PORTAL_EXIT_DWELL_SECONDS = .28
    PORTAL_MIN_ENTER_SAMPLES = 4
    PORTAL_MIN_EXIT_SAMPLES = 3
    GESTURE_STALE_SECONDS = .62
    EMBEDDED_GRAPH_MAX_NODES = 96

    GRAPH_NODE_COLORS = {
        "preference": QColor("#ffbb63"),
        "project": QColor("#66e6ff"),
        "decision": QColor("#ff718f"),
        "task": QColor("#68e2ae"),
        "conversation": QColor("#a88cff"),
        "fact": QColor("#76a9ff"),
        "default": QColor("#9cb5c9"),
    }

    AGENTS = (
        ("ROUTER", "circle", QColor("#66e6ff"), -0.45, -0.18),
        ("MEMORY", "hex", QColor("#a88cff"), 0.42, -0.25),
        ("VISION", "diamond", QColor("#ffbb63"), -0.30, 0.35),
        ("MAKER", "square", QColor("#68e2ae"), 0.28, 0.34),
        ("VERIFY", "triangle", QColor("#ff718f"), 0.02, 0.02),
    )

    def __init__(self, parent=None, *, compact: bool = False, graph_payload_provider=None):
        super().__init__(parent)
        self.compact = compact
        self.setMinimumHeight(210 if compact else 300)
        self.setMouseTracking(True)
        self.phase = 0.0
        self.orbit_angle = 0.0
        self.angular_velocity = 0.0018
        # ``phase`` is pulse-only.  Keeping camera orientation separate avoids
        # the old 65-degree snap caused by wrapping a phase-scaled angle.
        self.brain_yaw = 0.0
        self.brain_pitch = 0.0
        self.yaw_velocity = 0.0021
        self.pitch_velocity = 0.0
        self.camera_pan_x = 0.0
        self.camera_pan_y = 0.0
        self.pan_velocity_x = 0.0
        self.pan_velocity_y = 0.0
        self.zoom = 1.0
        self.target_zoom = 1.0
        self.zoom_response = 0.13
        self._gesture_zoom_sample: tuple[float, float] | None = None
        self.runtime_state = "idle"
        self.camera_running = False
        self.camera_error = ""
        self._drag_origin: QPointF | None = None
        self._drag_angle = 0.0
        self._drag_yaw = 0.0
        self._drag_pitch = 0.0
        self._drag_pan_x = 0.0
        self._drag_pan_y = 0.0
        self._hit_regions: dict[str, QRectF] = {}
        self._graph_hit_regions: dict[str, tuple[QRectF, float]] = {}
        self._surfaces: list[OrbitSurface] = []
        self.portal_progress = 0.0
        self.portal_mode = "brain"
        self._portal_requested = False
        self._portal_enabled = not compact
        self._portal_announced = False
        self._enter_candidate_at: float | None = None
        self._enter_candidate_samples = 0
        self._exit_candidate_at: float | None = None
        self._exit_candidate_samples = 0
        self._last_gesture_wall = 0.0
        self._manual_enter_at: float | None = None
        self._manual_exit_at: float | None = None
        self._manual_portal_active = False
        self._graph_payload_provider = graph_payload_provider
        self._graph_loaded = False
        self._graph_dirty = True
        self._graph_loading = False
        self._graph_loading_generation = 0
        self._graph_cache_payload: dict | None = None
        self._graph_cache_generation = 0
        self._graph_applied_generation = -1
        self._graph_reload_after_load = False
        self._graph_tasks: dict[int, _GraphLoadTask] = {}
        self._graph_error = ""
        self._selected_graph_note = ""
        self._selected_graph_node_id = ""
        self._hover_graph_node_id = ""
        self._spatial_graph_nodes: list[SpatialKnowledgeNode] = []
        self._spatial_graph_edges: list[tuple[int, int]] = []
        self._spatial_node_by_id: dict[str, SpatialKnowledgeNode] = {}
        self._spatial_adjacency: dict[str, set[str]] = {}
        self._vault_watcher: QFileSystemWatcher | None = None
        self._graph_reload_timer = QTimer(self)
        self._graph_reload_timer.setSingleShot(True)
        self._graph_reload_timer.setInterval(420)
        self._graph_reload_timer.timeout.connect(self._start_graph_load)
        # Defer visibility prefetch by one short UI beat.  This preserves the
        # asynchronous warm-up in the real application while avoiding a worker
        # being launched for windows that are constructed and immediately
        # closed (tests, transient previews, or rapid mode switches).
        self._graph_preload_timer = QTimer(self)
        self._graph_preload_timer.setSingleShot(True)
        self._graph_preload_timer.setInterval(80)
        self._graph_preload_timer.timeout.connect(self._start_graph_load)
        self._brain_points, self._brain_edges = self._build_brain_graph()
        self.set_surfaces([])
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

    @staticmethod
    def _build_brain_graph():
        """Build a deterministic, bilateral network with recognizable brain lobes.

        Points are generated in mirrored pairs.  That makes the silhouette stable
        instead of looking like a generic random cloud while retaining enough
        variation for the slow 2.5D rotation.
        """
        rng = random.Random(71345)
        points: list[tuple[float, float, float, int, str]] = []
        lobes = (
            ("frontal", 0.59, -0.25, 0.31, 0.34),
            ("parietal", 0.31, -0.43, 0.27, 0.27),
            ("temporal", 0.58, 0.25, 0.34, 0.30),
            ("occipital", 0.25, 0.11, 0.23, 0.33),
        )
        for lobe, cx, cy, rx, ry in lobes:
            for _ in range(28):
                angle = rng.random() * math.tau
                radius = math.sqrt(rng.random())
                x = min(0.94, max(0.07, cx + math.cos(angle) * rx * radius))
                y = cy + math.sin(angle) * ry * radius
                z = rng.uniform(-0.58, 0.58) * (0.72 + 0.28 * (1 - radius))
                # Append a left/right pair next to each other so symmetry can be
                # reasoned about and tested without relying on painter output.
                points.append((-x, y, z, -1, lobe))
                points.append((x, y, z, 1, lobe))

        edges: list[tuple[int, int]] = []
        for index, point in enumerate(points):
            distances = sorted(
                ((sum((point[k] - other[k]) ** 2 for k in range(3)), other_index)
                 for other_index, other in enumerate(points)
                 if other_index != index and other[3] == point[3]),
                key=lambda item: item[0],
            )[:3]
            for _, other_index in distances:
                if index < other_index:
                    edges.append((index, other_index))
        # Corpus-callosum-like bridges visually bind the two hemispheres without
        # destroying the central fissure.
        edges.extend((index, index + 1) for index in range(0, len(points), 14))
        return points, edges

    def set_surfaces(self, workspace_specs) -> None:
        surfaces = [
            OrbitSurface("command_center", "CONTROL", "system"),
            OrbitSurface("permissions", "ACCESS", "system"),
            OrbitSurface("sessions", "MEMORY", "system"),
            OrbitSurface("plugins", "TOOLS", "system"),
            OrbitSurface("voice", "VOICE", "system"),
        ]
        for spec in workspace_specs:
            surfaces.append(OrbitSurface(str(spec.key), str(spec.title).replace(" 전문가", "")))
        self._surfaces = surfaces
        self.update()

    def set_runtime_state(self, state) -> None:
        value = str(getattr(state, "name", state)).casefold()
        self.runtime_state = value
        self.update()

    def set_camera_status(self, status: dict) -> None:
        self.camera_running = bool(status.get("running"))
        self.camera_error = str(status.get("error") or "")
        self.setToolTip(self.camera_error or "카메라 영상은 로컬에서만 처리되며 저장되지 않습니다.")
        self.update()

    def apply_gesture_motion(self, payload: dict) -> None:
        zoom_signal = min(1.0, max(0.0, float(payload.get("zoom", 0.0))))
        timestamp = float(payload.get("timestamp", 0.0) or 0.0)
        tracking_state = str(payload.get("tracking_state", "tracking") or "tracking").casefold()
        hand_count = int(payload.get("hand_count", 1) or 0)
        gesture_hand_count = int(payload.get("gesture_hand_count", hand_count) or 0)
        swipe = float(payload.get("swipe_velocity", 0.0))
        swipe_phase = str(payload.get("swipe_phase", "idle") or "idle").casefold()
        gesture_mode_active = bool(payload.get("gesture_mode_active", True))
        zoom_active = bool(payload.get("zoom_active", False))
        if hand_count <= 0 or tracking_state in {"lost", "coasting", "missing", "none"}:
            self._reset_gesture_zoom_candidates()
            # A lost hand while entering must not complete an accidental dive.
            # Once inside the graph, hand loss keeps the current view stable.
            if self.portal_mode == "entering":
                self._request_portal(False)
                self.target_zoom = min(self.target_zoom, 1.18)
            return
        self._last_gesture_wall = time.monotonic()

        # One hand owns orientation/pan, two hands own depth.  Enforcing this at
        # the widget boundary keeps accidental open-palm scale estimates from
        # entering the brain even if a camera backend emits an ambiguous zoom.
        if gesture_hand_count == 1:
            self._reset_gesture_zoom_candidates()
            if self.portal_mode == "entering" and not self._manual_portal_active:
                self._request_portal(False)
                self.target_zoom = min(self.target_zoom, 1.18)
            vx = float(payload.get("velocity_x", swipe) or 0.0)
            vy = float(payload.get("velocity_y", 0.0) or 0.0)
            if abs(vx) > .012:
                self.yaw_velocity = max(-.095, min(.095, -vx * .040))
                self.angular_velocity = max(-.085, min(.085, -vx * .035))
            if abs(vy) > .012:
                self.pitch_velocity = max(-.070, min(.070, vy * .034))
            # Deep in the knowledge sphere, the same one-hand movement also
            # shifts the camera slightly.  It feels like travelling through a
            # 2.5D volume while orientation remains continuous.
            if self.portal_progress > .58:
                self.pan_velocity_x = max(-6.0, min(6.0, -vx * 3.8))
                self.pan_velocity_y = max(-5.0, min(5.0, vy * 3.2))
            return

        if (gesture_hand_count != 2 or not gesture_mode_active or not zoom_active
                or swipe_phase in {"candidate", "started", "updated"}):
            self._reset_gesture_zoom_candidates()
            if self.portal_mode == "entering" and not self._manual_portal_active:
                self._request_portal(False)
                self.target_zoom = min(self.target_zoom, 1.18)
            return
        sample_time = timestamp if timestamp > 0 else self._last_gesture_wall
        if self._gesture_zoom_sample is not None and timestamp > self._gesture_zoom_sample[1]:
            previous_signal, previous_time = self._gesture_zoom_sample
            zoom_speed = abs(zoom_signal - previous_signal) / max(1 / 120, timestamp - previous_time)
            self.zoom_response = max(0.08, min(0.58, 0.08 + zoom_speed * 0.12))
        self._gesture_zoom_sample = (zoom_signal, timestamp)
        self.target_zoom = (0.88 + zoom_signal * 1.16
                            if self._portal_enabled else 0.90 + zoom_signal * 0.40)
        if self._portal_enabled:
            if self.portal_mode in {"brain", "exiting"}:
                self._exit_candidate_at = None
                self._exit_candidate_samples = 0
                if zoom_signal >= self.PORTAL_ENTER_SIGNAL:
                    if self._enter_candidate_at is None:
                        self._enter_candidate_at = sample_time
                        self._enter_candidate_samples = 1
                    else:
                        self._enter_candidate_samples += 1
                    if (sample_time - self._enter_candidate_at >= self.PORTAL_ENTER_DWELL_SECONDS
                            and self._enter_candidate_samples >= self.PORTAL_MIN_ENTER_SAMPLES):
                        self._request_portal(True)
                elif zoom_signal <= self.PORTAL_ENTER_RESET_SIGNAL:
                    self._enter_candidate_at = None
                    self._enter_candidate_samples = 0
            elif self.portal_mode in {"graph", "entering"}:
                self._enter_candidate_at = None
                self._enter_candidate_samples = 0
                if zoom_signal <= self.PORTAL_EXIT_SIGNAL:
                    if self._exit_candidate_at is None:
                        self._exit_candidate_at = sample_time
                        self._exit_candidate_samples = 1
                    else:
                        self._exit_candidate_samples += 1
                    if (sample_time - self._exit_candidate_at >= self.PORTAL_EXIT_DWELL_SECONDS
                            and self._exit_candidate_samples >= self.PORTAL_MIN_EXIT_SAMPLES):
                        self._request_portal(False)
                elif zoom_signal >= self.PORTAL_EXIT_RESET_SIGNAL:
                    self._exit_candidate_at = None
                    self._exit_candidate_samples = 0

    def _reset_gesture_zoom_candidates(self) -> None:
        self._gesture_zoom_sample = None
        self._enter_candidate_at = None
        self._enter_candidate_samples = 0
        self._exit_candidate_at = None
        self._exit_candidate_samples = 0

    def _tick(self):
        self.phase = (self.phase + 0.023) % math.tau
        self.orbit_angle = (self.orbit_angle + self.angular_velocity) % math.tau
        self.brain_yaw = (self.brain_yaw + self.yaw_velocity) % math.tau
        self.brain_pitch = max(-1.08, min(1.08, self.brain_pitch + self.pitch_velocity))
        self.camera_pan_x = max(-self.width() * .24,
                                min(self.width() * .24, self.camera_pan_x + self.pan_velocity_x))
        self.camera_pan_y = max(-self.height() * .20,
                                min(self.height() * .20, self.camera_pan_y + self.pan_velocity_y))
        self.angular_velocity = self.angular_velocity * 0.94 + 0.0018 * 0.06
        self.yaw_velocity = self.yaw_velocity * .94 + .0021 * .06
        self.pitch_velocity *= .91
        self.pan_velocity_x *= .86
        self.pan_velocity_y *= .86
        self.zoom += (self.target_zoom - self.zoom) * self.zoom_response
        self.zoom_response = self.zoom_response * 0.92 + 0.13 * 0.08
        if self._portal_enabled:
            now = time.monotonic()
            if self._manual_enter_at is not None and now - self._manual_enter_at >= .34:
                self._manual_enter_at = None
                self._request_portal(True, manual=True)
            if self._manual_exit_at is not None and now - self._manual_exit_at >= .24:
                self._manual_exit_at = None
                self._request_portal(False)
            if (self.portal_mode == "entering" and self._last_gesture_wall
                    and now - self._last_gesture_wall > self.GESTURE_STALE_SECONDS
                    and self._manual_enter_at is None
                    and not self._manual_portal_active):
                self._request_portal(False)
                self.target_zoom = min(self.target_zoom, 1.18)
            graph_ready = self._graph_cache_payload is not None and not self._graph_dirty
            target = (1.0 if graph_ready else .92) if self._portal_requested else 0.0
            self.portal_progress += (target - self.portal_progress) * 0.105
            if abs(target - self.portal_progress) < 0.002:
                self.portal_progress = target
            self._sync_spatial_portal()
        self.update()

    def _set_portal_mode(self, mode: str) -> None:
        if mode not in {"brain", "entering", "graph", "exiting"}:
            raise ValueError(f"지원하지 않는 포털 상태입니다: {mode}")
        if self.portal_mode == mode:
            return
        self.portal_mode = mode
        if mode == "graph" and not self._portal_announced:
            self._portal_announced = True
            self.graph_portal_changed.emit(True)
        elif mode == "brain" and self._portal_announced:
            self._portal_announced = False
            self.graph_portal_changed.emit(False)

    def _request_portal(self, enter: bool, *, manual: bool = False) -> None:
        if not self._portal_enabled:
            return
        self._portal_requested = bool(enter)
        if enter:
            # A failed first load leaves a truthful error cache so the UI can
            # return to the brain instead of flying into an empty scene.  The
            # next explicit entry is also the user's retry request.
            if self._graph_error:
                self._graph_dirty = True
            self._manual_portal_active = bool(manual)
            self._set_portal_mode("entering")
            self._enter_candidate_at = None
            self._enter_candidate_samples = 0
            self._graph_preload_timer.stop()
            self._start_graph_load()
        else:
            self._manual_portal_active = False
            if self.portal_mode != "brain":
                self._set_portal_mode("exiting")
            self._exit_candidate_at = None
            self._exit_candidate_samples = 0

    def _brain_projection(self, center: QPointF, width: float, height: float):
        projected = []
        for x, y, z, hemisphere, lobe in self._brain_points:
            rx, ry, depth = self._rotate_xyz(x, y, z)
            perspective = max(.78, min(1.28, 1.0 + depth * .16))
            projected.append((
                QPointF(center.x() + rx * width * perspective,
                        center.y() + ry * height * perspective),
                depth, hemisphere, lobe,
            ))
        return projected

    def _rotate_xyz(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        """Rotate a point with persistent yaw and bounded pitch."""
        cos_yaw, sin_yaw = math.cos(self.brain_yaw), math.sin(self.brain_yaw)
        rx = x * cos_yaw - z * sin_yaw
        rz = x * sin_yaw + z * cos_yaw
        cos_pitch, sin_pitch = math.cos(self.brain_pitch), math.sin(self.brain_pitch)
        ry = y * cos_pitch - rz * sin_pitch
        depth = y * sin_pitch + rz * cos_pitch
        return rx, ry, depth

    @staticmethod
    def _smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    @staticmethod
    def _with_alpha(color: QColor, alpha: int) -> QColor:
        return QColor(color.red(), color.green(), color.blue(), max(0, min(255, alpha)))

    def _draw_brain(self, painter: QPainter, center: QPointF, scale: float):
        width, height = 168 * scale, 110 * scale
        pulse = 0.5 + 0.5 * math.sin(self.phase)
        # This is ambient illumination, not a filled anatomical silhouette.
        # The brain shape itself is made exclusively from nodes and links.
        gradient = QRadialGradient(center, width * 1.15)
        gradient.setColorAt(0, QColor(27, 108, 132, 52))
        gradient.setColorAt(0.56, QColor(80, 45, 122, 20))
        gradient.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, width * 1.22, height * 1.42)

        projected = self._brain_projection(center, width, height)
        active = self.runtime_state in {"processing", "executing", "responding", "running", "verifying"}
        for left, right in self._brain_edges:
            p1, d1, _, _ = projected[left]
            p2, d2, _, _ = projected[right]
            depth = (d1 + d2 + 1.3) / 2.6
            if active and (left + right) % 7 == int(self.phase * 2) % 7:
                color = QColor(255, 94 + int(70 * pulse), 132, 150)
            else:
                color = QColor(61, 177, 205, int(38 + 74 * max(0.0, (depth + .8) / 1.6)))
            painter.setPen(QPen(color, max(.45, 0.7 + depth * 0.8)))
            painter.drawLine(p1, p2)
        for point, depth, hemisphere, _lobe in sorted(projected, key=lambda item: item[1]):
            color = QColor("#70e8ff") if hemisphere < 0 else QColor("#aa83ff")
            alpha = int(72 + 150 * max(0.0, min(1.0, (depth + .75) / 1.5)))
            radius = max(1.2, (2.1 + depth * 1.3) * scale)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._with_alpha(color, alpha))
            painter.drawEllipse(point, radius, radius)

        for name, shape, color, x, y in self.AGENTS:
            point = QPointF(center.x() + x * width * 0.78, center.y() + y * height * 0.72)
            self._draw_shape(painter, point, 6.5 * scale, shape, color)
            if not self.compact:
                painter.setPen(self._with_alpha(color, 200))
                painter.setFont(QFont("Segoe UI", max(6, int(6.5 * scale)), 600))
                painter.drawText(QRectF(point.x() - 32, point.y() + 8, 64, 14),
                                 Qt.AlignmentFlag.AlignHCenter, name)

    def _draw_shape(self, painter, center, radius, shape, color):
        painter.setPen(QPen(self._with_alpha(color, 230), max(1.1, radius * 0.18)))
        painter.setBrush(self._with_alpha(color, 48))
        if shape == "circle":
            painter.drawEllipse(center, radius, radius)
        elif shape == "square":
            painter.drawRoundedRect(QRectF(center.x() - radius, center.y() - radius,
                                           radius * 2, radius * 2), 2, 2)
        else:
            sides = 4 if shape == "diamond" else 6 if shape == "hex" else 3
            offset = -math.pi / 2 + (math.pi / 4 if shape == "diamond" else 0)
            polygon = QPolygonF([
                QPointF(center.x() + math.cos(offset + i * math.tau / sides) * radius,
                        center.y() + math.sin(offset + i * math.tau / sides) * radius)
                for i in range(sides)
            ])
            painter.drawPolygon(polygon)

    def _draw_orbit(self, painter: QPainter, center: QPointF, scale: float):
        if not self._surfaces:
            return
        radius_x = min(self.width() * 0.41, 350) * self.zoom
        radius_y = min(self.height() * 0.31, 96) * self.zoom
        painter.setPen(QPen(QColor(87, 143, 166, 38), 1, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, radius_x, radius_y)
        entries = []
        for index, surface in enumerate(self._surfaces):
            angle = self.orbit_angle + index * math.tau / len(self._surfaces)
            depth = (math.sin(angle) + 1) / 2
            point = QPointF(center.x() + math.cos(angle) * radius_x,
                            center.y() + math.sin(angle) * radius_y)
            entries.append((depth, point, surface))
        self._hit_regions.clear()
        for depth, point, surface in sorted(entries, key=lambda item: item[0]):
            node_scale = (0.64 + depth * 0.54) * scale
            width, height = 78 * node_scale, 28 * node_scale
            rect = QRectF(point.x() - width / 2, point.y() - height / 2, width, height)
            color = QColor("#63dded") if surface.category == "system" else QColor("#a889ff")
            alpha = int(90 + depth * 150)
            painter.setPen(QPen(self._with_alpha(color, alpha), 1.2))
            painter.setBrush(QColor(8, 20, 34, int(115 + depth * 100)))
            painter.drawRoundedRect(rect, height / 2, height / 2)
            painter.setPen(self._with_alpha(QColor("#e9f8ff"), alpha))
            painter.setFont(QFont("Segoe UI", max(6, int(7 * node_scale)), 600))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, surface.label.upper())
            self._hit_regions[surface.key] = rect.adjusted(-5, -5, 5, 5)

    def _default_graph_payload(self) -> dict:
        from core.obsidian_vault import get_obsidian_vault
        # The full workspace can display more nodes.  The embedded portal keeps
        # a tighter bound so two animated canvases never compete for CPU/RAM.
        return get_obsidian_vault().build_graph(max_nodes=self.EMBEDDED_GRAPH_MAX_NODES)

    def _bounded_graph_payload(self, payload: dict) -> dict:
        nodes = list(payload.get("nodes") or [])
        ordered = sorted(
            (dict(node) for node in nodes if isinstance(node, dict) and node.get("id")),
            key=lambda node: (-float(node.get("degree", 0) or 0),
                              -float(node.get("importance", 0) or 0), str(node["id"])),
        )[:self.EMBEDDED_GRAPH_MAX_NODES]
        allowed = {str(node["id"]) for node in ordered}
        edges = [dict(edge) for edge in list(payload.get("edges") or [])
                 if isinstance(edge, dict)
                 and str(edge.get("source")) in allowed
                 and str(edge.get("target")) in allowed]
        return {"nodes": ordered, "edges": edges}

    def _start_graph_load(self) -> None:
        """Submit graph construction without doing Vault work on the GUI thread."""
        if not self._portal_enabled:
            return
        if self._graph_loading:
            if self._graph_dirty:
                self._graph_reload_after_load = True
            return
        if not self._graph_dirty and self._graph_cache_payload is not None:
            return
        self._graph_loading = True
        # Dirty now means "the source changed after this load began".  Existing
        # cached data may stay visible until the replacement arrives.
        self._graph_dirty = False
        self._graph_reload_after_load = False
        self._graph_loading_generation += 1
        generation = self._graph_loading_generation
        provider = self._graph_payload_provider or self._default_graph_payload
        task = _GraphLoadTask(provider, generation)
        task.signals.loaded.connect(self._on_graph_loaded)
        task.signals.failed.connect(self._on_graph_failed)
        self._graph_tasks[generation] = task
        QThreadPool.globalInstance().start(task)

    @pyqtSlot(object, int)
    def _on_graph_loaded(self, payload, generation: int) -> None:
        self._graph_tasks.pop(generation, None)
        if generation != self._graph_loading_generation:
            return
        self._graph_loading = False
        bounded = self._bounded_graph_payload(payload if isinstance(payload, dict) else {})
        self._graph_cache_payload = bounded
        self._graph_cache_generation = generation
        needs_reload = self._graph_reload_after_load
        self._graph_dirty = bool(needs_reload)
        self._graph_error = ""
        self._refresh_vault_watcher(bounded)
        if not needs_reload:
            self._apply_cached_graph()
        else:
            self._graph_reload_timer.start()
        self._sync_spatial_portal()
        self.update()

    @pyqtSlot(str, int)
    def _on_graph_failed(self, error: str, generation: int) -> None:
        self._graph_tasks.pop(generation, None)
        if generation != self._graph_loading_generation:
            return
        self._graph_loading = False
        self._graph_error = str(error)
        had_valid_graph = bool(self._spatial_graph_nodes and self._graph_cache_payload)
        needs_reload = self._graph_reload_after_load
        self._graph_dirty = bool(needs_reload)
        if had_valid_graph:
            # A refresh failure must not erase a graph that the user was
            # already navigating.  Keep the last verified payload visible and
            # surface the warning until a later explicit retry succeeds.
            self._graph_loaded = True
        else:
            self._graph_cache_payload = {"nodes": [], "edges": []}
            self._graph_cache_generation = generation
            self._graph_applied_generation = generation
            self._spatial_graph_nodes = []
            self._spatial_graph_edges = []
            self._spatial_node_by_id = {}
            self._spatial_adjacency = {}
            self._graph_loaded = False
            self._selected_graph_node_id = ""
            self._hover_graph_node_id = ""
            if self._portal_requested:
                self._request_portal(False)
                self.target_zoom = 1.0
        if needs_reload:
            self._graph_reload_timer.start()
        self._sync_spatial_portal()
        self.update()

    def _mark_graph_dirty(self, *_args) -> None:
        """Invalidate the prebuilt graph after a watched Vault mutation."""
        self._graph_dirty = True
        if self._graph_loading:
            self._graph_reload_after_load = True
            return
        if self.isVisible():
            self._graph_reload_timer.start()

    def _refresh_vault_watcher(self, payload: dict) -> None:
        if self._graph_payload_provider is not None:
            return
        try:
            from core.obsidian_vault import get_obsidian_vault
            vault = get_obsidian_vault()
            if self._vault_watcher is None:
                self._vault_watcher = QFileSystemWatcher(self)
                self._vault_watcher.directoryChanged.connect(self._mark_graph_dirty)
                self._vault_watcher.fileChanged.connect(self._mark_graph_dirty)
            current = set(self._vault_watcher.directories()) | set(self._vault_watcher.files())
            candidates = {vault.root, vault.root / "wiki", vault.root / "derived", vault.root / "prompts"}
            candidates.update(vault.root / folder for folder in vault.FOLDERS)
            # Watching displayed notes catches in-place edits; watching their
            # folders catches creates, deletes and renames without a recursive
            # scan on the UI thread.
            for node in payload.get("nodes", []):
                relative = str(node.get("relative_path") or "")
                if relative:
                    note = vault.root / relative
                    candidates.add(note)
                    candidates.add(note.parent)
            paths = [str(path) for path in candidates if path.exists() and str(path) not in current]
            if paths:
                self._vault_watcher.addPaths(paths)
        except Exception:
            # Graph access remains functional even when the configured Vault is
            # unavailable; the next explicit refresh can recover it.
            return

    def _apply_cached_graph(self) -> None:
        if self._graph_cache_payload is None or self._graph_dirty:
            return
        if self._graph_applied_generation == self._graph_cache_generation:
            return
        nodes: list[SpatialKnowledgeNode] = []
        by_id: dict[str, SpatialKnowledgeNode] = {}
        for raw in self._graph_cache_payload.get("nodes", []):
            node_id = str(raw.get("id") or "")
            if not node_id:
                continue
            # Stable hash coordinates keep individual memories in the same
            # place even when another note is inserted or the graph is sorted.
            vertical = self._stable_fraction(node_id, "vertical") * 2.0 - 1.0
            azimuth = self._stable_fraction(node_id, "azimuth") * math.tau
            shell = .56 + self._stable_fraction(node_id, "radius") * .37
            horizontal = math.sqrt(max(0.0, 1.0 - vertical * vertical))
            topics_value = raw.get("topics") or ()
            if isinstance(topics_value, str):
                topics = (topics_value,)
            else:
                topics = tuple(str(item) for item in topics_value if str(item).strip())
            node = SpatialKnowledgeNode(
                id=node_id,
                label=str(raw.get("label") or node_id),
                relative_path=str(raw.get("relative_path") or ""),
                note_type=str(raw.get("type") or "default").casefold(),
                action=str(raw.get("action") or ""),
                importance=max(0.0, min(1.0, float(raw.get("importance", .5) or .5))),
                degree=max(0.0, float(raw.get("degree", 0) or 0)),
                topics=topics,
                x=math.cos(azimuth) * horizontal * shell * 1.08,
                y=vertical * shell * .88,
                z=math.sin(azimuth) * horizontal * shell,
            )
            nodes.append(node)
            by_id[node_id] = node

        index_by_id = {node.id: index for index, node in enumerate(nodes)}
        edge_pairs: list[tuple[int, int]] = []
        adjacency = {node.id: set() for node in nodes}
        seen: set[tuple[int, int]] = set()
        for raw in self._graph_cache_payload.get("edges", []):
            source = str(raw.get("source") or "")
            target = str(raw.get("target") or "")
            if source not in index_by_id or target not in index_by_id or source == target:
                continue
            pair = tuple(sorted((index_by_id[source], index_by_id[target])))
            if pair in seen:
                continue
            seen.add(pair)
            edge_pairs.append(pair)
            adjacency[source].add(target)
            adjacency[target].add(source)

        self._spatial_graph_nodes = nodes
        self._spatial_graph_edges = edge_pairs
        self._spatial_node_by_id = by_id
        self._spatial_adjacency = adjacency
        if self._selected_graph_node_id not in by_id:
            self._selected_graph_node_id = ""
            for node in nodes:
                if node.relative_path == self._selected_graph_note:
                    self._selected_graph_node_id = node.id
                    break
        if self._hover_graph_node_id not in by_id:
            self._hover_graph_node_id = ""
        self._graph_applied_generation = self._graph_cache_generation
        self._graph_loaded = True

    @staticmethod
    def _stable_fraction(value: str, lane: str) -> float:
        digest = hashlib.sha256(f"{lane}\0{value}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") / float((1 << 64) - 1)

    def _sync_spatial_portal(self) -> None:
        if self._portal_requested and (self._graph_dirty or self._graph_cache_payload is None):
            self._start_graph_load()
        if self._graph_cache_payload is not None and not self._graph_dirty:
            self._apply_cached_graph()
        if not self._portal_requested and self.portal_progress <= .04:
            self._set_portal_mode("brain")
        if (self._portal_requested and self._graph_loaded
                and self.portal_progress >= .965 and self.portal_mode != "graph"):
            self._set_portal_mode("graph")
            self._manual_portal_active = False

    def _draw_knowledge_graph(
        self,
        painter: QPainter,
        center: QPointF,
        scale: float,
        opacity: float,
    ) -> None:
        """Draw the Vault graph in the same 2.5D coordinate system as the brain.

        The graph begins as a compact cluster behind the neural silhouette and
        expands continuously while the camera flies through it.  There is no
        second widget, screenshot or abrupt mode swap.
        """
        self._graph_hit_regions.clear()
        if not self._spatial_graph_nodes or opacity <= .005:
            return

        transition = self._smoothstep((self.portal_progress - .07) / .91)
        graph_center = QPointF(
            center.x() + self.camera_pan_x * transition,
            center.y() + self.camera_pan_y * transition,
        )
        extent = .16 + .84 * transition
        radius_x = min(self.width() * .445, 510.0) * extent * scale
        radius_y = min(self.height() * .415, 325.0) * extent * scale
        projected: list[tuple[QPointF, float, SpatialKnowledgeNode, float]] = []
        for node in self._spatial_graph_nodes:
            rx, ry, depth = self._rotate_xyz(node.x, node.y, node.z)
            perspective = max(.67, min(1.38, 1.0 + depth * .29))
            point = QPointF(
                graph_center.x() + rx * radius_x * perspective,
                graph_center.y() + ry * radius_y * perspective,
            )
            radius = (2.2 + node.importance * 3.7 + min(node.degree, 8.0) * .22)
            radius *= max(.70, min(1.32, perspective))
            item = (point, depth, node, radius)
            projected.append(item)

        selected = self._selected_graph_node_id
        selected_neighbours = self._spatial_adjacency.get(selected, set()) if selected else set()
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        for left, right in self._spatial_graph_edges:
            if left >= len(projected) or right >= len(projected):
                continue
            p1, d1, n1, _ = projected[left]
            p2, d2, n2, _ = projected[right]
            highlighted = bool(selected and (
                n1.id == selected or n2.id == selected
                or (n1.id in selected_neighbours and n2.id in selected_neighbours)
            ))
            depth = max(-1.0, min(1.0, (d1 + d2) / 2))
            if highlighted:
                edge_color = QColor(105, 226, 255, int(175 + 50 * (depth + 1) / 2))
                width = 1.55
            else:
                edge_color = QColor(75, 143, 176, int(34 + 70 * (depth + 1) / 2))
                width = .62 + max(0.0, depth) * .48
            painter.setPen(QPen(edge_color, width))
            painter.drawLine(p1, p2)

        # Label only the strongest memories plus the active node.  This keeps
        # the spatial topology legible while every node remains clickable.
        labelled = {
            node.id for node in sorted(
                self._spatial_graph_nodes,
                key=lambda item: (item.importance, item.degree, item.label),
                reverse=True,
            )[:7]
        }
        for point, depth, node, radius in sorted(projected, key=lambda item: item[1]):
            color = self.GRAPH_NODE_COLORS.get(node.note_type, self.GRAPH_NODE_COLORS["default"])
            is_selected = node.id == selected
            is_hovered = node.id == self._hover_graph_node_id
            connected = node.id in selected_neighbours
            front = max(0.0, min(1.0, (depth + 1.0) / 2.0))
            if selected and not (is_selected or is_hovered or connected):
                alpha = int(70 + front * 45)
            else:
                alpha = int(130 + front * 112)
            if is_selected or is_hovered:
                halo = radius + (7.5 if is_selected else 4.5)
                painter.setPen(QPen(self._with_alpha(color, 105), 1.2))
                painter.setBrush(self._with_alpha(color, 24))
                painter.drawEllipse(point, halo, halo)
            painter.setPen(QPen(self._with_alpha(QColor("#eafaff"), alpha), .72))
            painter.setBrush(self._with_alpha(color, alpha))
            painter.drawEllipse(point, radius, radius)
            if transition > .34:
                hit_radius = max(9.0, radius + 4.0)
                self._graph_hit_regions[node.id] = (
                    QRectF(point.x() - hit_radius, point.y() - hit_radius,
                           hit_radius * 2, hit_radius * 2),
                    depth,
                )
            if transition > .55 and (is_selected or is_hovered or node.id in labelled):
                label = node.label if len(node.label) <= 24 else node.label[:23] + "…"
                label_rect = QRectF(point.x() + radius + 4, point.y() - 8, 168, 18)
                painter.setPen(self._with_alpha(QColor("#e9f8ff"), 245 if is_selected else 185))
                painter.setFont(QFont("Segoe UI", 8 if is_selected else 7, 600))
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        painter.restore()

        if selected and transition > .40:
            self._draw_graph_detail_card(painter, self._spatial_node_by_id.get(selected), opacity)

    def _draw_graph_detail_card(
        self,
        painter: QPainter,
        node: SpatialKnowledgeNode | None,
        opacity: float,
    ) -> None:
        if node is None:
            return
        width = min(340.0, max(245.0, self.width() * .38))
        height = 108.0
        rect = QRectF(self.width() - width - 18, self.height() - height - 16, width, height)
        color = self.GRAPH_NODE_COLORS.get(node.note_type, self.GRAPH_NODE_COLORS["default"])
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        painter.setPen(QPen(self._with_alpha(color, 190), 1.15))
        painter.setBrush(QColor(5, 13, 24, 226))
        painter.drawRoundedRect(rect, 12, 12)
        painter.setPen(self._with_alpha(color, 235))
        painter.setFont(QFont("Segoe UI", 10, 700))
        painter.drawText(rect.adjusted(14, 9, -12, -78),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         node.label if len(node.label) <= 34 else node.label[:33] + "…")
        metadata = f"{node.note_type.upper()}  ·  중요도 {int(round(node.importance * 100))}%  ·  연결 {int(node.degree)}"
        painter.setPen(QColor(159, 190, 208, 220))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(rect.adjusted(14, 35, -12, -51), Qt.AlignmentFlag.AlignLeft, metadata)
        details = node.action.strip() or (" · ".join(node.topics[:3]) if node.topics else node.relative_path)
        if len(details) > 62:
            details = details[:61] + "…"
        painter.setPen(QColor(220, 236, 244, 220))
        painter.drawText(rect.adjusted(14, 57, -12, -28), Qt.AlignmentFlag.AlignLeft, details)
        painter.setPen(QColor(111, 150, 172, 205))
        painter.setFont(QFont("Segoe UI", 7))
        painter.drawText(rect.adjusted(14, 82, -12, -7), Qt.AlignmentFlag.AlignLeft,
                         "더블클릭하면 지식 그래프 작업공간에서 엽니다")
        painter.restore()

    def _graph_node_at(self, position: QPointF) -> str:
        candidates = [
            (depth, node_id)
            for node_id, (region, depth) in self._graph_hit_regions.items()
            if region.contains(position)
        ]
        return max(candidates, default=(-math.inf, ""))[1]

    def _select_graph_node(self, node_id: str) -> None:
        node = self._spatial_node_by_id.get(str(node_id or ""))
        if node is None:
            return
        self._selected_graph_node_id = node.id
        self._selected_graph_note = node.relative_path
        self.graph_node_selected.emit({
            "id": node.id,
            "label": node.label,
            "relative_path": node.relative_path,
            "type": node.note_type,
            "action": node.action,
            "importance": node.importance,
            "degree": node.degree,
            "topics": list(node.topics),
        })
        self.update()

    def _leave_graph(self) -> None:
        self._request_portal(False)
        self.target_zoom = 1.0

    def show_note(self, relative_path: str) -> None:
        """Select a Vault note inside the shared 2.5D canvas."""
        self._selected_graph_note = str(relative_path or "")
        for node in self._spatial_graph_nodes:
            if node.relative_path == self._selected_graph_note:
                self._select_graph_node(node.id)
                break
        self.update()

    def open_note(self, relative_path: str) -> None:
        # Reuse the full Knowledge Graph workspace on explicit double-click;
        # normal clicks remain local and do not spawn another window.
        self._selected_graph_note = str(relative_path or "")
        if self._selected_graph_note:
            self.graph_note_open_requested.emit(self._selected_graph_note)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.timer.isActive():
            self.timer.start(40)
        if self._portal_enabled:
            # Pre-cache shortly after visibility.  The delayed submission keeps
            # short-lived windows from leaving a QRunnable alive at shutdown;
            # the actual Vault build still never runs on the GUI thread.
            self._graph_preload_timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        self._graph_preload_timer.stop()
        self._graph_reload_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.timer.stop()
        self._graph_preload_timer.stop()
        self._graph_reload_timer.stop()
        # Ignore any queued result after close; the global pool itself must not
        # be synchronously drained from the UI thread.  If a load was in
        # flight, its result becomes stale when the generation advances.  Mark
        # the source dirty even when an older cache exists so reopening cannot
        # silently present that cache as the latest Vault state.
        refresh_interrupted = self._graph_loading or self._graph_reload_after_load
        self._graph_loading_generation += 1
        self._graph_loading = False
        self._graph_reload_after_load = False
        if refresh_interrupted or self._graph_cache_payload is None:
            self._graph_dirty = True
        super().closeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        bounds = self.rect()
        center = QPointF(bounds.center().x(), bounds.center().y() - (4 if self.compact else 8))
        scale = max(0.72, min(1.15, min(self.width() / 760, self.height() / 300)))
        painter.fillRect(bounds, QColor("#040911"))
        background = QRadialGradient(center, max(self.width(), self.height()) * 0.58)
        background.setColorAt(0, QColor(19, 42, 63, 150))
        background.setColorAt(0.62, QColor(10, 20, 35, 80))
        background.setColorAt(1, QColor(4, 9, 17, 0))
        painter.fillRect(bounds, background)
        portal = self._smoothstep(self.portal_progress)
        orbit_alpha = max(0.0, 1.0 - self._smoothstep(self.portal_progress / .58))
        self._hit_regions.clear()
        if orbit_alpha > .015:
            painter.save()
            painter.setOpacity(orbit_alpha)
            self._draw_orbit(painter, center, scale)
            painter.restore()

        # Vault nodes start as a distant cluster inside the neural silhouette,
        # then occupy the same scene as the camera crosses the brain surface.
        graph_alpha = self._smoothstep((self.portal_progress - .10) / .62)
        self._draw_knowledge_graph(painter, center, scale, graph_alpha)

        # Non-linear enlargement plus a fast neural fade makes the view pass
        # between the nodes instead of replacing one widget with another.
        dive_scale = 1.0 + (portal ** 1.58) * 5.1
        brain_alpha = max(0.0, 1.0 - self._smoothstep((self.portal_progress - .05) / .67))
        painter.save()
        painter.setOpacity(brain_alpha)
        self._draw_brain(painter, center, scale * self.zoom * dive_scale)
        painter.restore()
        if .06 < self.portal_progress < .72:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for index in range(4):
                radius = (28 + index * 42) * (1 + portal * 4.9)
                ring_alpha = int((1.0 - self._smoothstep(self.portal_progress / .75))
                                 * max(0, 82 - index * 13))
                painter.setPen(QPen(QColor(104, 220, 244, ring_alpha), 1.2))
                painter.drawEllipse(center, radius, radius * .64)

        if self._graph_error and self.portal_progress < .22:
            painter.setPen(QColor(255, 117, 138, 225))
            painter.setFont(QFont("Segoe UI", 8, 600))
            painter.drawText(
                QRectF(18, self.height() - 44, self.width() - 36, 20),
                Qt.AlignmentFlag.AlignCenter,
                "지식 공간을 불러오지 못했습니다 · 다시 확대하면 재시도합니다",
            )
        elif (self._graph_loaded and not self._spatial_graph_nodes
              and self.portal_progress > .72):
            painter.setPen(QColor(151, 184, 202, 215))
            painter.setFont(QFont("Segoe UI", 9, 600))
            painter.drawText(
                QRectF(18, center.y() - 18, self.width() - 36, 36),
                Qt.AlignmentFlag.AlignCenter,
                "아직 연결된 지식 노드가 없습니다",
            )

        camera_color = QColor("#65e0a1") if self.camera_running else QColor("#8293a3")
        if self.camera_error:
            camera_color = QColor("#ff758a")
        painter.setPen(camera_color)
        painter.setFont(QFont("Consolas", 8, 600))
        status = "CAM LOCAL ON" if self.camera_running else "CAM OFF"
        if self.camera_error:
            status = "CAM BLOCKED / ERROR"
        status_rect = QRectF(12, 9, 125, 20)
        painter.drawText(status_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, status)
        self._hit_regions["__camera__"] = status_rect.adjusted(-5, -4, 5, 4)
        painter.setPen(QColor(105, 139, 162, 150))
        painter.setFont(QFont("Segoe UI", 7))
        painter.drawText(QRectF(self.width() - 350, 8, 338, 20),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         "ONE HAND · ROTATE / PAN    TWO HANDS · ENTER BRAIN")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position()
            self._drag_angle = self.orbit_angle
            self._drag_yaw = self.brain_yaw
            self._drag_pitch = self.brain_pitch
            self._drag_pan_x = self.camera_pan_x
            self._drag_pan_y = self.camera_pan_y
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta_x = event.position().x() - self._drag_origin.x()
            delta_y = event.position().y() - self._drag_origin.y()
            pan_only = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if pan_only and self.portal_progress > .30:
                self.camera_pan_x = max(-self.width() * .24,
                                        min(self.width() * .24, self._drag_pan_x + delta_x))
                self.camera_pan_y = max(-self.height() * .20,
                                        min(self.height() * .20, self._drag_pan_y + delta_y))
            else:
                self.orbit_angle = (self._drag_angle
                                    + delta_x / max(140.0, self.width() * .32)) % math.tau
                self.brain_yaw = (self._drag_yaw
                                  + delta_x / max(95.0, self.width() * .17)) % math.tau
                self.brain_pitch = max(-1.08, min(
                    1.08, self._drag_pitch + delta_y / max(105.0, self.height() * .42),
                ))
                self.angular_velocity = max(
                    -.07, min(.07, delta_x / max(1.0, self.width()) * .12),
                )
                self.yaw_velocity = max(
                    -.095, min(.095, delta_x / max(1.0, self.width()) * .16),
                )
                if self.portal_progress > .72:
                    self.camera_pan_x = max(-self.width() * .24, min(
                        self.width() * .24, self._drag_pan_x + delta_x * .12,
                    ))
                    self.camera_pan_y = max(-self.height() * .20, min(
                        self.height() * .20, self._drag_pan_y + delta_y * .12,
                    ))
            self.update()
            event.accept()
            return
        node_id = self._graph_node_at(event.position())
        if node_id != self._hover_graph_node_id:
            self._hover_graph_node_id = node_id
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if node_id else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            start = self._drag_origin
            self._drag_origin = None
            if start is not None and (event.position() - start).manhattanLength() < 7:
                graph_node_id = self._graph_node_at(event.position())
                if graph_node_id:
                    self._select_graph_node(graph_node_id)
                    event.accept()
                    return
                for key, region in reversed(tuple(self._hit_regions.items())):
                    if region.contains(event.position()):
                        if key == "__camera__":
                            self.camera_toggle_requested.emit(not self.camera_running)
                        else:
                            self.surface_requested.emit(key)
                        break
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            node_id = self._graph_node_at(event.position())
            node = self._spatial_node_by_id.get(node_id)
            if node is not None:
                self._select_graph_node(node.id)
                self.open_note(node.relative_path)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        upper = 2.04 if self._portal_enabled else 1.34
        self.target_zoom = max(0.86, min(upper, self.target_zoom + event.angleDelta().y() / 1150))
        if self._portal_enabled:
            now = time.monotonic()
            if self.target_zoom >= 1.78 and self.portal_mode in {"brain", "exiting"}:
                if self._manual_enter_at is None:
                    self._manual_enter_at = now
                self._manual_exit_at = None
            elif self.target_zoom <= 1.18 and self.portal_mode in {"graph", "entering"}:
                if self._manual_exit_at is None:
                    self._manual_exit_at = now
                self._manual_enter_at = None
            elif 1.28 <= self.target_zoom <= 1.64:
                self._manual_enter_at = None
                self._manual_exit_at = None
        event.accept()
