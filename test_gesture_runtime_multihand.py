from dataclasses import dataclass

import pytest

from core.gesture_runtime import GestureRuntime, GestureTuning


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
    if shift_y:
        points = [_Point(point.x, point.y + shift_y) for point in points]
    return points


def _event(events, gesture, phase=None):
    return [
        item for item in events
        if item["gesture"] == gesture and (phase is None or item["phase"] == phase)
    ]


def test_two_hand_tracking_keeps_identity_when_detector_order_changes():
    runtime = GestureRuntime()
    first = runtime.process_landmarks(
        [_open_hand(0.25), _open_hand(0.75)],
        handedness=[("left", 0.96), ("right", 0.94)],
        timestamp=1.0,
        emit=False,
    )
    second = runtime.process_landmarks(
        [_open_hand(0.70), _open_hand(0.30)],
        handedness=[("right", 0.93), ("left", 0.95)],
        timestamp=1.1,
        emit=False,
    )

    assert first.hand_count == second.hand_count == 2
    by_id = {hand["track_id"]: hand for hand in second.hands}
    assert set(by_id) == {"left", "right"}
    assert by_id["left"]["center_x"] < by_id["right"]["center_x"]
    assert by_id["left"]["velocity_x"] > 0
    assert by_id["right"]["velocity_x"] < 0
    assert second.swipe_velocity == 0.0


def test_two_hand_distance_emits_continuous_zoom_events_without_swipe():
    events = []
    runtime = GestureRuntime()
    runtime.register_event("zoom", events.append)
    runtime.register_event("swipe", events.append)

    runtime.process_landmarks(
        [_open_hand(0.40), _open_hand(0.60)],
        handedness=["left", "right"], timestamp=1.0,
    )
    spread = runtime.process_landmarks(
        [_open_hand(0.28), _open_hand(0.72)],
        handedness=["left", "right"], timestamp=1.05,
    )
    pinched = runtime.process_landmarks(
        [_open_hand(0.43), _open_hand(0.57)],
        handedness=["left", "right"], timestamp=1.10,
    )

    zoom_events = _event(events, "zoom")
    assert len(zoom_events) == 3
    assert zoom_events[1]["source"] == "two_hand"
    assert zoom_events[1]["hand_count"] == 2
    assert zoom_events[1]["delta"] > 0
    assert zoom_events[1]["metadata"]["scale"] > 1.0
    assert zoom_events[2]["delta"] < 0
    assert spread.zoom_delta > 0
    assert pinched.zoom_delta < 0
    assert not _event(events, "swipe")


@pytest.mark.parametrize(
    ("initial", "translated"),
    [
        ((0.24, 0.58), (0.36, 0.70)),
        ((0.42, 0.76), (0.30, 0.64)),
    ],
)
def test_parallel_two_hand_translation_is_navigation_neutral(initial, translated):
    events = []
    runtime = GestureRuntime(actions={"gesture_event": events.append})
    runtime.process_landmarks(
        [_open_hand(initial[0]), _open_hand(initial[1])],
        handedness=["left", "right"], timestamp=1.0,
    )
    midpoint = tuple((left + right) / 2.0 for left, right in zip(initial, translated))
    candidate = runtime.process_landmarks(
        [_open_hand(midpoint[0]), _open_hand(midpoint[1])],
        handedness=["left", "right"], timestamp=1.05,
    )
    assert candidate.swipe_phase == "idle"
    assert not _event(events, "swipe", "started")
    motion = runtime.process_landmarks(
        [_open_hand(translated[0]), _open_hand(translated[1])],
        handedness=["left", "right"], timestamp=1.10,
    )

    assert not _event(events, "swipe", "started")
    assert motion.swipe_phase == "idle"
    assert motion.swipe_direction == ""
    assert motion.swipe_velocity == 0.0
    assert abs(motion.zoom_delta) <= runtime.tuning.zoom_deadband
    assert motion.zoom_active is False
    assert motion.gesture_intent == "neutral"


