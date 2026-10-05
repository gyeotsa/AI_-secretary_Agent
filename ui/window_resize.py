"""Native edge resizing, with a deterministic fallback for non-native Qt platforms."""
from PyQt6.QtCore import QObject, QEvent, Qt, QRect
from PyQt6.QtWidgets import QWidget


def resized_geometry(original, delta, edges, minimum, maximum):
    rect = QRect(original)
    if edges & Qt.Edge.LeftEdge:
        width = max(minimum.width(), min(maximum.width(), original.width() - delta.x()))
        rect.setLeft(original.right() - width + 1)
    elif edges & Qt.Edge.RightEdge:
        rect.setWidth(max(minimum.width(), min(maximum.width(), original.width() + delta.x())))
    if edges & Qt.Edge.TopEdge:
        height = max(minimum.height(), min(maximum.height(), original.height() - delta.y()))
        rect.setTop(original.bottom() - height + 1)
    elif edges & Qt.Edge.BottomEdge:
        rect.setHeight(max(minimum.height(), min(maximum.height(), original.height() + delta.y())))
    return rect


class ResizeHandle(QWidget):
    def __init__(self, window, edges, cursor):
        super().__init__(window)
        self.edges = edges
        self.drag_origin = None
        self.setCursor(cursor)
        self.setObjectName('windowResizeHandle')
        self.setStyleSheet('background: transparent; border: 0;')
        self.setAccessibleName('창 크기 조절')

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton: return
        window = self.window()
        if window.isMaximized() or getattr(window, 'window_mode', '') == 'mini': return
        handle = window.windowHandle()
        if handle and handle.startSystemResize(self.edges):
            event.accept()
            return
        self.drag_origin = event.globalPosition().toPoint()
        self.original_geometry = window.geometry()
        self.grabMouse()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_origin is None: return
        window = self.window()
        minimum = window.minimumSize().expandedTo(window.minimumSizeHint())
        window.setGeometry(resized_geometry(self.original_geometry,
            event.globalPosition().toPoint() - self.drag_origin, self.edges,
            minimum, window.maximumSize()))
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.drag_origin is not None:
            self.drag_origin = None
            self.releaseMouse()
            self.window().normal_geometry = self.window().geometry()
        event.accept()


class WindowResizeController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        E, C = Qt.Edge, Qt.CursorShape
        specs = [(E.LeftEdge, C.SizeHorCursor), (E.RightEdge, C.SizeHorCursor),
                 (E.TopEdge, C.SizeVerCursor), (E.BottomEdge, C.SizeVerCursor),
                 (E.LeftEdge | E.TopEdge, C.SizeFDiagCursor),
                 (E.RightEdge | E.TopEdge, C.SizeBDiagCursor),
                 (E.LeftEdge | E.BottomEdge, C.SizeBDiagCursor),
                 (E.RightEdge | E.BottomEdge, C.SizeFDiagCursor)]
        self.handles = [ResizeHandle(window, edge, cursor) for edge, cursor in specs]
        window.installEventFilter(self)
        self.update_handles()

    def update_handles(self):
        w, h, edge, corner = self.window.width(), self.window.height(), 6, 14
        rects = [(0,corner,edge,h-2*corner),(w-edge,corner,edge,h-2*corner),
                 (corner,0,w-2*corner,edge),(corner,h-edge,w-2*corner,edge),
                 (0,0,corner,corner),(w-corner,0,corner,corner),
                 (0,h-corner,corner,corner),(w-corner,h-corner,corner,corner)]
        enabled = not (self.window.isMaximized() or self.window.isFullScreen()
                       or getattr(self.window, 'window_mode', '') == 'mini')
        for handle, rect in zip(self.handles, rects):
            handle.setGeometry(*rect)
            handle.setVisible(enabled)
            handle.raise_()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.WindowStateChange):
            self.update_handles()
        return False
