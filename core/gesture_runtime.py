"""Optional, privacy-first hand gesture adapter for explicit UI control."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
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


class GestureRuntime:
    """Camera stays off until ``start`` is explicitly called.

    MediaPipe is imported lazily. Open palm stops TTS, thumbs-up approves,
    a fist cancels, and an index-only gesture requests workspace switching.
    """

    MODEL_URL = (
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
        "hand_landmarker/float16/1/hand_landmarker.task"
    )

    def __init__(self, *, camera_index: int = 0, actions: dict[str, Callable[[], None]] | None = None,
                 model_path: str | None = None):
        self.camera_index = int(camera_index)
        self.actions = dict(actions or {})
        configured = model_path or os.getenv("JARVIS_HAND_LANDMARKER_MODEL", "")
        self.model_path = Path(configured or "data/models/mediapipe/hand_landmarker.task")
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_gesture = ""
        self._error = ""
        self._cooldown_until = 0.0

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
            return {"available": False, "backend": "2D UI fallback", "error": f"{type(exc).__name__}: {exc}"}

    def ensure_model(self) -> Path:
        """Install the official hand landmark model only after explicit opt-in."""
        if self.model_path.is_file() and self.model_path.stat().st_size > 1_000_000:
            return self.model_path
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.model_path.with_suffix(".download")
        try:
            with urllib.request.urlopen(self.MODEL_URL, timeout=60) as response, temporary.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
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
        return GestureStatus(bool(dependency["available"]), self._running, self.camera_index,
                             dependency["backend"], self._last_gesture,
                             self._error or dependency.get("error", "")).__dict__

    def start(self) -> GestureStatus:
        if self._running:
            return GestureStatus(**self.status())
        dependency = self.dependency_status()
        if not dependency["available"]:
            raise RuntimeError("제스처 선택 기능에는 optional MediaPipe 패키지가 필요합니다.")
        try:
            self.ensure_model()
        except Exception as exc:
            raise RuntimeError(f"손 인식 모델을 준비하지 못했습니다: {exc}") from exc
        self._running = True
        self._error = ""
        self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-gesture")
        self._thread.start()
        get_event_bus().publish(Event("gesture.started", "gesture_runtime", data={"camera": self.camera_index}))
        return GestureStatus(**self.status())

    def stop(self) -> GestureStatus:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        get_event_bus().publish(Event("gesture.stopped", "gesture_runtime", data={}))
        return GestureStatus(**self.status())

    def _loop(self) -> None:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python.vision import (
            HandLandmarker, HandLandmarkerOptions, RunningMode,
        )
        capture = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        hands = HandLandmarker.create_from_options(HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.model_path.resolve())),
            running_mode=RunningMode.VIDEO, num_hands=1,
            min_hand_detection_confidence=0.7,
            min_hand_presence_confidence=0.65,
            min_tracking_confidence=0.65,
        ))
        timestamp_ms = 0
        try:
            if not capture.isOpened():
                raise RuntimeError(f"카메라 {self.camera_index}를 열 수 없습니다.")
            while self._running:
                ok, frame = capture.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                timestamp_ms = max(timestamp_ms + 1, int(time.monotonic() * 1000))
                result = hands.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp_ms,
                )
                if result.hand_landmarks:
                    gesture = self._classify(result.hand_landmarks[0])
                    if gesture and gesture != self._last_gesture and time.monotonic() >= self._cooldown_until:
                        self._dispatch(gesture)
                time.sleep(0.025)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            get_event_bus().publish(Event("gesture.failed", "gesture_runtime", data={"error": self._error}))
        finally:
            self._running = False
            hands.close()
            capture.release()

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
        self._cooldown_until = time.monotonic() + 1.5
        callback = self.actions.get(gesture)
        if callback:
            callback()
        get_event_bus().publish(Event("gesture.recognized", "gesture_runtime", data={"gesture": gesture}))