def test_parallel_two_hand_translation_with_span_noise_stays_neutral():
    events = []
    runtime = GestureRuntime(actions={"gesture_event": events.append})
    runtime.process_landmarks(
        [_open_hand(0.24), _open_hand(0.58)],
        handedness=["left", "right"], timestamp=1.0,
    )
    first_move = runtime.process_landmarks(
        [_open_hand(0.30), _open_hand(0.645)],
        handedness=["left", "right"], timestamp=1.05,
    )
    second_move = runtime.process_landmarks(
        [_open_hand(0.36), _open_hand(0.705)],
        handedness=["left", "right"], timestamp=1.10,
    )

    assert first_move.zoom_delta == 0.0
    assert second_move.zoom_delta == 0.0
    assert first_move.swipe_phase == "idle"
    assert second_move.swipe_phase == "idle"
    assert second_move.swipe_direction == ""
    assert second_move.gesture_intent == "neutral"
    assert all(item["delta"] == 0.0 for item in _event(events, "zoom"))
    assert not _event(events, "swipe", "started")


def test_two_hand_spread_cannot_leak_into_coordinated_swipe():
    events = []
    runtime = GestureRuntime(actions={"gesture_event": events.append})
    runtime.process_landmarks(
        [_open_hand(0.40), _open_hand(0.60)],
        handedness=["left", "right"], timestamp=1.0,
    )
    motion = runtime.process_landmarks(
        [_open_hand(0.27), _open_hand(0.73)],
        handedness=["left", "right"], timestamp=1.1,
    )

    assert motion.zoom_delta > 0
    assert motion.zoom_active is True
    assert motion.gesture_intent == "zoom"
    assert motion.swipe_velocity == 0.0
    assert not _event(events, "swipe", "started")


def test_two_hand_zoom_remains_latched_while_spread_pose_is_held():
    runtime = GestureRuntime()
    runtime.process_landmarks(
        [_open_hand(0.40), _open_hand(0.60)],
        handedness=["left", "right"], timestamp=1.0, emit=False,
    )
    spread = runtime.process_landmarks(
        [_open_hand(0.27), _open_hand(0.73)],
        handedness=["left", "right"], timestamp=1.1, emit=False,
    )
    held_once = runtime.process_landmarks(
        [_open_hand(0.27), _open_hand(0.73)],
        handedness=["left", "right"], timestamp=1.24, emit=False,
    )
    held_after_phase_release = runtime.process_landmarks(
        [_open_hand(0.27), _open_hand(0.73)],
        handedness=["left", "right"], timestamp=1.38, emit=False,
    )

    assert spread.zoom_active is True
    assert held_once.zoom_active is True
    assert held_after_phase_release.zoom_active is True
    assert held_after_phase_release.gesture_intent == "zoom"
    assert runtime._two_hand_intent.zoom_latched is True


def test_stationary_wide_two_hand_pose_does_not_acquire_zoom_latch():
    runtime = GestureRuntime()
    samples = [
        runtime.process_landmarks(
            [_open_hand(0.18), _open_hand(0.82)],
            handedness=["left", "right"], timestamp=timestamp, emit=False,
        )
        for timestamp in (1.0, 1.2, 1.4, 1.6)
    ]

    assert all(sample.zoom_active is False for sample in samples)
    assert all(sample.gesture_intent == "neutral" for sample in samples)
    assert runtime._two_hand_intent.zoom_latched is False


def test_small_parallel_two_hand_jitter_does_not_release_latched_zoom():
    runtime = GestureRuntime(tuning=GestureTuning(track_velocity_alpha=1.0))
    runtime.process_landmarks(
        [_open_hand(0.40), _open_hand(0.60)],
        handedness=["left", "right"], timestamp=1.0, emit=False,
    )
    runtime.process_landmarks(
        [_open_hand(0.10), _open_hand(0.90)],
        handedness=["left", "right"], timestamp=1.1, emit=False,
    )
    runtime.process_landmarks(
        [_open_hand(0.10), _open_hand(0.90)],
        handedness=["left", "right"], timestamp=1.24, emit=False,
    )
    held = runtime.process_landmarks(
        [_open_hand(0.10), _open_hand(0.90)],
        handedness=["left", "right"], timestamp=1.38, emit=False,
    )
    jittered = runtime.process_landmarks(
        [_open_hand(0.105), _open_hand(0.905)],
        handedness=["left", "right"], timestamp=1.48, emit=False,
    )

    assert held.zoom_active is True
    assert jittered.swipe_phase == "idle"
    assert jittered.zoom_active is True
    assert jittered.gesture_intent == "zoom"
    assert runtime._two_hand_intent.zoom_latched is True


