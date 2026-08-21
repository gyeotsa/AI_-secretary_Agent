"""Low-overhead 2.5D brain, agent and workspace navigator."""
from __future__ import annotations

from dataclasses import dataclass
import math
import random

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import QWidget


@dataclass(frozen=True)
class OrbitSurface:
    key: str
    label: str
    category: str = "workspace"


class BrainOrbitWidget(QWidget):
    """A truthful navigator: every orbit node is backed by a real UI surface."""

    surface_requested = pyqtSignal(str)
    camera_toggle_requested = pyqtSignal(bool)

    AGENTS = (
        ("ROUTER", "circle", QColor("#66e6ff"), -0.45, -0.18),
        ("MEMORY", "hex", QColor("#a88cff"), 0.42, -0.25),
        ("VISION", "diamond", QColor("#ffbb63"), -0.30, 0.35),
        ("MAKER", "square", QColor("#68e2ae"), 0.28, 0.34),
        ("VERIFY", "triangle", QColor("#ff718f"), 0.02, 0.02),
    )

    def __init__(self, parent=None, *, compact: bool = False):
        super().__init__(parent)
        self.compact = compact
        self.setMinimumHeight(210 if compact else 300)
        self.setMouseTracking(True)
        self.phase = 0.0
        self.orbit_angle = 0.0
        self.angular_velocity = 0.0018
        self.zoom = 1.0
        self.target_zoom = 1.0
        self.zoom_response = 0.13
        self._gesture_zoom_sample: tuple[float, float] | None = None
        self.runtime_state = "idle"
        self.camera_running = False
        self.camera_error = ""
        self._drag_origin: QPointF | None = None
        self._drag_angle = 0.0
        self._hit_regions: dict[str, QRectF] = {}
        self._surfaces: list[OrbitSurface] = []
        self._brain_points, self._brain_edges = self._build_brain_graph()
        self.set_surfaces([])
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(40)

    @staticmethod
    def _build_brain_graph():
        rng = random.Random(71345)
        points: list[tuple[float, float, float, int]] = []
        while len(points) < 92:
            x, y, z = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-0.65, 0.65))
            hemisphere = -1 if x < 0 else 1
            lobe_x = x + (0.28 if hemisphere < 0 else -0.28)
            if (lobe_x / 0.76) ** 2 + (y / 0.88) ** 2 + (z / 0.85) ** 2 <= 1:
                points.append((x, y, z, hemisphere))
        edges = []
        for index, point in enumerate(points):
            distances = sorted(
                ((sum((point[k] - other[k]) ** 2 for k in range(3)), other_index)
                 for other_index, other in enumerate(points) if other_index != index),
                key=lambda item: item[0],
            )[:2]
            for _, other_index in distances:
                if index < other_index:
                    edges.append((index, other_index))
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
        if self._gesture_zoom_sample is not None and timestamp > self._gesture_zoom_sample[1]:
            previous_signal, previous_time = self._gesture_zoom_sample
            zoom_speed = abs(zoom_signal - previous_signal) / max(1 / 120, timestamp - previous_time)
            self.zoom_response = max(0.08, min(0.58, 0.08 + zoom_speed * 0.12))
        self._gesture_zoom_sample = (zoom_signal, timestamp)
        self.target_zoom = 0.90 + zoom_signal * 0.40
        swipe = float(payload.get("swipe_velocity", 0.0))
        if abs(swipe) > 0.03:
            # Content moves opposite to the user's swipe, like a physical carousel.
            self.angular_velocity = max(-0.085, min(0.085, -swipe * 0.035))

    def _tick(self):
        self.phase = (self.phase + 0.023) % (math.pi * 2)
        self.orbit_angle = (self.orbit_angle + self.angular_velocity) % (math.pi * 2)
        self.angular_velocity = self.angular_velocity * 0.94 + 0.0018 * 0.06
        self.zoom += (self.target_zoom - self.zoom) * self.zoom_response
        self.zoom_response = self.zoom_response * 0.92 + 0.13 * 0.08
        self.update()

    def _brain_projection(self, center: QPointF, width: float, height: float):
        angle = self.phase * 0.18
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        projected = []
        for x, y, z, hemisphere in self._brain_points:
            rx = x * cos_a - z * sin_a
            depth = x * sin_a + z * cos_a
            projected.append((
                QPointF(center.x() + rx * width, center.y() + y * height),
                depth, hemisphere,
            ))
        return projected

    @staticmethod
    def _with_alpha(color: QColor, alpha: int) -> QColor:
        return QColor(color.red(), color.green(), color.blue(), max(0, min(255, alpha)))

    def _draw_brain(self, painter: QPainter, center: QPointF, scale: float):
        width, height = 168 * scale, 110 * scale
        pulse = 0.5 + 0.5 * math.sin(self.phase)
        gradient = QRadialGradient(center, width * 1.15)
        gradient.setColorAt(0, QColor(27, 108, 132, 92))
        gradient.setColorAt(0.56, QColor(80, 45, 122, 44))
        gradient.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, width * 1.22, height * 1.42)

        projected = self._brain_projection(center, width, height)
        active = self.runtime_state in {"processing", "executing", "responding", "running", "verifying"}
        for left, right in self._brain_edges:
            p1, d1, _ = projected[left]
            p2, d2, _ = projected[right]
            depth = (d1 + d2 + 1.3) / 2.6
            if active and (left + right) % 7 == int(self.phase * 2) % 7:
                color = QColor(255, 94 + int(70 * pulse), 132, 150)
            else:
                color = QColor(61, 177, 205, int(28 + 65 * depth))
            painter.setPen(QPen(color, 0.7 + depth * 0.8))
            painter.drawLine(p1, p2)
        for point, depth, hemisphere in sorted(projected, key=lambda item: item[1]):
            color = QColor("#70e8ff") if hemisphere < 0 else QColor("#aa83ff")
            alpha = int(80 + 120 * ((depth + 0.7) / 1.4))
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
        self._draw_orbit(painter, center, scale)
        self._draw_brain(painter, center, scale * self.zoom)

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
        painter.drawText(QRectF(self.width() - 210, 8, 198, 20),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         "OPEN HAND · ZOOM   /   SWIPE · ROTATE")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position()
            self._drag_angle = self.orbit_angle
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.position().x() - self._drag_origin.x()
            self.orbit_angle = self._drag_angle + delta / max(140.0, self.width() * 0.32)
            self.angular_velocity = max(-0.07, min(0.07, delta / max(1.0, self.width()) * 0.12))
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            start = self._drag_origin
            self._drag_origin = None
            if start is not None and (event.position() - start).manhattanLength() < 7:
                for key, region in reversed(tuple(self._hit_regions.items())):
                    if region.contains(event.position()):
                        if key == "__camera__":
                            self.camera_toggle_requested.emit(not self.camera_running)
                        else:
                            self.surface_requested.emit(key)
                        break
            event.accept()

    def wheelEvent(self, event):
        self.target_zoom = max(0.86, min(1.34, self.target_zoom + event.angleDelta().y() / 1800))
        event.accept()
