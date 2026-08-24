from __future__ import annotations

import math
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QApplication


_APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _drain_graph_workers_between_tests():
    """Keep queued Qt signals from one async graph test out of the next one."""
    yield
    QThreadPool.globalInstance().waitForDone(3000)
    _APP.processEvents()


def _graph_payload(count: int = 2):
    nodes = [
        {
            "id": f"memory/node-{index}",
            "relative_path": f"wiki/node-{index}.md",
            "label": f"기억 {index}",
            "type": "preference" if index % 2 else "project",
            "importance": .9 - min(index, 8) * .02,
            "degree": 2 if count > 2 else 1,
        }
        for index in range(count)
    ]
    return {
        "nodes": nodes,
        "edges": [
            {"source": nodes[index]["id"], "target": nodes[index + 1]["id"]}
            for index in range(max(0, count - 1))
        ],
    }


def _pump_until(app, predicate, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    app.processEvents()
    return bool(predicate())


def _hold_open(widget, start: float = 1.0):
    for offset in (0.0, .14, .28, .42):
        widget.apply_gesture_motion({
            "zoom": 1.0,
            "swipe_velocity": 0.0,
            "timestamp": start + offset,
            "hand_count": 2,
            "gesture_hand_count": 2,
            "tracking_state": "tracking",
            "zoom_active": True,
            "gesture_mode_active": True,
        })


def _hold_closed(widget, start: float = 2.0):
    for offset in (0.0, .15, .30):
        widget.apply_gesture_motion({
            "zoom": 0.0,
            "swipe_velocity": 0.0,
            "timestamp": start + offset,
            "hand_count": 2,
            "gesture_hand_count": 2,
            "tracking_state": "tracking",
            "zoom_active": True,
            "gesture_mode_active": True,
        })


def test_brain_network_is_dense_bilateral_and_lobed(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    try:
        assert len(widget._brain_points) >= 220
        assert len(widget._brain_edges) >= 250
        assert {point[4] for point in widget._brain_points} == {
            "frontal", "parietal", "temporal", "occipital",
        }
        for left, right in zip(widget._brain_points[::2], widget._brain_points[1::2]):
            assert left[0] == -right[0]
            assert left[1:3] == right[1:3]
            assert (left[3], right[3]) == (-1, 1)
            assert left[4] == right[4]
    finally:
        widget.close()
        app.processEvents()


def test_graph_prefetch_is_async_dirty_reloadable_and_bounded(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    gui_thread = threading.get_ident()
    provider_threads = []

    def provider():
        provider_threads.append(threading.get_ident())
        time.sleep(.07)
        return _graph_payload(130)

    widget = BrainOrbitWidget(graph_payload_provider=provider)
    started = time.perf_counter()
    widget.show()
    show_elapsed = time.perf_counter() - started
    try:
        assert show_elapsed < .05
        assert _pump_until(app, lambda: widget._graph_cache_payload is not None)
        assert provider_threads and provider_threads[0] != gui_thread
        assert len(widget._graph_cache_payload["nodes"]) == widget.EMBEDDED_GRAPH_MAX_NODES

        widget._mark_graph_dirty("simulated-vault-change")
        assert _pump_until(
            app,
            lambda: len(provider_threads) >= 2 and not widget._graph_dirty and not widget._graph_loading,
        )
        assert provider_threads[1] != gui_thread
    finally:
        widget.close()
        app.processEvents()


def test_portal_requires_dwell_survives_jitter_and_cancels_on_hand_loss(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.show()
    try:
        for timestamp, zoom in ((1.0, .9), (1.08, .68), (1.16, .84), (1.23, .68)):
            widget.apply_gesture_motion({
                "zoom": zoom, "timestamp": timestamp,
                "gesture_hand_count": 2, "tracking_state": "tracking",
                "swipe_velocity": 0.0, "zoom_active": True,
                "gesture_mode_active": True, "hand_count": 2,
            })
        assert widget.portal_mode == "brain"
        assert widget._portal_requested is False

        _hold_open(widget, 2.0)
        assert widget.portal_mode == "entering"
        assert widget._portal_requested is True
        widget.apply_gesture_motion({
            "zoom": 1.0, "timestamp": 2.5, "hand_count": 0,
            "tracking_state": "lost", "swipe_velocity": 0.0,
        })
        assert widget.portal_mode == "exiting"
        assert widget._portal_requested is False
        for _ in range(70):
            widget._tick()
        assert widget.portal_mode == "brain"
    finally:
        widget.close()
        app.processEvents()


def test_swipe_rotation_never_accumulates_brain_portal_zoom(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.show()
    try:
        for offset in (0.0, .14, .28, .42, .56):
            widget.apply_gesture_motion({
                "zoom": .96,
                "swipe_velocity": 1.2,
                "swipe_phase": "updated" if offset else "started",
                "zoom_active": False,
                "gesture_mode_active": True,
                "timestamp": 1.0 + offset,
                "hand_count": 1,
                "gesture_hand_count": 1,
                "tracking_state": "tracking",
            })
        assert widget.portal_mode == "brain"
        assert widget._portal_requested is False
        assert widget._enter_candidate_samples == 0
        assert widget.angular_velocity < 0

        _hold_open(widget, 2.0)
        assert widget.portal_mode == "entering"
    finally:
        widget.close()
        app.processEvents()


def test_candidate_swipe_never_accumulates_brain_portal_zoom(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.show()
    try:
        for offset in (0.0, .14, .28, .42, .56):
            widget.apply_gesture_motion({
                "zoom": .98,
                "swipe_velocity": 0.0,
                "swipe_phase": "candidate",
                "zoom_active": True,
                "gesture_mode_active": True,
                "timestamp": 1.0 + offset,
                "hand_count": 1,
                "gesture_hand_count": 1,
                "tracking_state": "tracking",
            })
        assert widget.portal_mode == "brain"
        assert widget._portal_requested is False
        assert widget._enter_candidate_samples == 0

        _hold_open(widget, 2.0)
        assert widget.portal_mode == "entering"
    finally:
        widget.close()
        app.processEvents()


def test_swipe_claim_cancels_an_in_progress_gesture_portal_entry(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.show()
    try:
        _hold_open(widget, 1.0)
        assert widget.portal_mode == "entering"
        assert widget._portal_requested is True

        widget.apply_gesture_motion({
            "zoom": .96,
            "swipe_velocity": 1.1,
            "swipe_phase": "started",
            "zoom_active": False,
            "gesture_mode_active": True,
            "timestamp": 1.7,
            "hand_count": 1,
            "gesture_hand_count": 1,
            "tracking_state": "tracking",
        })

        assert widget.portal_mode == "exiting"
        assert widget._portal_requested is False
        assert widget.target_zoom <= 1.18
        for _ in range(80):
            widget._tick()
        assert widget.portal_mode == "brain"
        assert widget.portal_progress == pytest.approx(0.0)
    finally:
        widget.close()
        app.processEvents()


def test_close_during_graph_load_can_reopen_and_reload(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    calls = []

    def provider():
        calls.append(time.monotonic())
        time.sleep(.12)
        return _graph_payload()

    widget = BrainOrbitWidget(graph_payload_provider=provider)
    widget.show()
    try:
        assert _pump_until(app, lambda: len(calls) == 1 and widget._graph_loading)
        widget.close()
        app.processEvents()
        assert widget._graph_loading is False

        widget.show()
        assert _pump_until(
            app,
            lambda: len(calls) >= 2 and widget._graph_cache_payload is not None
            and not widget._graph_loading,
        )
        assert widget._graph_loaded
    finally:
        widget.close()
        app.processEvents()


def test_close_during_cached_refresh_reloads_new_payload_after_reopen(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    calls = []
    release_refresh = threading.Event()

    def provider():
        call_number = len(calls) + 1
        calls.append(call_number)
        if call_number == 2:
            release_refresh.wait(.4)
        return _graph_payload(call_number)

    widget = BrainOrbitWidget(graph_payload_provider=provider)
    widget.show()
    try:
        assert _pump_until(app, lambda: widget._graph_cache_payload is not None)
        assert len(widget._graph_cache_payload["nodes"]) == 1

        widget._mark_graph_dirty("simulated-vault-change")
        widget._start_graph_load()
        assert _pump_until(app, lambda: calls == [1, 2] and widget._graph_loading)
        widget.close()
        app.processEvents()
        assert widget._graph_dirty is True

        release_refresh.set()
        widget.show()
        assert _pump_until(
            app,
            lambda: len(calls) >= 3 and widget._graph_cache_payload is not None
            and len(widget._graph_cache_payload["nodes"]) == 3
            and not widget._graph_loading and not widget._graph_dirty,
        )
    finally:
        release_refresh.set()
        widget.close()
        app.processEvents()


def test_deep_zoom_uses_explicit_states_and_embedded_existing_graph(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    loads = []

    def provider():
        loads.append(True)
        return _graph_payload()

    widget = BrainOrbitWidget(graph_payload_provider=provider)
    widget.resize(900, 520)
    transitions = []
    widget.graph_portal_changed.connect(transitions.append)
    widget.show()
    try:
        assert _pump_until(app, lambda: widget._graph_cache_payload is not None)
        _hold_open(widget)
        assert widget.portal_mode == "entering"
        for _ in range(75):
            widget._tick()
        app.processEvents()

        assert widget.portal_mode == "graph"
        assert not hasattr(widget, "_graph_view")
        assert widget._graph_loaded
        assert len(widget._spatial_graph_nodes) == 2
        assert len(widget._spatial_graph_edges) == 1
        assert loads == [True]
        assert transitions == [True]
        assert not bool(widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

        opened = []
        selected = []
        widget.graph_note_open_requested.connect(opened.append)
        widget.graph_node_selected.connect(selected.append)
        widget._select_graph_node(widget._spatial_graph_nodes[0].id)
        assert selected[-1]["relative_path"] == "wiki/node-0.md"
        widget.open_note("projects/jarvis.md")
        assert opened == ["projects/jarvis.md"]

        _hold_closed(widget)
        assert widget.portal_mode == "exiting"
        for _ in range(90):
            widget._tick()
        app.processEvents()
        assert widget.portal_mode == "brain"
        assert not hasattr(widget, "_graph_view")
        assert transitions == [True, False]
        assert loads == [True]
    finally:
        widget.close()
        app.processEvents()


def test_hidden_widget_stops_all_animation_timers(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.show()
    try:
        assert widget.timer.isActive()
        assert _pump_until(app, lambda: widget._graph_cache_payload is not None)
        _hold_open(widget)
        for _ in range(75):
            widget._tick()
        assert widget.portal_mode == "graph"
        widget.hide()
        app.processEvents()
        assert not widget.timer.isActive()
        assert not widget._graph_reload_timer.isActive()
        assert not hasattr(widget, "_graph_view")
    finally:
        widget.close()
        app.processEvents()


def test_mouse_wheel_uses_the_same_portal_state_machine(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    import ui.brain_orbit as brain_orbit

    app = _APP
    clock = [10.0]
    monkeypatch.setattr(brain_orbit.time, "monotonic", lambda: clock[0])
    widget = brain_orbit.BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.show()

    def wheel(delta):
        widget.wheelEvent(QWheelEvent(
            QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, delta),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False,
        ))

    try:
        assert _pump_until(app, lambda: widget._graph_cache_payload is not None)
        wheel(1200)
        assert widget.portal_mode == "brain"
        clock[0] += .35
        widget._tick()
        assert widget.portal_mode == "entering"
        for _ in range(75):
            widget._tick()
        assert widget.portal_mode == "graph"

        wheel(-1200)
        clock[0] += .25
        widget._tick()
        assert widget.portal_mode == "exiting"
        for _ in range(75):
            widget._tick()
        assert widget.portal_mode == "brain"
    finally:
        widget.close()
        app.processEvents()


def test_compact_command_center_map_does_not_duplicate_graph_runtime(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    app = _APP
    calls = []
    widget = BrainOrbitWidget(compact=True, graph_payload_provider=lambda: calls.append(1) or _graph_payload())
    try:
        widget.apply_gesture_motion({"zoom": 1.0, "swipe_velocity": 0.0, "timestamp": 1.0})
        for _ in range(20):
            widget._tick()
        assert not hasattr(widget, "_graph_view")
        assert calls == []
        assert widget.target_zoom <= 1.3
    finally:
        widget.close()
        app.processEvents()


def test_vault_coordinates_are_deterministic_across_payload_order(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    payload = _graph_payload(8)
    first = BrainOrbitWidget(graph_payload_provider=lambda: payload)
    second = BrainOrbitWidget(graph_payload_provider=lambda: payload)
    try:
        first._graph_cache_payload = payload
        first._graph_cache_generation = 1
        first._apply_cached_graph()
        second._graph_cache_payload = {
            "nodes": list(reversed(payload["nodes"])),
            "edges": list(reversed(payload["edges"])),
        }
        second._graph_cache_generation = 1
        second._apply_cached_graph()
        coordinates_first = {
            node.id: (node.x, node.y, node.z) for node in first._spatial_graph_nodes
        }
        coordinates_second = {
            node.id: (node.x, node.y, node.z) for node in second._spatial_graph_nodes
        }
        assert coordinates_first == coordinates_second
    finally:
        first.close()
        second.close()
        _APP.processEvents()


def test_one_hand_rotates_but_only_two_hands_can_zoom(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    try:
        initial_zoom = widget.target_zoom
        widget.apply_gesture_motion({
            "zoom": 1.0,
            "zoom_active": True,
            "gesture_mode_active": True,
            "velocity_x": 1.3,
            "velocity_y": .4,
            "timestamp": 1.0,
            "hand_count": 1,
            "gesture_hand_count": 1,
            "tracking_state": "tracking",
        })
        assert widget.target_zoom == initial_zoom
        assert widget.yaw_velocity < 0
        assert widget.pitch_velocity > 0
        assert widget._enter_candidate_samples == 0

        _hold_open(widget, 2.0)
        assert widget.target_zoom > initial_zoom
        assert widget.portal_mode == "entering"
    finally:
        widget.close()
        _APP.processEvents()


def test_brain_yaw_crosses_full_turn_without_projection_snap(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QPointF
    from ui.brain_orbit import BrainOrbitWidget

    widget = BrainOrbitWidget(compact=True, graph_payload_provider=_graph_payload)
    try:
        widget.brain_yaw = math.tau - .002
        widget.yaw_velocity = .006
        before = widget._brain_projection(QPointF(200, 150), 170, 110)[0][0]
        widget._tick()
        after = widget._brain_projection(QPointF(200, 150), 170, 110)[0][0]
        assert 0.0 <= widget.brain_yaw < .02
        assert (after - before).manhattanLength() < 4.0
    finally:
        widget.close()
        _APP.processEvents()


def test_spatial_nodes_select_locally_and_double_click_opens_workspace(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtTest import QTest
    from ui.brain_orbit import BrainOrbitWidget

    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.resize(900, 520)
    widget._graph_cache_payload = _graph_payload(4)
    widget._graph_cache_generation = 1
    widget._graph_dirty = False
    widget._apply_cached_graph()
    widget.portal_progress = 1.0
    widget.portal_mode = "graph"
    widget.show()
    selected = []
    opened = []
    widget.graph_node_selected.connect(selected.append)
    widget.graph_note_open_requested.connect(opened.append)
    try:
        image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        widget._draw_knowledge_graph(
            painter,
            QPointF(widget.width() / 2, widget.height() / 2 - 8),
            1.0,
            1.0,
        )
        painter.end()
        assert widget._graph_hit_regions
        node_id, (region, _depth) = next(iter(widget._graph_hit_regions.items()))

        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=region.center().toPoint())
        assert widget._selected_graph_node_id == node_id
        assert selected[-1]["id"] == node_id

        QTest.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=region.center().toPoint())
        assert opened[-1] == widget._spatial_node_by_id[node_id].relative_path
    finally:
        widget.close()
        _APP.processEvents()


def test_hidden_orbit_nodes_are_not_clickable_inside_knowledge_space(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter
    from ui.brain_orbit import BrainOrbitWidget

    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    widget.resize(900, 520)
    widget._graph_cache_payload = _graph_payload(4)
    widget._graph_cache_generation = 1
    widget._graph_dirty = False
    widget._apply_cached_graph()
    widget.portal_progress = 1.0
    widget.portal_mode = "graph"
    widget.show()
    try:
        image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        widget.render(painter)
        painter.end()

        assert set(widget._hit_regions) == {"__camera__"}
        assert widget._graph_hit_regions
    finally:
        widget.close()
        _APP.processEvents()


def test_first_graph_load_failure_returns_to_brain_and_explicit_entry_retries(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from ui.brain_orbit import BrainOrbitWidget

    widget = BrainOrbitWidget(graph_payload_provider=_graph_payload)
    retries = []
    widget._graph_loading_generation = 3
    widget._graph_loading = True
    widget._portal_requested = True
    widget.portal_mode = "entering"
    widget.portal_progress = .61
    try:
        widget._on_graph_failed("vault unavailable", 3)
        assert widget._graph_error == "vault unavailable"
        assert widget._graph_loaded is False
        assert widget._portal_requested is False
        assert widget.portal_mode == "exiting"
        assert widget.target_zoom == pytest.approx(1.0)

        monkeypatch.setattr(widget, "_start_graph_load", lambda: retries.append(True))
        widget._request_portal(True, manual=True)
        assert widget._graph_dirty is True
        assert retries == [True]
        assert widget.portal_mode == "entering"
    finally:
        widget.close()
        _APP.processEvents()