def test_parallel_two_hand_translation_does_not_steal_latched_zoom():
    runtime = GestureRuntime(tuning=GestureTuning(track_velocity_alpha=1.0))
    runtime.process_landmarks(
        [_open_hand(0.40), _open_hand(0.60)],
        handedness=["left", "right"], timestamp=1.0, emit=False,
    )
    runtime.process_landmarks(
        [_open_hand(0.20), _open_hand(0.80)],
        handedness=["left", "right"], timestamp=1.1, emit=False,
    )
    runtime.process_landmarks(
        [_open_hand(0.20), _open_hand(0.80)],
        handedness=["left", "right"], timestamp=1.24, emit=False,
    )
    runtime.process_landmarks(
        [_open_hand(0.20), _open_hand(0.80)],
        handedness=["left", "right"], timestamp=1.38, emit=False,
    )
    candidate = runtime.process_landmarks(
        [_open_hand(0.27), _open_hand(0.87)],
        handedness=["left", "right"], timestamp=1.48, emit=False,
    )
    started = runtime.process_landmarks(
        [_open_hand(0.34), _open_hand(0.94)],
        handedness=["left", "right"], timestamp=1.58, emit=False,
    )

    assert candidate.swipe_phase == "idle"
    assert candidate.zoom_active is True
    assert candidate.gesture_intent == "zoom"
    assert started.swipe_phase == "idle"
    assert started.zoom_active is True
    assert started.gesture_intent == "zoom"
    assert runtime._two_hand_intent.zoom_latched is True


def test_short_two_to_one_to_two_dropout_stays_neutral_and_keeps_two_hand_mode():
    events = []
    runtime = GestureRuntime(actions={"gesture_event": events.append})
    runtime.process_landmarks(
        [_open_hand(0.25), _open_hand(0.65)],
        handedness=["left", "right"], timestamp=1.0,
    )
    pending = runtime.process_landmarks(
        [_open_hand(0.48)], handedness=["left"], timestamp=1.05,
    )
    recovered = runtime.process_landmarks(
        [_open_hand(0.31), _open_hand(0.71)],
        handedness=["left", "right"], timestamp=1.10,
    )

    assert pending.hand_count == 1
    assert pending.gesture_hand_count == 2
    assert pending.gesture_mode_active is False
    assert pending.swipe_velocity == 0.0
    assert recovered.gesture_hand_count == 2
    assert recovered.gesture_mode_active is False
    assert runtime._hand_count_state.stable_count == 2
    assert not _event(events, "swipe", "started")


def test_one_to_two_false_positive_cancels_candidate_and_requires_stable_rearm():
    events = []
    runtime = GestureRuntime(actions={"gesture_event": events.append})
    runtime.process_landmarks([_open_hand(0.20)], timestamp=1.0)
    runtime.process_landmarks([_open_hand(0.27)], timestamp=1.1)
    false_two = runtime.process_landmarks(
        [_open_hand(0.30), _open_hand(0.70)],
        handedness=["left", "right"], timestamp=1.15,
    )
    returned = runtime.process_landmarks([_open_hand(0.34)], timestamp=1.20)

    assert false_two.gesture_hand_count == 1
    assert false_two.gesture_mode_active is False
    assert returned.gesture_hand_count == 1
    assert returned.gesture_mode_active is False
    assert runtime._swipe_state.candidate_samples == 0
    assert not _event(events, "swipe", "started")


def test_persistent_hand_count_change_waits_for_grace_and_settle_before_one_hand_commands():
    calls = []
    runtime = GestureRuntime()
    runtime.register_gesture(
        "custom_command", lambda _points: True,
        hold_seconds=0.0, cooldown=0.0,
        callback=lambda: calls.append("called"),
    )
    runtime.process_landmarks(
        [_open_hand(0.25), _open_hand(0.70)],
        handedness=["left", "right"], timestamp=1.0, emit=False,
    )
    runtime.process_landmarks([_open_hand(0.30)], timestamp=1.05, emit=False)
    changed = runtime.process_landmarks([_open_hand(0.30)], timestamp=1.15, emit=False)
    settle_one = runtime.process_landmarks([_open_hand(0.30)], timestamp=1.25, emit=False)
    settle_two = runtime.process_landmarks([_open_hand(0.30)], timestamp=1.35, emit=False)
    assert changed.gesture_mode_active is False
    assert settle_one.gesture_mode_active is False
    assert settle_two.gesture_mode_active is False
    assert calls == []

    runtime.process_landmarks([_open_hand(0.30)], timestamp=1.45, emit=False)
    runtime.process_landmarks([_open_hand(0.30)], timestamp=1.46, emit=False)
    assert calls == ["called"]


