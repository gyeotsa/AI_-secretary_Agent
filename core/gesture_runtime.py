"""Privacy-first, extensible continuous hand-gesture runtime.

Frames are processed locally and discarded immediately. The runtime publishes
discrete actions and smoothed motion samples so the UI can respond to hand
openness and swipe speed without hard-coding every future gesture in the loop.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional
import math
import os
import threading
import time
import urllib.request

from core.runtime.event_bus import Event, get_event_bus


GESTURE_POSE_LABELS = {
    "open_palm": "손바닥 펼치기",
    "thumbs_up": "엄지 올리기",
    "closed_fist": "주먹 쥐기",
    "point_up": "검지 올리기",
}

GESTURE_ACTION_LABELS = {
    "": "동작 없음",
    "stop_tts": "음성 출력 중지",
    "approve": "승인",
    "cancel": "취소",
    "next_workspace": "다음 전문가 작업공간",
    "toggle_chat": "채팅 영역 접기/펼치기",
}

DEFAULT_GESTURE_MAPPING = {
    "open_palm": "stop_tts",
    "thumbs_up": "approve",
    "closed_fist": "cancel",
    "point_up": "next_workspace",
}


def normalize_gesture_sensitivity(value) -> int:
    """Return a stable 0..100 sensitivity value for persisted/UI input."""
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        numeric = 60
    return max(0, min(100, numeric))


def normalize_gesture_mapping(mapping) -> dict[str, str]:
    """Keep only supported physical poses and safe in-app actions."""
    source = mapping if isinstance(mapping, dict) else {}
    allowed_actions = set(GESTURE_ACTION_LABELS)
    normalized = {}
    for pose in GESTURE_POSE_LABELS:
        action = str(source.get(pose, DEFAULT_GESTURE_MAPPING[pose]) or "").strip()
        normalized[pose] = action if action in allowed_actions else DEFAULT_GESTURE_MAPPING[pose]
    return normalized


@dataclass(frozen=True)
class GestureStatus:
    enabled: bool
    running: bool
    camera_index: int
    backend: str
    last_gesture: str = ""
    error: str = ""
    privacy: str = "local-only; frames are not stored"
    sensitivity: int = 60
    command_gestures_enabled: bool = False
    gesture_mapping: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GestureMotion:
    openness: float
    pinch_span: float
    zoom: float
    center_x: float
    center_y: float
    velocity_x: float
    velocity_y: float
    swipe_velocity: float
    timestamp: float
    hand_count: int = 1
    zoom_delta: float = 0.0
    zoom_scale: float = 1.0
    swipe_direction: str = ""
    swipe_phase: str = "idle"
    tracking_state: str = "tracking"
    hands: tuple[dict, ...] = ()
    gesture_hand_count: int = 1
    gesture_mode_active: bool = True
    zoom_active: bool = False
    gesture_intent: str = "neutral"


@dataclass(frozen=True)
class GestureEvent:
    """Device-independent gesture event published by the runtime.

    ``gesture`` is the registry key (for example ``zoom`` or ``swipe``),
    while ``phase`` lets consumers handle start/update/end without adding new
    camera-loop branches for every interaction.
    """

    gesture: str
    phase: str
    value: float
    timestamp: float
    delta: float = 0.0
    velocity: float = 0.0
    direction: str = ""
    hand_count: int = 0
    source: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GestureTuning:
    """Centralised motion thresholds; callers may tune devices in tests/UI."""

    max_hands: int = 2
    hand_count_confirm_samples: int = 2
    hand_count_switch_grace: float = 0.08
    hand_count_settle_samples: int = 2
    smoothing_alpha: float = 0.32
    track_velocity_alpha: float = 0.48
    track_match_distance: float = 0.65
    hand_loss_grace: float = 0.30
    velocity_reset_gap: float = 0.14
    swipe_min_openness: float = 0.58
    swipe_enter_velocity: float = 0.42
    swipe_exit_velocity: float = 0.16
    # Dominant-axis ratio shared by horizontal rotation and vertical panning.
    # The historic name is retained for configuration compatibility.
    swipe_horizontal_ratio: float = 1.35
    swipe_min_travel: float = 0.045
    swipe_confirm_samples: int = 2
    swipe_release_seconds: float = 0.10
    swipe_cooldown: float = 0.32
    # Retained as no-op constructor fields for older saved tuning profiles.
    # Two-hand translation no longer participates in gesture recognition.
    two_hand_swipe_span_deadband: float = 0.025
    two_hand_intent_dominance: float = 1.35
    two_hand_zoom_min_travel: float = 0.012
    two_hand_translation_deadband: float = 0.004
    two_hand_phase_release_seconds: float = 0.10
    zoom_deadband: float = 0.003

    def with_sensitivity(self, value) -> "GestureTuning":
        """Scale recognition gates while preserving explicit device fields.

        Fifty reproduces the historical thresholds. Higher values accept a
        smaller/faster-to-confirm movement; lower values deliberately require a
        larger and cleaner swipe. The returned immutable object is safe to swap
        while the capture thread is running.
        """
        sensitivity = normalize_gesture_sensitivity(value)
        normalized = sensitivity / 100.0
        threshold_scale = 1.45 - (0.90 * normalized)
        return replace(
            self,
            track_velocity_alpha=max(0.28, min(0.78, 0.34 + normalized * 0.34)),
            swipe_min_openness=max(0.40, min(0.72, 0.68 - normalized * 0.20)),
            swipe_enter_velocity=max(0.20, min(0.68, 0.42 * threshold_scale)),
            swipe_exit_velocity=max(0.08, min(0.28, 0.16 * threshold_scale)),
            swipe_horizontal_ratio=max(1.12, min(1.62, 1.57 - normalized * 0.42)),
            swipe_min_travel=max(0.020, min(0.070, 0.045 * threshold_scale)),
            swipe_release_seconds=max(0.06, min(0.16, 0.14 - normalized * 0.08)),
            swipe_cooldown=max(0.16, min(0.55, 0.50 - normalized * 0.32)),
        )


@dataclass(frozen=True)
class HandMotion:
    track_id: str
    handedness: str
    confidence: float
    openness: float
    pinch_span: float
    center_x: float
    center_y: float
    velocity_x: float
    velocity_y: float
    delta_x: float = 0.0
    delta_y: float = 0.0
    recovered: bool = False


@dataclass
class _HandTrack:
    track_id: str
    handedness: str
    confidence: float
    center_x: float
    center_y: float
    timestamp: float
    velocity_x: float = 0.0
    velocity_y: float = 0.0


@dataclass
class _SwipeState:
    candidate_direction: str = ""
    candidate_hand_count: int = 0
    candidate_origin: float = 0.0
    candidate_samples: int = 0
    active_direction: str = ""
    active_hand_count: int = 0
    inactive_since: float = 0.0
    cooldown_until: float = 0.0
    armed: bool = True
    rearm_since: float = 0.0


@dataclass
class _HandCountState:
    stable_count: int = 0
    candidate_count: int = 0
    candidate_since: float = 0.0
    candidate_samples: int = 0
    settle_remaining: int = 0


@dataclass
class _TwoHandIntentState:
    phase: str = "neutral"
    previous_span: float | None = None
    accumulated_span: float = 0.0
    zoom_anchor: float | None = None
    # A real two-hand spread/pinch acquires portal zoom until the hands are
    # lost or their count changes. Two-hand translation deliberately never
    # becomes navigation; one hand exclusively owns rotation and panning.
    # This lets users hold a spread pose for a dwell transition without
    # treating an initially wide, stationary pose as zoom.
    zoom_latched: bool = False


@dataclass
class GestureRecognizer:
    name: str
    predicate: Callable[[object], bool]
    cooldown: float = 1.5
    hold_seconds: float = 0.8
    action: str = ""


class GestureRuntime:
    """Run MediaPipe lazily and expose continuous, device-independent motion."""

    MODEL_URL = (
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
        "hand_landmarker/float16/1/hand_landmarker.task"
    )

    def __init__(self, *, camera_index: int = 0, actions: dict[str, Callable] | None = None,
                 model_path: str | None = None, enable_command_gestures: bool = False,
                 tuning: GestureTuning | None = None, sensitivity: int = 60,
                 gesture_mapping: dict[str, str] | None = None):
        self.camera_index = int(camera_index)
        self.actions = dict(actions or {})
        self.sensitivity = normalize_gesture_sensitivity(sensitivity)
        self._base_tuning = tuning or GestureTuning()
        self.tuning = self._base_tuning.with_sensitivity(self.sensitivity)
        self.command_gestures_enabled = bool(enable_command_gestures)
        self.gesture_mapping = normalize_gesture_mapping(gesture_mapping)
        configured = model_path or os.getenv("JARVIS_HAND_LANDMARKER_MODEL", "")
        self.model_path = Path(configured or "data/models/mediapipe/hand_landmarker.task")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_gesture = ""
        self._last_pose = ""
        self._error = ""
        self._cooldown_until: dict[str, float] = {}
        self._ready = threading.Event()
        self._previous_center: tuple[float, float, float] | None = None
        self._smooth_motion: GestureMotion | None = None
        self._recognizers: list[GestureRecognizer] = []
        self._event_handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._hand_tracks: dict[str, _HandTrack] = {}
        self._next_track_number = 1
        self._last_hand_seen: float | None = None
        self._tracking_state = "idle"
        self._previous_zoom: tuple[str, float, float] | None = None
        self._two_hand_baseline: float | None = None
        self._previous_two_hand_span: float | None = None
        self._swipe_state = _SwipeState()
        self._hand_count_state = _HandCountState()
        self._two_hand_intent = _TwoHandIntentState()
        self._last_mode_active = False
        self._candidate_gesture = ""
        self._candidate_since = 0.0
        if self.command_gestures_enabled:
            self._register_builtin_gestures()

    def _register_builtin_gestures(self) -> None:
        self._recognizers = [
            item for item in self._recognizers if not item.name.startswith("builtin_pose:")
        ]
        pose_classes = {
            "open_palm": "stop_tts",
            "thumbs_up": "approve",
            "closed_fist": "cancel",
            "point_up": "switch_workspace",
        }
        if not self.command_gestures_enabled:
            return
        for pose, legacy_class in pose_classes.items():
            action = self.gesture_mapping.get(pose, "")
            if not action:
                continue
            self.register_gesture(
                f"builtin_pose:{pose}",
                lambda points, expected=legacy_class: self._classify(points) == expected,
                action=action,
            )

    def configure(self, *, sensitivity=None, command_gestures_enabled=None,
                  gesture_mapping=None) -> dict:
        """Apply persisted UI configuration without restarting the camera."""
        if sensitivity is not None:
            self.sensitivity = normalize_gesture_sensitivity(sensitivity)
            self.tuning = self._base_tuning.with_sensitivity(self.sensitivity)
            self._reset_motion_state(tracking_state=self._tracking_state)
        if gesture_mapping is not None:
            self.gesture_mapping = normalize_gesture_mapping(gesture_mapping)
        if command_gestures_enabled is not None:
            self.command_gestures_enabled = bool(command_gestures_enabled)
        if gesture_mapping is not None or command_gestures_enabled is not None:
            self._register_builtin_gestures()
            self._candidate_gesture = ""
            self._candidate_since = 0.0
            self._last_gesture = ""
            self._last_pose = ""
        return self.configuration()

    def configuration(self) -> dict:
        return {
            "sensitivity": self.sensitivity,
            "command_gestures_enabled": self.command_gestures_enabled,
            "gesture_mapping": dict(self.gesture_mapping),
        }

    def register_gesture(self, name: str, predicate: Callable[[object], bool], *, cooldown: float = 1.5,
                         hold_seconds: float = 0.8, callback: Callable | None = None,
                         action: str | None = None) -> None:
        """Register an opt-in command gesture with a stable-hold safety gate."""
        self._recognizers = [item for item in self._recognizers if item.name != name]
        self._recognizers.append(GestureRecognizer(
            str(name), predicate, max(0.0, float(cooldown)),
            max(0.0, float(hold_seconds)), str(action or name),
        ))
        if callback is not None:
            self.actions[str(action or name)] = callback

    def register_event(self, gesture: str, callback: Callable[[dict], None]) -> None:
        """Subscribe to a continuous gesture without modifying the capture loop.

        Use ``"*"`` to observe every event. Event callbacks receive a stable
        dictionary representation of :class:`GestureEvent`.
        """
        key = str(gesture or "").strip().casefold()
        if not key:
            raise ValueError("gesture event name is required")
        handlers = self._event_handlers.setdefault(key, [])
        if callback not in handlers:
            handlers.append(callback)

    def unregister_event(self, gesture: str, callback: Callable[[dict], None]) -> None:
        key = str(gesture or "").strip().casefold()
        handlers = self._event_handlers.get(key, [])
        if callback in handlers:
            handlers.remove(callback)
        if not handlers:
            self._event_handlers.pop(key, None)

    def dependency_status(self) -> dict:
        try:
            import mediapipe as mp
            import cv2  # noqa: F401
            tasks_available = bool(getattr(mp, "tasks", None))
            return {
                "available": tasks_available,
                "backend": "MediaPipe Tasks Hand Landmarker",
                "model_ready": self.model_path.is_file(),
                "model_path": str(self.model_path),
                "error": "" if tasks_available else "MediaPipe Tasks API를 찾지 못했습니다.",
            }
        except Exception as exc:
            return {"available": False, "backend": "2D pointer fallback", "error": f"{type(exc).__name__}: {exc}"}

    def ensure_model(self) -> Path:
        if self.model_path.is_file() and self.model_path.stat().st_size > 1_000_000:
            return self.model_path
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.model_path.with_suffix(".download")
        try:
            with urllib.request.urlopen(self.MODEL_URL, timeout=60) as response, temporary.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            if temporary.stat().st_size <= 1_000_000:
                raise RuntimeError("손 인식 모델 다운로드 결과가 비정상적으로 작습니다.")
            os.replace(temporary, self.model_path)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return self.model_path

    def status(self) -> dict:
        dependency = self.dependency_status()
        return asdict(GestureStatus(
            bool(dependency["available"]), self._running, self.camera_index,
            dependency["backend"], self._last_gesture,
            self._error or dependency.get("error", ""),
            sensitivity=self.sensitivity,
            command_gestures_enabled=self.command_gestures_enabled,
            gesture_mapping=dict(self.gesture_mapping),
        ))

    def start(self, *, ready_timeout: float = 5.0) -> GestureStatus:
        if self._running:
            return GestureStatus(**self.status())
        dependency = self.dependency_status()
        if not dependency["available"]:
            raise RuntimeError("제스처 기능에는 optional MediaPipe 패키지가 필요합니다.")
        try:
            self.ensure_model()
        except Exception as exc:
            raise RuntimeError(f"손 인식 모델을 준비하지 못했습니다: {exc}") from exc
        self._running = True
        self._error = ""
        self._ready.clear()
        self._reset_motion_state(tracking_state="idle")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-gesture")
        self._thread.start()
        self._ready.wait(timeout=max(0.1, ready_timeout))
        if self._error:
            self._running = False
            raise RuntimeError(self._error)
        if not self._ready.is_set():
            self.stop()
            raise RuntimeError("카메라 준비 시간이 초과되었습니다.")
        get_event_bus().publish(Event("gesture.started", "gesture_runtime", data={"camera": self.camera_index}))
        return GestureStatus(**self.status())

    def stop(self) -> GestureStatus:
        was_running = self._running
        self._running = False
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._reset_motion_state(tracking_state="idle")
        if was_running:
            get_event_bus().publish(Event("gesture.stopped", "gesture_runtime", data={}))
        return GestureStatus(**self.status())

    def _loop(self) -> None:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode

        capture = None
        hands = None
        timestamp_ms = 0
        try:
            capture = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
            if not capture.isOpened():
                raise RuntimeError(f"카메라 {self.camera_index}를 열 수 없습니다.")
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            capture.set(cv2.CAP_PROP_FPS, 30)
            hands = HandLandmarker.create_from_options(HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(self.model_path.resolve())),
                running_mode=RunningMode.VIDEO, num_hands=max(1, int(self.tuning.max_hands)),
                min_hand_detection_confidence=0.65,
                min_hand_presence_confidence=0.6,
                min_tracking_confidence=0.6,
            ))
            self._ready.set()
            while self._running:
                ok, frame = capture.read()
                if not ok:
                    time.sleep(0.04)
                    continue
                frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                timestamp_ms = max(timestamp_ms + 1, int(time.monotonic() * 1000))
                result = hands.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp_ms)
                if result.hand_landmarks:
                    handedness = [
                        result.handedness[index] if index < len(result.handedness) else None
                        for index in range(len(result.hand_landmarks))
                    ]
                    self.process_landmarks(
                        result.hand_landmarks,
                        handedness=handedness,
                        timestamp=timestamp_ms / 1000.0,
                    )
                else:
                    self.process_landmarks((), timestamp=timestamp_ms / 1000.0)
                time.sleep(0.018)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            get_event_bus().publish(Event("gesture.failed", "gesture_runtime", data={"error": self._error}))
        finally:
            self._running = False
            self._ready.set()
            if hands is not None:
                hands.close()
            if capture is not None:
                capture.release()

    @staticmethod
    def _coord(point, axis: str) -> float:
        return float(getattr(point, axis, 0.0))

    @classmethod
    def _distance(cls, left, right) -> float:
        return math.hypot(cls._coord(left, "x") - cls._coord(right, "x"),
                          cls._coord(left, "y") - cls._coord(right, "y"))

    @staticmethod
    def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
        return min(upper, max(lower, float(value)))

    @staticmethod
    def _normalise_handedness(value) -> tuple[str, float]:
        """Accept MediaPipe categories as well as simple test-friendly values."""
        if isinstance(value, (list, tuple)) and value:
            if len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], (int, float)):
                return value[0].casefold(), float(value[1])
            value = value[0]
        if isinstance(value, str):
            return value.casefold(), 0.0
        if isinstance(value, dict):
            label = value.get("category_name") or value.get("display_name") or value.get("label") or ""
            return str(label).casefold(), float(value.get("score") or value.get("confidence") or 0.0)
        label = getattr(value, "category_name", "") or getattr(value, "display_name", "")
        return str(label).casefold(), float(getattr(value, "score", 0.0) or 0.0)

    def _hand_geometry(self, points) -> tuple[float, float, float, float]:
        indices = (0, 5, 9, 13, 17)
        center_x = sum(self._coord(points[index], "x") for index in indices) / len(indices)
        center_y = sum(self._coord(points[index], "y") for index in indices) / len(indices)
        palm = max(0.04, self._distance(points[0], points[9]))
        extended_count = sum(
            self._coord(points[tip], "y") < self._coord(points[pip], "y")
            for tip, pip in zip((8, 12, 16, 20), (6, 10, 14, 18))
        )
        reach = sum(self._distance(points[0], points[index]) for index in (8, 12, 16, 20)) / (4 * palm)
        reach_open = min(1.0, max(0.0, (reach - 1.15) / 1.25))
        openness = min(1.0, max(0.0, 0.62 * (extended_count / 4.0) + 0.38 * reach_open))
        pinch_span = min(1.0, max(0.0, self._distance(points[4], points[8]) / (palm * 1.35)))
        return center_x, center_y, openness, pinch_span

    def _allocate_track_id(self, handedness: str) -> str:
        base = handedness if handedness in {"left", "right"} else "hand"
        candidate = base
        while candidate in self._hand_tracks:
            candidate = f"{base}-{self._next_track_number}"
            self._next_track_number += 1
        return candidate

    def _observe_hands(self, hands, handedness, now: float) -> list[HandMotion]:
        self._hand_tracks = {
            key: track for key, track in self._hand_tracks.items()
            if now - track.timestamp <= self.tuning.hand_loss_grace
        }
        descriptions = []
        handedness = list(handedness or ())
        for index, points in enumerate(hands):
            label, confidence = self._normalise_handedness(
                handedness[index] if index < len(handedness) else None
            )
            center_x, center_y, openness, pinch_span = self._hand_geometry(points)
            descriptions.append((label, confidence, center_x, center_y, openness, pinch_span))

        unused_tracks = set(self._hand_tracks)
        observed: list[HandMotion] = []
        for label, confidence, center_x, center_y, openness, pinch_span in descriptions:
            matching = [
                track for key, track in self._hand_tracks.items()
                if key in unused_tracks and label and track.handedness == label
            ]
            candidates = matching or [
                track for key, track in self._hand_tracks.items() if key in unused_tracks
            ]
            track = min(
                candidates,
                key=lambda item: math.hypot(center_x - item.center_x, center_y - item.center_y),
                default=None,
            )
            if track is not None and math.hypot(
                    center_x - track.center_x, center_y - track.center_y
            ) > self.tuning.track_match_distance:
                track = None

            recovered = False
            delta_x = delta_y = velocity_x = velocity_y = 0.0
            if track is None:
                track_id = self._allocate_track_id(label)
                track = _HandTrack(track_id, label, confidence, center_x, center_y, now)
                self._hand_tracks[track_id] = track
            else:
                unused_tracks.discard(track.track_id)
                dt = now - track.timestamp
                recovered = dt <= 0.0 or dt > self.tuning.velocity_reset_gap
                if not recovered:
                    delta_x = center_x - track.center_x
                    delta_y = center_y - track.center_y
                    bounded_dt = max(1 / 120, dt)
                    raw_velocity_x = delta_x / bounded_dt
                    raw_velocity_y = delta_y / bounded_dt
                    alpha = self._clamp(self.tuning.track_velocity_alpha)
                    velocity_x = track.velocity_x + alpha * (raw_velocity_x - track.velocity_x)
                    velocity_y = track.velocity_y + alpha * (raw_velocity_y - track.velocity_y)
                track.handedness = label or track.handedness
                track.confidence = confidence or track.confidence
                track.center_x = center_x
                track.center_y = center_y
                track.timestamp = now
                track.velocity_x = velocity_x
                track.velocity_y = velocity_y

            observed.append(HandMotion(
                track.track_id, track.handedness, track.confidence,
                openness, pinch_span, center_x, center_y,
                velocity_x, velocity_y, delta_x, delta_y, recovered,
            ))
        return sorted(observed, key=lambda item: item.track_id)

    def _reset_interaction_phases(self, now: float, *, emit_events: bool, reason: str) -> None:
        """Clear gesture interpretation without discarding detector tracks."""
        self._finish_swipe(now, emit_events=emit_events, reason=reason)
        self._swipe_state = _SwipeState()
        self._smooth_motion = None
        self._previous_center = None
        self._previous_zoom = None
        self._two_hand_baseline = None
        self._previous_two_hand_span = None
        self._two_hand_intent = _TwoHandIntentState()

    def _gate_hand_count(self, raw_count: int, now: float, *, emit_events: bool) -> tuple[int, bool, str]:
        """Debounce detector hand-count changes before enabling commands.

        A one-frame 2→1 dropout must not become a one-hand command and a
        one-frame 1→2 false positive must not start a two-hand gesture. Every
        proposed transition is neutral until it survives both a sample and
        time gate, followed by stable settle frames.
        """
        state = self._hand_count_state
        raw_count = max(1, int(raw_count))
        if state.stable_count <= 0:
            state.stable_count = raw_count
            state.settle_remaining = 0
            return raw_count, True, "stable"

        if raw_count != state.stable_count:
            if state.candidate_count != raw_count:
                state.candidate_count = raw_count
                state.candidate_since = now
                state.candidate_samples = 1
                self._reset_interaction_phases(
                    now, emit_events=emit_events, reason="hand_count_transition",
                )
            else:
                state.candidate_samples += 1
            confirmed = (
                state.candidate_samples >= max(2, int(self.tuning.hand_count_confirm_samples))
                and now - state.candidate_since >= self.tuning.hand_count_switch_grace
            )
            if confirmed:
                state.stable_count = raw_count
                state.candidate_count = 0
                state.candidate_since = 0.0
                state.candidate_samples = 0
                state.settle_remaining = max(1, int(self.tuning.hand_count_settle_samples))
                self._reset_interaction_phases(
                    now, emit_events=emit_events, reason="hand_count_changed",
                )
                return state.stable_count, False, "mode_changed"
            return state.stable_count, False, "mode_pending"

        if state.candidate_count:
            state.candidate_count = 0
            state.candidate_since = 0.0
            state.candidate_samples = 0
            state.settle_remaining = max(1, int(self.tuning.hand_count_settle_samples))
            self._reset_interaction_phases(
                now, emit_events=emit_events, reason="hand_count_recovered",
            )
            return state.stable_count, False, "mode_recovered"
        if state.settle_remaining > 0:
            state.settle_remaining -= 1
            return state.stable_count, False, "mode_settling"
        return state.stable_count, True, "stable"

    def _update_two_hand_intent(
            self, observed: list[HandMotion], *, span: float, raw_zoom: float,
            now: float, mode_active: bool) -> tuple[str, float]:
        """Recognize only relative two-hand span movement as depth zoom.

        The two-hand contract is intentionally narrow: moving both hands in
        parallel is neutral, while changing the distance between them owns
        zoom. Navigation is handled exclusively by the one-hand recognizer.
        """
        state = self._two_hand_intent
        if state.previous_span is None:
            state.previous_span = span
            state.zoom_anchor = raw_zoom
            self._previous_two_hand_span = span
            return "neutral", 0.0

        span_delta = span - state.previous_span
        state.previous_span = span
        self._previous_two_hand_span = span
        if not mode_active:
            state.phase = "neutral"
            state.accumulated_span = 0.0
            state.zoom_anchor = raw_zoom
            state.zoom_latched = False
            return "neutral", 0.0

        if state.zoom_latched:
            state.phase = "zoom"
            return "zoom", span_delta

        state.accumulated_span += span_delta
        if abs(state.accumulated_span) >= self.tuning.two_hand_zoom_min_travel:
            state.phase = "zoom"
            state.zoom_latched = True
            return "zoom", span_delta

        # Parallel translation preserves span, so it can never claim a
        # two-hand gesture. Keep the original anchor until a real pinch/spread
        # crosses the travel threshold.
        state.phase = "neutral"
        return "neutral", span_delta

    def _finish_swipe(self, now: float, *, emit_events: bool, reason: str = "released") -> str:
        state = self._swipe_state
        direction = state.active_direction
        if not direction:
            return "idle"
        if emit_events:
            self._emit_gesture_event(GestureEvent(
                "swipe", "ended", 0.0, now, direction=direction,
                hand_count=state.active_hand_count,
                source="hand_motion", metadata={"reason": reason},
            ))
        state.active_direction = ""
        state.active_hand_count = 0
        state.inactive_since = 0.0
        state.cooldown_until = now + self.tuning.swipe_cooldown
        state.armed = reason == "released"
        state.rearm_since = 0.0
        state.candidate_direction = ""
        state.candidate_hand_count = 0
        state.candidate_samples = 0
        return "ended"

    def _update_swipe(self, hand: HandMotion | None, now: float, *, hand_count: int,
                      emit_events: bool) -> tuple[float, str, str]:
        state = self._swipe_state
        velocity_x = hand.velocity_x if hand is not None else 0.0
        velocity_y = hand.velocity_y if hand is not None else 0.0
        ratio = self.tuning.swipe_horizontal_ratio
        horizontal = abs(velocity_x) >= ratio * max(0.01, abs(velocity_y))
        vertical = abs(velocity_y) >= ratio * max(0.01, abs(velocity_x))
        if horizontal:
            axis_velocity = velocity_x
            direction = "right" if velocity_x > 0 else "left" if velocity_x < 0 else ""
            coordinate = hand.center_x if hand is not None else 0.0
            previous_coordinate = coordinate - hand.delta_x if hand is not None else coordinate
        elif vertical:
            axis_velocity = velocity_y
            direction = "down" if velocity_y > 0 else "up" if velocity_y < 0 else ""
            coordinate = hand.center_y if hand is not None else 0.0
            previous_coordinate = coordinate - hand.delta_y if hand is not None else coordinate
        else:
            axis_velocity = 0.0
            direction = ""
            coordinate = previous_coordinate = 0.0
        eligible = bool(
            hand is not None
            and not hand.recovered
            and hand.openness >= self.tuning.swipe_min_openness
            and (horizontal or vertical)
        )
        speed = abs(axis_velocity)

        if state.active_direction:
            if state.active_hand_count != hand_count:
                phase = self._finish_swipe(
                    now, emit_events=emit_events, reason="hand_count_changed",
                )
                return 0.0, "", phase
            if eligible and direction == state.active_direction and speed >= self.tuning.swipe_exit_velocity:
                state.inactive_since = 0.0
                if emit_events:
                    self._emit_gesture_event(GestureEvent(
                        "swipe", "updated", speed, now, velocity=axis_velocity,
                        direction=direction, hand_count=hand_count, source="hand_motion",
                    ))
                return axis_velocity, direction, "updated"
            if eligible and direction and direction != state.active_direction and speed >= self.tuning.swipe_enter_velocity:
                phase = self._finish_swipe(now, emit_events=emit_events, reason="direction_changed")
                return 0.0, "", phase
            if state.inactive_since <= 0.0:
                state.inactive_since = now
            if now - state.inactive_since < self.tuning.swipe_release_seconds:
                return 0.0, state.active_direction, "holding"
            phase = self._finish_swipe(now, emit_events=emit_events)
            return 0.0, "", phase

        clearly_released = not eligible or speed < self.tuning.swipe_exit_velocity
        if clearly_released:
            if state.rearm_since <= 0.0:
                state.rearm_since = now
            elif now - state.rearm_since >= self.tuning.swipe_release_seconds:
                state.armed = True
        else:
            state.rearm_since = 0.0

        if (not state.armed or not eligible or speed < self.tuning.swipe_enter_velocity
                or not direction or now < state.cooldown_until):
            state.candidate_direction = ""
            state.candidate_hand_count = 0
            state.candidate_samples = 0
            return 0.0, "", "idle"

        if state.candidate_direction != direction or state.candidate_hand_count != hand_count:
            state.candidate_direction = direction
            state.candidate_hand_count = hand_count
            state.candidate_origin = previous_coordinate
            # Only actual same-direction movement frames count as confirmation;
            # the stationary baseline is deliberately not counted as a sample.
            state.candidate_samples = 1
        else:
            state.candidate_samples += 1
        travel = abs(coordinate - state.candidate_origin)
        confirmed = (
            state.candidate_samples >= max(2, int(self.tuning.swipe_confirm_samples))
            and travel >= self.tuning.swipe_min_travel
        )
        if not confirmed:
            return 0.0, direction, "candidate"

        state.active_direction = direction
        state.active_hand_count = hand_count
        state.armed = False
        state.rearm_since = 0.0
        state.inactive_since = 0.0
        state.candidate_direction = ""
        state.candidate_hand_count = 0
        state.candidate_samples = 0
        if emit_events:
            self._emit_gesture_event(GestureEvent(
                "swipe", "started", speed, now, velocity=axis_velocity,
                direction=direction, hand_count=hand_count, source="hand_motion",
            ))
        return axis_velocity, direction, "started"

    def _motion_sample_many(self, hands, *, handedness=None, timestamp: float | None = None,
                            emit_events: bool = False) -> GestureMotion:
        now = float(timestamp if timestamp is not None else time.monotonic())
        observed = self._observe_hands(hands, handedness, now)
        if not observed:
            raise ValueError("at least one hand is required")

        previous_state = self._tracking_state
        if previous_state == "idle":
            frame_state = "acquired"
        elif previous_state in {"coasting", "lost"}:
            frame_state = "recovered"
        else:
            frame_state = "tracking"
        self._tracking_state = "tracking"
        self._last_hand_seen = now

        if previous_state == "coasting":
            # Even a brief detector dropout invalidates instantaneous velocity
            # and zoom baselines. Keep track identities, but never turn the
            # reacquisition jump into a swipe/zoom command.
            self._reset_interaction_phases(
                now, emit_events=emit_events, reason="tracking_interrupted",
            )
            self._hand_count_state.settle_remaining = max(
                self._hand_count_state.settle_remaining,
                max(1, int(self.tuning.hand_count_settle_samples)),
            )
            observed = [
                HandMotion(
                    item.track_id, item.handedness, item.confidence,
                    item.openness, item.pinch_span, item.center_x, item.center_y,
                    0.0, 0.0, 0.0, 0.0, True,
                )
                for item in observed
            ]
            for item in observed:
                track = self._hand_tracks.get(item.track_id)
                if track is not None:
                    track.velocity_x = 0.0
                    track.velocity_y = 0.0

        gesture_hand_count, mode_active, mode_state = self._gate_hand_count(
            len(observed), now, emit_events=emit_events,
        )
        self._last_mode_active = mode_active
        # Tracking lifecycle is independent from the gesture-mode debounce.
        # In particular, consumers must still see a real ``recovered`` frame
        # after a detector dropout even though commands remain gated while the
        # hand-count mode settles.  ``gesture_mode_active`` carries that gate.
        if mode_state != "stable" and frame_state == "tracking":
            frame_state = mode_state

        primary = max(
            observed,
            key=lambda item: (item.openness, max(abs(item.velocity_x), abs(item.velocity_y))),
        )
        center_x = sum(item.center_x for item in observed) / len(observed)
        center_y = sum(item.center_y for item in observed) / len(observed)
        openness = sum(item.openness for item in observed) / len(observed)
        pinch_span = sum(item.pinch_span for item in observed) / len(observed)
        source = "two_hand" if len(observed) >= 2 else "one_hand"
        two_hand_phase = "neutral"
        if len(observed) >= 2:
            first, second = observed[:2]
            span = math.hypot(first.center_x - second.center_x, first.center_y - second.center_y)
            if self._two_hand_baseline is None or self._previous_zoom is None or self._previous_zoom[0] != source:
                self._two_hand_baseline = max(0.04, span)
            raw_zoom = self._clamp((span - 0.10) / 0.55)
            two_hand_phase, _ = self._update_two_hand_intent(
                observed, span=span, raw_zoom=raw_zoom,
                now=now, mode_active=mode_active and gesture_hand_count == 2,
            )
            two_hand_zoom_active = (
                two_hand_phase == "zoom" or self._two_hand_intent.zoom_latched
            )
            if two_hand_zoom_active:
                zoom = raw_zoom
                zoom_scale = span / max(0.04, self._two_hand_baseline)
            else:
                zoom = self._two_hand_intent.zoom_anchor
                if zoom is None:
                    zoom = raw_zoom
                zoom_scale = 1.0
        else:
            self._two_hand_baseline = None
            self._previous_two_hand_span = None
            self._two_hand_intent = _TwoHandIntentState()
            zoom = self._clamp(openness * 0.72 + pinch_span * 0.28)
            zoom_scale = 1.0
            two_hand_zoom_active = False

        zoom_delta = zoom_velocity = 0.0
        # Depth zoom has exactly one owner: a stable two-hand pinch/spread.
        # A single open palm still publishes diagnostic openness/zoom values,
        # but can never activate the depth transition.
        zoom_enabled = bool(
            mode_active
            and len(observed) == 2
            and gesture_hand_count == 2
            and two_hand_zoom_active
        )
        if zoom_enabled and self._previous_zoom is not None and self._previous_zoom[0] == source:
            _, previous_zoom, previous_time = self._previous_zoom
            dt = now - previous_time
            if 0.0 < dt <= self.tuning.hand_loss_grace:
                zoom_delta = zoom - previous_zoom
                if abs(zoom_delta) < self.tuning.zoom_deadband:
                    zoom_delta = 0.0
                zoom_velocity = zoom_delta / max(1 / 120, dt)
        self._previous_zoom = (source, zoom, now)

        # Navigation has exactly one owner: one stable hand. Two-hand centroid
        # translation is intentionally ignored so it cannot rotate or pan the
        # scene while the user is preparing a pinch/spread.
        swipe_hand = None
        if mode_active and gesture_hand_count == len(observed):
            if len(observed) == 1 and gesture_hand_count == 1:
                swipe_hand = observed[0]
        swipe_velocity, swipe_direction, swipe_phase = self._update_swipe(
            swipe_hand, now, hand_count=gesture_hand_count, emit_events=emit_events,
        )
        # A one-hand swipe cannot coexist with two-hand zoom, but retaining the
        # guard makes hand-count transition frames explicitly neutral.
        swipe_claimed = swipe_phase in {"candidate", "started", "updated"}
        if swipe_claimed:
            zoom_enabled = False
        swipe_active = swipe_phase in {"started", "updated"}
        zoom_active = bool(zoom_enabled and not swipe_active)
        if not zoom_active:
            # Absolute hand openness remains high while an open palm swipes.
            # Publishing that value as an active zoom would make consumers
            # enter the brain portal during carousel rotation.
            zoom_delta = 0.0
            zoom_velocity = 0.0
        if swipe_active:
            gesture_intent = "swipe"
        elif zoom_active:
            gesture_intent = "zoom"
        else:
            gesture_intent = "neutral"
        raw_velocity_x = primary.velocity_x if mode_active else 0.0
        raw_velocity_y = primary.velocity_y if mode_active else 0.0
        raw = GestureMotion(
            openness, pinch_span, zoom, center_x, center_y,
            raw_velocity_x, raw_velocity_y, swipe_velocity, now,
            hand_count=len(observed), zoom_delta=zoom_delta,
            zoom_scale=zoom_scale, swipe_direction=swipe_direction,
            swipe_phase=swipe_phase, tracking_state=frame_state,
            hands=tuple(asdict(item) for item in observed),
            gesture_hand_count=gesture_hand_count,
            gesture_mode_active=mode_active,
            zoom_active=zoom_active,
            gesture_intent=gesture_intent,
        )
        self._previous_center = (center_x, center_y, now)
        if self._smooth_motion is None:
            smoothed = raw
        else:
            alpha = self._clamp(self.tuning.smoothing_alpha)
            old = self._smooth_motion
            smoothed = GestureMotion(
                openness=old.openness + alpha * (raw.openness - old.openness),
                pinch_span=old.pinch_span + alpha * (raw.pinch_span - old.pinch_span),
                zoom=old.zoom + alpha * (raw.zoom - old.zoom),
                center_x=old.center_x + alpha * (raw.center_x - old.center_x),
                center_y=old.center_y + alpha * (raw.center_y - old.center_y),
                velocity_x=old.velocity_x + alpha * (raw.velocity_x - old.velocity_x),
                velocity_y=old.velocity_y + alpha * (raw.velocity_y - old.velocity_y),
                swipe_velocity=raw.swipe_velocity,
                timestamp=now,
                hand_count=raw.hand_count,
                zoom_delta=raw.zoom_delta,
                zoom_scale=raw.zoom_scale,
                swipe_direction=raw.swipe_direction,
                swipe_phase=raw.swipe_phase,
                tracking_state=raw.tracking_state,
                hands=raw.hands,
                gesture_hand_count=raw.gesture_hand_count,
                gesture_mode_active=raw.gesture_mode_active,
                zoom_active=raw.zoom_active,
                gesture_intent=raw.gesture_intent,
            )
        self._smooth_motion = smoothed

        if emit_events:
            if frame_state in {"acquired", "recovered"}:
                self._emit_gesture_event(GestureEvent(
                    "tracking", frame_state, float(len(observed)), now,
                    hand_count=len(observed), source="hand_landmarks",
                ))
            self._emit_gesture_event(GestureEvent(
                "zoom", "updated", smoothed.zoom, now,
                delta=zoom_delta, velocity=zoom_velocity,
                hand_count=len(observed), source=source,
                metadata={
                    "scale": zoom_scale,
                    "two_hand_phase": two_hand_phase,
                    "gesture_hand_count": gesture_hand_count,
                    "mode_active": mode_active,
                    "zoom_active": zoom_active,
                    "intent": gesture_intent,
                },
            ))
        return smoothed

    def _motion_sample(self, points, *, timestamp: float | None = None) -> GestureMotion:
        """Backward-compatible one-hand sampler used by existing integrations."""
        return self._motion_sample_many((points,), timestamp=timestamp, emit_events=False)

    def process_landmarks(self, hands, *, handedness=None, timestamp: float | None = None,
                          emit: bool = True) -> GestureMotion | None:
        """Process detected landmarks without opening a camera.

        This is the deterministic extension/test seam for alternative landmark
        providers. ``hands`` may be multiple hands or one legacy 21-point hand.
        """
        now = float(timestamp if timestamp is not None else time.monotonic())
        hands = list(hands or ())
        if hands and len(hands) == 21 and hasattr(hands[0], "x"):
            hands = [hands]
        hands = hands[:max(1, int(self.tuning.max_hands))]
        if not hands:
            self._handle_hand_loss(now, emit_events=emit)
            return None
        motion = self._motion_sample_many(
            hands, handedness=handedness, timestamp=now, emit_events=emit,
        )
        if emit:
            self._emit_motion(motion)
        # Discrete commands require a stable one-hand mode; a transient 2→1
        # detector dropout is always neutral.
        if (len(hands) == 1 and self._last_mode_active
                and self._hand_count_state.stable_count == 1):
            self._recognize_discrete(hands[0], timestamp=now)
        else:
            self._candidate_gesture = ""
            self._candidate_since = 0.0
            self._last_gesture = ""
            self._last_pose = ""
        return motion

    def _handle_hand_loss(self, now: float, *, emit_events: bool) -> None:
        self._candidate_gesture = ""
        self._candidate_since = 0.0
        self._last_gesture = ""
        self._last_pose = ""
        if self._last_hand_seen is None:
            return
        elapsed = now - self._last_hand_seen
        if elapsed <= self.tuning.hand_loss_grace:
            self._tracking_state = "coasting"
            return
        if self._tracking_state != "lost":
            self._finish_swipe(now, emit_events=emit_events, reason="tracking_lost")
            if emit_events:
                self._emit_gesture_event(GestureEvent(
                    "tracking", "lost", 0.0, now,
                    hand_count=0, source="hand_landmarks",
                ))
                previous = self._smooth_motion
                self._emit_motion(GestureMotion(
                    openness=previous.openness if previous else 0.0,
                    pinch_span=previous.pinch_span if previous else 0.0,
                    zoom=previous.zoom if previous else 0.0,
                    center_x=previous.center_x if previous else 0.5,
                    center_y=previous.center_y if previous else 0.5,
                    velocity_x=0.0,
                    velocity_y=0.0,
                    swipe_velocity=0.0,
                    timestamp=now,
                    hand_count=0,
                    zoom_delta=0.0,
                    zoom_scale=1.0,
                    swipe_direction="",
                    swipe_phase="ended",
                    tracking_state="lost",
                    hands=(),
                    gesture_hand_count=0,
                    gesture_mode_active=False,
                    zoom_active=False,
                    gesture_intent="lost",
                ))
        self._reset_motion_state(tracking_state="lost")

    def _reset_motion_state(self, *, tracking_state: str) -> None:
        self._previous_center = None
        self._smooth_motion = None
        self._hand_tracks.clear()
        self._next_track_number = 1
        self._last_hand_seen = None
        self._previous_zoom = None
        self._two_hand_baseline = None
        self._previous_two_hand_span = None
        self._swipe_state = _SwipeState()
        self._hand_count_state = _HandCountState()
        self._two_hand_intent = _TwoHandIntentState()
        self._last_mode_active = False
        self._last_pose = ""
        self._tracking_state = tracking_state

    def _emit_gesture_event(self, gesture_event: GestureEvent) -> None:
        payload = asdict(gesture_event)
        key = gesture_event.gesture.casefold()
        callbacks = list(self._event_handlers.get(key, ())) + list(self._event_handlers.get("*", ()))
        for action_key in (f"{key}_event", "gesture_event", "event"):
            callback = self.actions.get(action_key)
            if callback is not None and callback not in callbacks:
                callbacks.append(callback)
        for callback in callbacks:
            try:
                callback(payload)
            except Exception as exc:
                get_event_bus().publish(Event(
                    "gesture.callback_failed", "gesture_runtime",
                    data={"gesture": key, "error": f"{type(exc).__name__}: {exc}"},
                ))
        get_event_bus().publish(Event("gesture.event", "gesture_runtime", data=payload))
        get_event_bus().publish(Event(f"gesture.{key}", "gesture_runtime", data=payload))

    def _emit_motion(self, motion: GestureMotion) -> None:
        payload = asdict(motion)
        callback = self.actions.get("motion")
        if callback:
            callback(payload)
        get_event_bus().publish(Event("gesture.motion", "gesture_runtime", data=payload))

    def _recognize_discrete(self, points, *, timestamp: float | None = None) -> None:
        now = float(timestamp if timestamp is not None else time.monotonic())
        matched_recognizer = None
        for recognizer in self._recognizers:
            try:
                matched = bool(recognizer.predicate(points))
            except Exception:
                matched = False
            if matched:
                matched_recognizer = recognizer
                break
        if matched_recognizer is None:
            self._candidate_gesture = ""
            self._candidate_since = 0.0
            self._last_gesture = ""
            self._last_pose = ""
            return

        name = matched_recognizer.name
        if name == self._last_pose:
            return
        if self._candidate_gesture != name:
            self._candidate_gesture = name
            self._candidate_since = now
            return
        if now - self._candidate_since < matched_recognizer.hold_seconds:
            return
        if now < self._cooldown_until.get(name, 0.0):
            return
        self._cooldown_until[name] = now + matched_recognizer.cooldown
        self._candidate_gesture = ""
        self._candidate_since = 0.0
        self._dispatch(matched_recognizer.action or name, pose=name)

    @staticmethod
    def _classify(points) -> str:
        tips = (8, 12, 16, 20)
        pips = (6, 10, 14, 18)
        extended = [points[tip].y < points[pip].y for tip, pip in zip(tips, pips)]
        thumb_up = points[4].y < points[3].y < points[2].y
        if all(extended):
            return "stop_tts"
        if thumb_up and not any(extended):
            return "approve"
        if not thumb_up and not any(extended):
            return "cancel"
        if extended[0] and not any(extended[1:]):
            return "switch_workspace"
        return ""

    def _dispatch(self, gesture: str, *, pose: str = "") -> None:
        self._last_gesture = gesture
        self._last_pose = pose or gesture
        callback = self.actions.get(gesture)
        if callback:
            callback()
        get_event_bus().publish(Event(
            "gesture.recognized", "gesture_runtime",
            data={"gesture": gesture, "pose": pose or gesture},
        ))
