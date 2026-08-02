"""Wake-word candidate ranking, duplex echo suppression and device recovery policy."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable, Iterable, Optional


@dataclass(frozen=True)
class SpeechCandidate:
    text: str
    acoustic_score: float = 0.0
    no_speech_probability: float = 0.0


class WakeWordCandidateEvaluator:
    def __init__(self, wake_word: str):
        self.wake_word = re.sub(r"\W", "", wake_word.casefold())

    def rank(self, candidates: Iterable[SpeechCandidate], vocabulary: Iterable[str] = ()) -> list[SpeechCandidate]:
        vocabulary = tuple(str(item).casefold() for item in vocabulary)

        def score(candidate: SpeechCandidate):
            words = candidate.text.strip().casefold().split()
            first = re.sub(r"\W", "", words[0]) if words else ""
            wake = SequenceMatcher(None, first, self.wake_word).ratio()
            context = sum(1 for item in vocabulary if item and item in candidate.text.casefold())
            return wake * 4 + candidate.acoustic_score - candidate.no_speech_probability * 2 + min(context, 3) * 0.2

        return sorted(candidates, key=score, reverse=True)

    def select(self, candidates: Iterable[SpeechCandidate], vocabulary: Iterable[str] = ()) -> Optional[str]:
        ranked = self.rank(candidates, vocabulary)
        if not ranked:
            return None
        words = ranked[0].text.strip().split(maxsplit=1)
        if not words:
            return None
        similarity = SequenceMatcher(None, re.sub(r"\W", "", words[0].casefold()), self.wake_word).ratio()
        return ranked[0].text if similarity >= 0.72 else None


class VoiceDuplexController:
    """Tracks TTS reference energy and detects sustained near-end speech for barge-in."""

    def __init__(self, interrupt_callback: Optional[Callable[[], None]] = None):
        self.interrupt_callback = interrupt_callback
        self.output_active = threading.Event()
        self.cancel_event = threading.Event()
        self._reference_rms = 0.0
        self._near_end_started: Optional[float] = None
        self._lock = threading.Lock()

    def start_output(self) -> None:
        with self._lock:
            self.cancel_event.clear()
            self._near_end_started = None
            self.output_active.set()

    def update_output(self, rms: float) -> None:
        with self._lock:
            measured = max(0.0, float(rms))
            self._reference_rms = measured if self._reference_rms == 0.0 else self._reference_rms * 0.8 + measured * 0.2

    def observe_input(self, rms: float, now: Optional[float] = None) -> bool:
        if not self.output_active.is_set():
            return False
        now = time.monotonic() if now is None else now
        # Require input well above both the floor and estimated speaker leakage.
        threshold = max(0.008, self._reference_rms * 1.8)
        with self._lock:
            if rms >= threshold:
                self._near_end_started = self._near_end_started or now
                if now - self._near_end_started >= 0.25:
                    self.cancel()
                    return True
            else:
                self._near_end_started = None
        return False

    def cancel(self) -> None:
        self.cancel_event.set()
        callback = self.interrupt_callback
        if callback:
            callback()

    def finish_output(self) -> None:
        with self._lock:
            self.output_active.clear()
            self._reference_rms = 0.0
            self._near_end_started = None


class DeviceRecoveryPolicy:
    def __init__(self, max_attempts: int = 5, base_delay: float = 0.25):
        self.max_attempts, self.base_delay = max(1, max_attempts), max(0.0, base_delay)

    def run(self, connect: Callable[[], object], *, sleep: Callable[[float], None] = time.sleep):
        last_error = None
        for attempt in range(self.max_attempts):
            try:
                return connect()
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    sleep(min(4.0, self.base_delay * (2 ** attempt)))
        raise RuntimeError(f"장치 자동 복구 실패 ({self.max_attempts}회): {last_error}") from last_error


_duplex = None


def get_voice_duplex_controller() -> VoiceDuplexController:
    global _duplex
    if _duplex is None:
        _duplex = VoiceDuplexController()
    return _duplex