def test_swipe_uses_horizontal_hysteresis_release_and_cooldown():
    events = []
    runtime = GestureRuntime(actions={"gesture_event": events.append})

    # One vertical movement frame is only a candidate; confirmation prevents
    # an isolated detector jump from firing a pan command.
    runtime.process_landmarks([_open_hand(0.20, center_y=0.45)], timestamp=1.0)
    vertical_candidate = runtime.process_landmarks(
        [_open_hand(0.205, center_y=0.62)], timestamp=1.1,
    )
    assert vertical_candidate.swipe_phase == "candidate"
    assert vertical_candidate.swipe_direction == "down"
    assert not _event(events, "swipe", "started")

    runtime._reset_motion_state(tracking_state="idle")
    runtime.process_landmarks([_open_hand(0.20)], timestamp=2.0)
    candidate = runtime.process_landmarks([_open_hand(0.27)], timestamp=2.05)
    assert candidate.swipe_phase == "candidate"
    assert not _event(events, "swipe", "started")
    started = runtime.process_landmarks([_open_hand(0.34)], timestamp=2.10)
    assert started.swipe_direction == "right"
    assert started.swipe_velocity > 0
    assert len(_event(events, "swipe", "started")) == 1

    # A tiny reversal is absorbed by the active-direction hysteresis.
    runtime.process_landmarks([_open_hand(0.33)], timestamp=2.24)
    assert not any(item["direction"] == "left" for item in _event(events, "swipe", "started"))

    # Slow frames release once, then the cooldown blocks an immediate reversal.
    runtime.process_landmarks([_open_hand(0.33)], timestamp=2.40)
    runtime.process_landmarks([_open_hand(0.33)], timestamp=2.52)
    assert len(_event(events, "swipe", "ended")) == 1
    runtime.process_landmarks([_open_hand(0.12)], timestamp=2.60)
    assert len(_event(events, "swipe", "started")) == 1

    # After re-arm, accumulated left travel produces exactly one new start.
    runtime.process_landmarks([_open_hand(0.12)], timestamp=2.90)
    runtime.process_landmarks([_open_hand(0.07)], timestamp=2.95)
    runtime.process_landmarks([_open_hand(0.02)], timestamp=3.00)
    starts = _event(events, "swipe", "started")
    assert [item["direction"] for item in starts] == ["right", "left"]


def test_swipe_direction_change_requires_release_before_rearming():
    events = []
    runtime = GestureRuntime(
        actions={"gesture_event": events.append},
        tuning=GestureTuning(track_velocity_alpha=1.0, swipe_cooldown=0.20),
    )
    runtime.process_landmarks([_open_hand(0.20)], timestamp=1.0)
    runtime.process_landmarks([_open_hand(0.275)], timestamp=1.1)
    runtime.process_landmarks([_open_hand(0.35)], timestamp=1.2)
    runtime.process_landmarks([_open_hand(0.15)], timestamp=1.3)
    assert [item["direction"] for item in _event(events, "swipe", "started")] == ["right"]

    # Continuing the reversed motion after cooldown is not a release/re-arm.
    runtime.process_landmarks([_open_hand(0.05)], timestamp=1.6)
    assert [item["direction"] for item in _event(events, "swipe", "started")] == ["right"]

    # A real neutral interval re-arms the state machine for the next swipe.
    runtime.process_landmarks([_open_hand(0.05)], timestamp=1.7)
    runtime.process_landmarks([_open_hand(0.05)], timestamp=1.82)
    runtime.process_landmarks([_open_hand(0.125)], timestamp=1.92)
    runtime.process_landmarks([_open_hand(0.20)], timestamp=2.02)
    assert [item["direction"] for item in _event(events, "swipe", "started")] == ["right", "right"]


