"""Privacy-first, extensible continuous hand-gesture runtime.

Frames are processed locally and discarded immediately. The runtime publishes
discrete actions and smoothed motion samples so the UI can respond to hand
openness and swipe speed without hard-coding every future gesture in the loop.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional
import math
import os
import threading
import time
import urllib.request

from core.runtime.event_bus import Event, get_event_bus


@dataclass(frozen=True)
class GestureStatus:
    enabled: bool
    running: bool
    camera_index: int
    backend: str
    last_gesture: str = ""
    error: str = ""
    privacy: str = "local-only; frames are not stored"


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


@dataclass
class GestureRecognizer:
    name: str
    predicate: Callable[[object], bool]
    cooldown: float = 1.5
    hold_seconds: float = 0.8


class GestureRuntime:
    """Run MediaPipe lazily and expose continuous, device-independent motion."""

    MODEL_URL = (
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
        "hand_landmarker/float16/1/hand_landmarker.task"
    )

    def __init__(self, *, camera_index: int = 0, actions: dict[str, Callable] | None = None,
                 model_path: str | None = None, enable_command_gestures: bool = False):
        self.camera_index = int(camera_index)
        self.actions = dict(actions or {})
        configured = model_path or os.getenv("JARVIS_HAND_LANDMARKER_MODEL", "")
        self.model_path = Path(configured or "data/models/mediapipe/hand_landmarker.task")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_gesture = ""
        self._error = ""
        self._cooldown_until: dict[str, float] = {}
        self._ready = threading.Event()
        self._previous_center: tuple[float, float, float] | None = None
        self._smooth_motion: GestureMotion | None = None
        self._recognizers: list[GestureRecognizer] = []
        self._candidate_gesture = ""
        self._candidate_since = 0.0
        if enable_command_gestures:
            self._register_builtin_gestures()

    def _register_builtin_gestures(self) -> None:
        self.register_gesture("stop_tts", lambda points: self._classify(points) == "stop_tts")
        self.register_gesture("approve", lambda points: self._classify(points) == "approve")
        self.register_gesture("cancel", lambda points: self._classify(points) == "cancel")

    def register_gesture(self, name: str, predicate: Callable[[object], bool], *, cooldown: float = 1.5,
                         hold_seconds: float = 0.8, callback: Callable | None = None) -> None:
        """Register an opt-in command gesture with a stable-hold safety gate."""
        self._recognizers = [item for item in self._recognizers if item.name != name]
        self._recognizers.append(GestureRecognizer(
            str(name), predicate, max(0.0, float(cooldown)),
            max(0.0, float(hold_seconds)),
        ))
        if callback is not None:
            self.actions[str(name)] = callback

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
        self._previous_center = None
        self._smooth_motion = None
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
                running_mode=RunningMode.VIDEO, num_hands=1,
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
                    points = result.hand_landmarks[0]
                    self._emit_motion(self._motion_sample(points))
                    self._recognize_discrete(points)
                else:
                    self._previous_center = None
                    self._candidate_gesture = ""
                    self._candidate_since = 0.0
                    self._last_gesture = ""
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

    def _motion_sample(self, points, *, timestamp: float | None = None) -> GestureMotion:
        now = float(timestamp if timestamp is not None else time.monotonic())
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
        zoom = min(1.0, max(0.0, openness * 0.72 + pinch_span * 0.28))

        velocity_x = velocity_y = 0.0
        if self._previous_center is not None:
            previous_x, previous_y, previous_time = self._previous_center
            dt = max(1 / 120, min(0.25, now - previous_time))
            velocity_x = (center_x - previous_x) / dt
            velocity_y = (center_y - previous_y) / dt
        self._previous_center = (center_x, center_y, now)
        swipe_velocity = velocity_x if openness >= 0.58 and abs(velocity_x) >= 0.28 else 0.0
        raw = GestureMotion(openness, pinch_span, zoom, center_x, center_y,
                            velocity_x, velocity_y, swipe_velocity, now)
        if self._smooth_motion is None:
            self._smooth_motion = raw
            return raw
        alpha = 0.28
        old = self._smooth_motion
        old_values = (old.openness, old.pinch_span, old.zoom, old.center_x, old.center_y,
                      old.velocity_x, old.velocity_y, old.swipe_velocity)
        new_values = (raw.openness, raw.pinch_span, raw.zoom, raw.center_x, raw.center_y,
                      raw.velocity_x, raw.velocity_y, raw.swipe_velocity)
        values = [previous + alpha * (current - previous)
                  for previous, current in zip(old_values, new_values)]
        smoothed = GestureMotion(*values, now)
        self._smooth_motion = smoothed
        return smoothed

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
            return

        name = matched_recognizer.name
        if name == self._last_gesture:
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
        self._dispatch(name)

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

    def _dispatch(self, gesture: str) -> None:
        self._last_gesture = gesture
        callback = self.actions.get(gesture)
        if callback:
            callback()
        get_event_bus().publish(Event("gesture.recognized", "gesture_runtime", data={"gesture": gesture}))
