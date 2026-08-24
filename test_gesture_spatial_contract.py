"""Interaction contract for the gesture-driven 2.5D brain canvas.

These tests deliberately describe user-facing ownership rules rather than
MediaPipe implementation details:

* one hand owns rotation/panning (horizontal and vertical swipes), never zoom;
* two hands own depth zoom, never carousel swipes.
"""
from dataclasses import dataclass

import pytest

from core.gesture_runtime import GestureRuntime


@dataclass
class _Point:
    x: float
    y: float


def _open_hand(center_x: float, *, center_y: float = 0.55):
    points = [_Point(center_x, 0.65) for _ in range(21)]
    points[0] = _Point(center_x, 0.82)
    points[5] = _Point(center_x - 0.10, 0.63)
    points[9] = _Point(center_x, 0.55)
    points[13] = _Point(center_x + 0.06, 0.63)
    points[17] = _Point(center_x + 0.11, 0.68)
    for offset, (tip, pip) in enumerate(zip((8, 12, 16, 20), (6, 10, 14, 18))):
        points[pip] = _Point(center_x + (offset - 1.5) * 0.045, 0.52)
        points[tip] = _Point(center_x + (offset - 1.5) * 0.07, 0.20)
    points[4] = _Point(center_x - 0.18, 0.53)
    shift_y = center_y - 0.55
    return [_Point(point.x, point.y + shift_y) for point in points]


def test_one_hand_open_pose_never_owns_depth_zoom():
    runtime = GestureRuntime()

    first = runtime.process_landmarks([_open_hand(0.50)], timestamp=1.0, emit=False)
    second = runtime.process_landmarks([_open_hand(0.50)], timestamp=1.1, emit=False)

    assert first.hand_count == second.hand_count == 1
    assert first.zoom_active is False
    assert second.zoom_active is False
    assert second.zoom_delta == 0.0
    assert second.gesture_intent == "neutral"


def test_two_hand_parallel_translation_never_becomes_swipe():
    runtime = GestureRuntime()
    runtime.process_landmarks(
        [_open_hand(0.24), _open_hand(0.58)],
        handedness=["left", "right"], timestamp=1.0, emit=False,
    )
    candidate = runtime.process_landmarks(
        [_open_hand(0.30), _open_hand(0.64)],
        handedness=["left", "right"], timestamp=1.05, emit=False,
    )
    moved = runtime.process_landmarks(
        [_open_hand(0.36), _open_hand(0.70)],
        handedness=["left", "right"], timestamp=1.10, emit=False,
    )

    assert candidate.swipe_phase == "idle"
    assert moved.swipe_phase == "idle"
    assert moved.swipe_direction == ""
    assert moved.swipe_velocity == 0.0
    assert moved.zoom_active is False
    assert moved.gesture_intent == "neutral"


@pytest.mark.parametrize(
    ("path", "direction"),
    [
        (((0.20, 0.55), (0.28, 0.55), (0.36, 0.55)), "right"),
        (((0.80, 0.55), (0.72, 0.55), (0.64, 0.55)), "left"),
        (((0.50, 0.70), (0.50, 0.60), (0.50, 0.50)), "up"),
        (((0.50, 0.30), (0.50, 0.40), (0.50, 0.50)), "down"),
    ],
)
def test_one_hand_motion_owns_four_direction_navigation_without_zoom(path, direction):
    runtime = GestureRuntime()
    samples = [
        runtime.process_landmarks(
            [_open_hand(center_x, center_y=center_y)], timestamp=timestamp, emit=False,
        )
        for (center_x, center_y), timestamp in zip(path, (1.0, 1.05, 1.10))
    ]
    moved = samples[-1]

    assert moved.swipe_phase == "started"
    assert moved.swipe_direction == direction
    if direction == "right":
        assert moved.velocity_x > 0.0
    elif direction == "left":
        assert moved.velocity_x < 0.0
    elif direction == "up":
        assert moved.velocity_y < 0.0
    else:
        assert moved.velocity_y > 0.0
    assert moved.zoom_active is False
    assert moved.gesture_intent == "swipe"


def test_two_hand_span_change_is_the_only_depth_zoom_owner():
    runtime = GestureRuntime()
    runtime.process_landmarks(
        [_open_hand(0.40), _open_hand(0.60)],
        handedness=["left", "right"], timestamp=1.0, emit=False,
    )
    spread = runtime.process_landmarks(
        [_open_hand(0.28), _open_hand(0.72)],
        handedness=["left", "right"], timestamp=1.1, emit=False,
    )

    assert spread.hand_count == 2
    assert spread.zoom_active is True
    assert spread.zoom_delta > 0.0
    assert spread.swipe_phase == "idle"
    assert spread.gesture_intent == "zoom"