def test_tracking_loss_resets_motion_state_and_reacquire_has_no_velocity_spike():
    events = []
    runtime = GestureRuntime(actions={"event": events.append})
    runtime.process_landmarks([_open_hand(0.20)], timestamp=1.0)
    runtime.process_landmarks([_open_hand(0.31)], timestamp=1.1)
    runtime.process_landmarks([_open_hand(0.42)], timestamp=1.2)
    assert runtime._swipe_state.active_direction == "right"

    runtime.process_landmarks([], timestamp=1.30)
    recovered = runtime.process_landmarks([_open_hand(0.80)], timestamp=1.41)
    assert recovered.tracking_state == "recovered"
    assert recovered.velocity_x == pytest.approx(0.0)
    assert recovered.swipe_velocity == 0.0

    runtime.process_landmarks([], timestamp=1.80)
    assert runtime._tracking_state == "lost"
    assert runtime._hand_tracks == {}
    assert runtime._smooth_motion is None
    assert runtime._previous_zoom is None
    assert runtime._two_hand_baseline is None
    assert runtime._swipe_state.active_direction == ""

    reacquired = runtime.process_landmarks([_open_hand(0.10)], timestamp=2.1)
    assert reacquired.tracking_state == "recovered"
    assert reacquired.velocity_x == pytest.approx(0.0)
    assert reacquired.swipe_velocity == 0.0
    assert len(_event(events, "tracking", "lost")) == 1


def test_no_hand_grace_emits_one_neutral_lost_motion_for_motion_only_consumers():
    motions = []
    runtime = GestureRuntime(actions={"motion": motions.append})
    runtime.process_landmarks([_open_hand(0.30)], timestamp=1.0)
    runtime.process_landmarks([], timestamp=1.20)
    assert len(motions) == 1

    runtime.process_landmarks([], timestamp=1.31)
    assert len(motions) == 2
    assert motions[-1]["hand_count"] == 0
    assert motions[-1]["tracking_state"] == "lost"
    assert motions[-1]["velocity_x"] == 0.0
    assert motions[-1]["swipe_velocity"] == 0.0

    runtime.process_landmarks([], timestamp=1.80)
    assert len(motions) == 2


def test_stop_clears_all_tracking_swipe_and_zoom_baselines():
    runtime = GestureRuntime()
    runtime.process_landmarks(
        [_open_hand(0.30), _open_hand(0.70)],
        handedness=["left", "right"], timestamp=1.0, emit=False,
    )
    runtime.process_landmarks(
        [_open_hand(0.42), _open_hand(0.82)],
        handedness=["left", "right"], timestamp=1.1, emit=False,
    )
    assert runtime._hand_tracks
    assert runtime._previous_two_hand_span is not None

    runtime.stop()

    assert runtime._tracking_state == "idle"
    assert runtime._hand_tracks == {}
    assert runtime._smooth_motion is None
    assert runtime._previous_zoom is None
    assert runtime._two_hand_baseline is None
    assert runtime._previous_two_hand_span is None
    assert runtime._swipe_state.active_direction == ""


def test_two_hands_suppress_registered_one_hand_commands():
    calls = []
    runtime = GestureRuntime()
    runtime.register_gesture(
        "custom_command", lambda _points: True,
        hold_seconds=0.0, cooldown=0.0,
        callback=lambda: calls.append("called"),
    )
    runtime.process_landmarks([_open_hand(0.25), _open_hand(0.75)], timestamp=1.0, emit=False)
    runtime.process_landmarks([_open_hand(0.25), _open_hand(0.75)], timestamp=2.0, emit=False)
    assert calls == []

    runtime.process_landmarks([_open_hand(0.25)], timestamp=3.0, emit=False)
    runtime.process_landmarks([_open_hand(0.25)], timestamp=3.10, emit=False)
    runtime.process_landmarks([_open_hand(0.25)], timestamp=3.20, emit=False)
    runtime.process_landmarks([_open_hand(0.25)], timestamp=3.30, emit=False)
    runtime.process_landmarks([_open_hand(0.25)], timestamp=3.40, emit=False)
    runtime.process_landmarks([_open_hand(0.25)], timestamp=3.41, emit=False)
    assert calls == ["called"]


def test_event_registry_can_unsubscribe_without_touching_camera_runtime():
    received = []
    runtime = GestureRuntime(tuning=GestureTuning(max_hands=2))
    runtime.register_event("zoom", received.append)
    runtime.process_landmarks([_open_hand(0.5)], timestamp=1.0)
    assert len(received) == 1
    runtime.unregister_event("zoom", received.append)
    runtime.process_landmarks([_open_hand(0.5)], timestamp=1.1)
    assert len(received) == 1
