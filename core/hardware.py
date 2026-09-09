import os
import numpy as np
import threading
import time
import queue
import re
import math
import json
import subprocess
from pathlib import Path
from config import Config
from core.assistant_settings import get_assistant_settings


try:
    import sounddevice as sd
    import librosa
    from scipy.signal import resample_poly
    SOUND_AVAILABLE = True
except (ImportError, OSError):
    SOUND_AVAILABLE = False

FasterWhisperModel = None

whisper = None


def _cuda_device_count() -> int:
    """Check NVIDIA devices without importing CTranslate2/PyTorch DLLs."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=4, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return len([line for line in result.stdout.splitlines() if line.strip()]) if result.returncode == 0 else 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return 0


def _load_faster_whisper_class():
    global FasterWhisperModel
    if FasterWhisperModel is None:
        from faster_whisper import WhisperModel
        FasterWhisperModel = WhisperModel
    return FasterWhisperModel


def _load_openai_whisper():
    """Load the heavyweight Torch fallback only after faster-whisper actually failed."""
    global whisper
    if whisper is None:
        try:
            import whisper as whisper_module
        except (ImportError, OSError) as exc:
            raise RuntimeError(f"OpenAI Whisper/Torch fallback을 불러오지 못했습니다: {exc}") from exc
        whisper = whisper_module
    return whisper


class HardwareManager:
    MIN_SPEECH_RMS = 0.0005
    MAX_SPEECH_RMS_THRESHOLD = 0.003

    @staticmethod
    def _record_voice_metric(key: str, value: float, **context) -> None:
        """Record only observed voice events without making audio capture depend on telemetry."""
        try:
            from core.quality_metrics import get_quality_metric_store
            get_quality_metric_store().record(
                key, value, success=value <= 0.0, context=context,
            )
        except Exception:
            pass

    def __init__(self):
        from core.voice_runtime import get_voice_duplex_controller
        self.running = False
        self.wakeword_thread = None
        self.clap_thread = None
        self.last_clap_time = 0
        self.clap_count = 0
        self.continuous_listen_thread = None
        self.audio_queue = queue.Queue()
        self.on_text_detected = None  # 텍스트 감지 시 호출될 콜백
        self.audio_processor = None  # 오디오 프로세서
        self.device = "cpu"
        self.microphone_device = None
        self.microphone_info = None
        self._stream_ready = threading.Event()
        self._stream_error = ""
        self._output_active = threading.Event()
        self._ignore_input_until = 0.0
        self.duplex = get_voice_duplex_controller()
        self._manual_stop = False
        self._recovery_attempts = 0
        self.stt_engine = Config.STT_ENGINE
        self.whisper_model_name = Config.WHISPER_MODEL
        self.whisper_model = None
        self.stt_state = "not_loaded" if SOUND_AVAILABLE else "unavailable"
        self.stt_error = ""
        self._stt_load_lock = threading.Lock()
        self._stt_load_attempt = 0
        self._listener_start_lock = threading.Lock()
        self._closed = False

    def ensure_stt_model(self, *, retry: bool = False):
        """Load only on voice use; concurrent callers share one loading attempt.

        Failure is cached until an explicit retry (e.g. turning the microphone
        on again), so a capture loop cannot repeatedly load a broken model.
        This never opens or records from a microphone.
        """
        if getattr(self, "_closed", False):
            raise RuntimeError("음성 런타임이 종료되어 모델을 시작하지 않습니다.")
        if getattr(self, "whisper_model", None) is not None:
            return self.whisper_model
        if not SOUND_AVAILABLE:
            raise RuntimeError("음성 인식 라이브러리를 사용할 수 없습니다.")
        previous_attempt = self._stt_load_attempt
        with self._stt_load_lock:
            if self.whisper_model is not None:
                return self.whisper_model
            if self.stt_state == "failed" and (not retry or previous_attempt != self._stt_load_attempt):
                raise RuntimeError(self.stt_error)
            self._stt_load_attempt += 1
            self.stt_state = "loading"
            self.stt_error = ""
            self.stt_engine = Config.STT_ENGINE
            self.whisper_model_name = Config.WHISPER_MODEL
            try:
                if self._closed:
                    raise RuntimeError("음성 런타임이 종료되었습니다.")
                requested_device = Config.WHISPER_DEVICE
                self.device = ("cuda" if requested_device in {"auto", "cuda"}
                               and _cuda_device_count() > 0 else "cpu")
                from core.gpu_scheduler import get_gpu_resource_queue
                requested = (3072 if self.whisper_model_name.startswith("large") else 1536) if self.device == "cuda" else 0
                with get_gpu_resource_queue().reserve("stt", requested, priority=1):
                    model = self._load_stt_model()
                if model is None:
                    raise RuntimeError("음성 인식 모델 로더가 모델을 반환하지 않았습니다.")
                if self._closed:
                    raise RuntimeError("음성 런타임이 종료되어 모델 준비를 취소했습니다.")
                self.whisper_model = model
                self.stt_state = "ready"
                return model
            except Exception as exc:
                self.stt_state = "failed"
                self.stt_error = f"STT 모델을 초기화하지 못했습니다: {exc}"
                raise RuntimeError(self.stt_error) from exc

    def _load_stt_model(self):
        if self.stt_engine == "faster-whisper":
            compute_type = Config.WHISPER_COMPUTE_TYPE if self.device == "cuda" else "int8"
            print(
                f"[STT] faster-whisper 모델 로딩: {self.whisper_model_name} "
                f"(device={self.device}, compute={compute_type})"
            )
            try:
                model_class = _load_faster_whisper_class()
                return model_class(
                    self.whisper_model_name,
                    device=self.device,
                    compute_type=compute_type,
                )
            except Exception as exc:
                print(f"[STT] faster-whisper 초기화 실패, OpenAI Whisper fallback: {exc}")
        fallback = _load_openai_whisper()
        self.stt_engine = "openai-whisper"
        self.whisper_model_name = Config.WHISPER_FALLBACK_MODEL
        print(f"[STT] OpenAI Whisper fallback 로딩: {self.whisper_model_name}")
        return fallback.load_model(
            self.whisper_model_name,
            device=self.device,
            download_root=Config.WHISPER_CACHE_DIR,
        )

    def set_output_active(self, active: bool, cooldown: float = 1.2) -> None:
        """TTS 출력이 마이크 명령으로 되먹임되지 않도록 입력 처리를 잠시 멈춥니다."""
        if active:
            self._output_active.set()
            self._ignore_input_until = float("inf")
        else:
            self._ignore_input_until = time.monotonic() + max(0.0, cooldown)
            self._output_active.clear()

    @staticmethod
    def extract_wake_command(text: str) -> str | None:
        """호출어가 첫 단어인 경우에만 뒤따르는 명령을 반환합니다."""
        normalized = text.strip().lower()
        compact_wake_word = get_assistant_settings().wake_word.casefold()
        if normalized.startswith(compact_wake_word) and len(normalized) > len(compact_wake_word):
            remainder = normalized[len(compact_wake_word):].lstrip(" ,.!?，。！？")
            if remainder:
                return remainder
        match = re.match(r"^([^\s]+)(?:\s+(.*))?$", normalized)
        if not match:
            return None
        first_word = re.sub(r"[^0-9a-zA-Z가-힣]", "", match.group(1))
        if first_word != compact_wake_word:
            return None
        return (match.group(2) or "").strip()

    @staticmethod
    def _speech_vocabulary() -> list[str]:
        """앱 Registry의 별칭을 STT 고유명사 힌트로 재사용합니다."""
        vocabulary = []
        data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("app_aliases.json", "user_app_aliases.json"):
            try:
                mapping = json.loads((data_dir / name).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            aliases = [str(key).strip() for key in mapping if str(key).strip()]
            vocabulary.extend(aliases)
        try:
            from core.plugin import get_plugin_registry
            registry = get_plugin_registry()
            registry.load_plugins_from_directory()
            for _plugin, intent in registry.get_all_intents():
                vocabulary.extend(intent.utterance_hints)
                vocabulary.extend(intent.execution_hints)
        except Exception:
            pass
        return list(dict.fromkeys(vocabulary))[:100]

    def _whisper_prompt(self) -> str:
        wake_word = get_assistant_settings().wake_word
        return Config.WHISPER_INITIAL_PROMPT.replace(Config.WAKE_WORD, wake_word)[:300]

    @classmethod
    def _whisper_hotwords(cls, max_characters: int = 240) -> str:
        selected = []
        length = 0
        for word in cls._speech_vocabulary():
            extra = len(word) + (2 if selected else 0)
            if length + extra > max_characters:
                break
            selected.append(word)
            length += extra
        return ", ".join(selected)

    @staticmethod
    def _hangul_jamo(text: str) -> str:
        result = []
        for char in re.sub(r"[^0-9a-zA-Z가-힣]", "", text.casefold()):
            code = ord(char) - 0xAC00
            if 0 <= code < 11172:
                result.append(chr(0x1100 + code // 588))
                result.append(chr(0x1161 + (code % 588) // 28))
                tail = code % 28
                if tail:
                    result.append(chr(0x11A7 + tail))
            else:
                result.append(char)
        return "".join(result)

    @staticmethod
    def _edit_similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        previous = list(range(len(right) + 1))
        for row, left_char in enumerate(left, 1):
            current = [row]
            for column, right_char in enumerate(right, 1):
                current.append(min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                ))
            previous = current
        return 1.0 - previous[-1] / max(len(left), len(right))

    @staticmethod
    def _speech_stem(text: str) -> str:
        normalized = re.sub(r"[^0-9a-zA-Z가-힣]", "", text.casefold())
        for ending in ("해주세요", "해줘요", "해요", "세요", "줘요", "주세요", "요", "어요", "아요"):
            if normalized.endswith(ending) and len(normalized) > len(ending):
                return normalized[:-len(ending)]
        if normalized.endswith("어") and len(normalized) > 1:
            return normalized[:-1]
        return normalized

    @classmethod
    def _correct_registry_command(cls, command: str) -> str:
        """등록된 별칭과 동사 중 음소상 유일하게 가까운 명령만 복원합니다."""
        normalized = command.casefold()
        try:
            from core.plugin import get_plugin_registry
            registry = get_plugin_registry()
            registry.load_plugins_from_directory()
            intents = [
                intent for _plugin, intent in registry.get_all_intents()
                if any(slot.name == "target" for slot in intent.slots)
            ]
        except Exception:
            return command
        if any(
            hint.casefold() in normalized
            for intent in intents for hint in intent.execution_hints
        ):
            return command
        aliases = []
        data_dir = Path(__file__).resolve().parent.parent / "data"
        for name in ("app_aliases.json", "user_app_aliases.json"):
            try:
                aliases.extend(json.loads((data_dir / name).read_text(encoding="utf-8")).keys())
            except (OSError, ValueError, TypeError):
                pass
        alias = next(
            (item for item in sorted(set(aliases), key=len, reverse=True) if item.casefold() in normalized),
            "",
        )
        if not alias:
            return command
        remainder = normalized.split(alias.casefold(), 1)[1]
        spoken = cls._hangul_jamo(cls._speech_stem(remainder))
        scored = []
        for intent in intents:
            for hint in dict.fromkeys([*intent.execution_hints, *intent.utterance_hints]):
                candidate = cls._hangul_jamo(cls._speech_stem(hint))
                score = cls._edit_similarity(spoken, candidate)
                scored.append((score, hint))
        scored.sort(reverse=True)
        if not scored:
            return command
        best_score, best_hint = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if best_score < 0.45 or best_score - second_score < 0.08:
            return command
        corrected = f"{alias} {best_hint}"
        print(
            f"[STT] Registry 음소 보정: {command!r} → {corrected!r} "
            f"(score={best_score:.2f})"
        )
        return corrected

    @staticmethod
    def _normalize_audio(audio: np.ndarray) -> np.ndarray:
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size == 0:
            return audio
        audio = audio - float(np.mean(audio))
        peak = float(np.max(np.abs(audio)))
        if peak <= 1e-6:
            return audio
        gain = min(20.0, 0.25 / peak)
        return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)

    @classmethod
    def _trim_trailing_silence(cls, audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
        """종료 판정을 위해 쌓인 뒤쪽 무음을 제거해 Whisper 환각을 줄입니다."""
        audio = np.asarray(audio, dtype=np.float32)
        frame_size = max(1, int(sample_rate * 0.03))
        active_end = 0
        for start in range(0, len(audio), frame_size):
            frame = audio[start:start + frame_size]
            if frame.size and float(np.sqrt(np.mean(frame * frame))) >= cls.MIN_SPEECH_RMS:
                active_end = min(len(audio), start + frame_size)
        if not active_end:
            return audio[:0]
        return audio[:min(len(audio), active_end + int(sample_rate * 0.2))]

    def _transcribe_audio_unqueued(self, audio: np.ndarray) -> dict:
        self.ensure_stt_model()
        normalized = self._normalize_audio(audio)
        if self.stt_engine == "faster-whisper":
            hotwords = self._whisper_hotwords()
            segments, info = self.whisper_model.transcribe(
                normalized,
                language="ko",
                task="transcribe",
                beam_size=5,
                temperature=0,
                condition_on_previous_text=False,
                initial_prompt=self._whisper_prompt(),
                hotwords=hotwords or None,
                vad_filter=True,
                vad_parameters={
                    "threshold": 0.5,
                    "min_speech_duration_ms": 200,
                    "min_silence_duration_ms": 400,
                    "speech_pad_ms": 150,
                },
                word_timestamps=True,
                hallucination_silence_threshold=1.0,
            )
            segment_list = list(segments)
            text = " ".join(segment.text.strip() for segment in segment_list if segment.text.strip()).strip()
            return {
                "text": text,
                "language": getattr(info, "language", "ko"),
                "language_probability": float(getattr(info, "language_probability", 0.0)),
                "segments": [
                    {
                        "avg_logprob": float(segment.avg_logprob),
                        "no_speech_prob": float(segment.no_speech_prob),
                        "compression_ratio": float(segment.compression_ratio),
                    }
                    for segment in segment_list
                ],
            }
        return self.whisper_model.transcribe(
            normalized,
            language="ko",
            task="transcribe",
            temperature=0,
            beam_size=5,
            condition_on_previous_text=False,
            initial_prompt=self._whisper_prompt(),
            suppress_blank=True,
        )

    def _transcribe_audio(self, audio: np.ndarray) -> dict:
        from core.gpu_scheduler import get_gpu_resource_queue
        self.ensure_stt_model()
        model_name = str(getattr(self, "whisper_model_name", ""))
        requested = (3072 if model_name.startswith("large") else 1536) if getattr(self, "device", "cpu") == "cuda" else 0
        with get_gpu_resource_queue().reserve("stt", requested, priority=1):
            return self._transcribe_audio_unqueued(audio)

    def _trusted_transcription_text(self, result: dict) -> str:
        text = str(result.get("text", "")).strip()
        if not text or self.stt_engine != "faster-whisper":
            return text
        from core.voice_runtime import is_probable_repetition_hallucination
        if is_probable_repetition_hallucination(text):
            print(f"[STT] 반복성 환각 결과 폐기: text={text!r}")
            return ""
        segments = result.get("segments") or []
        if not segments:
            return ""
        avg_logprob = sum(item["avg_logprob"] for item in segments) / len(segments)
        max_no_speech = max(item["no_speech_prob"] for item in segments)
        max_compression = max(item["compression_ratio"] for item in segments)
        if avg_logprob < -1.0 or max_no_speech > 0.65 or max_compression > 2.4:
            print(
                "[STT] 낮은 신뢰도 결과 폐기: "
                f"logprob={avg_logprob:.2f}, no_speech={max_no_speech:.2f}, "
                f"compression={max_compression:.2f}, text={text!r}"
            )
            return ""
        return text

    def _select_wake_candidate(self, result: dict) -> str:
        """전사 후보를 호출어·음향 점수·Registry 어휘로 재평가합니다."""
        from core.voice_runtime import SpeechCandidate, WakeWordCandidateEvaluator
        alternatives = result.get("alternatives") or [result]
        candidates = []
        for item in alternatives:
            segments = item.get("segments") or []
            acoustic = (sum(float(segment.get("avg_logprob", 0.0)) for segment in segments) /
                        len(segments)) if segments else 0.0
            no_speech = max((float(segment.get("no_speech_prob", 0.0)) for segment in segments), default=0.0)
            candidates.append(SpeechCandidate(str(item.get("text", "")), acoustic, no_speech))
        return WakeWordCandidateEvaluator(get_assistant_settings().wake_word).select(candidates, self._speech_vocabulary()) or ""

    @staticmethod
    def list_input_devices():
        if not SOUND_AVAILABLE:
            return []
        return [dict(device) for device in sd.query_devices() if device["max_input_channels"] > 0]

    def _select_microphone(self):
        devices = self.list_input_devices()
        if not devices:
            raise RuntimeError("사용 가능한 마이크 입력 장치가 없습니다.")
        requested = str(Config.MICROPHONE_DEVICE).strip()
        selected = None
        if requested and requested.casefold() != "auto":
            if requested.isdigit():
                selected = next((item for item in devices if item["index"] == int(requested)), None)
            else:
                selected = next((item for item in devices if requested.casefold() in item["name"].casefold()), None)
            if selected is None:
                raise RuntimeError(f"설정한 마이크를 찾을 수 없습니다: {requested}")
        if selected is None:
            default_index = sd.default.device[0]
            selected = next((item for item in devices if item["index"] == default_index), devices[0])
        sample_rate = int(selected["default_samplerate"] or 16000)
        sd.check_input_settings(device=selected["index"], channels=1, samplerate=sample_rate, dtype="float32")
        self.microphone_device, self.microphone_info = selected["index"], selected
        return selected, sample_rate

    @staticmethod
    def _to_16khz(audio: np.ndarray, source_rate: int) -> np.ndarray:
        if source_rate == 16000:
            return audio.astype(np.float32, copy=False)
        divisor = math.gcd(int(source_rate), 16000)
        converted = resample_poly(audio, 16000 // divisor, int(source_rate) // divisor)
        return converted.astype(np.float32, copy=False)

    def start_continuous_listen(self, on_text_callback, audio_processor=None) -> str:
        """호출어로 시작하는 음성 명령을 지속적으로 감지합니다."""
        with self._listener_start_lock:
            return self._start_continuous_listen_locked(on_text_callback, audio_processor)

    def _start_continuous_listen_locked(self, on_text_callback, audio_processor=None) -> str:
        if not SOUND_AVAILABLE:
            return "오류: sounddevice, whisper가 설치되지 않았습니다."
        if self.running:
            return "이미 음성 감지가 실행 중입니다."
        self._manual_stop = False
        try:
            self.ensure_stt_model(retry=True)
        except RuntimeError as exc:
            return f"오류: {exc} 텍스트 입력은 계속 사용할 수 있습니다."
        if self._manual_stop:
            return "음성 감지 시작을 취소했습니다."
        self.running = True
        self._recovery_attempts = getattr(self, "_recovery_attempts", 0)
        if not hasattr(self, "duplex"):
            from core.voice_runtime import get_voice_duplex_controller
            self.duplex = get_voice_duplex_controller()
        self.on_text_detected = on_text_callback
        self.audio_processor = audio_processor
        self._stream_ready.clear()
        self._stream_error = ""
        
        def continuous_detect():
            try:
                info, native_rate = self._select_microphone()
                chunk_size = max(256, int(native_rate * 0.1))
                print(f"[마이크] 입력 장치 연결: {info['name']} (index={info['index']}, {native_rate}Hz)")
                with sd.InputStream(device=info["index"], samplerate=native_rate, channels=1,
                                    dtype="float32", blocksize=chunk_size) as stream:
                    self._stream_ready.set()
                    wake_buffer = np.array([], dtype=np.float32)
                    command_buffer = np.array([], dtype=np.float32)
                    command_wake_audio = np.array([], dtype=np.float32)
                    command_prefix = ""
                    listening = False
                    silence_started = None
                    listening_started = None
                    last_wake_check = 0.0
                    noise_samples = []
                    speech_threshold = self.MIN_SPEECH_RMS
                    wake_voice_chunks = []
                    voiced_seconds = 0.0
                    while self.running:
                        chunk, overflowed = stream.read(chunk_size)
                        chunk = chunk[:, 0].copy()
                        if overflowed:
                            print("[마이크] 입력 버퍼 overflow 감지")
                        rms = float(np.sqrt(np.mean(chunk * chunk)))
                        if self._output_active.is_set():
                            residual = self.duplex.suppress_echo(chunk)
                            residual_rms = float(np.sqrt(np.mean(residual * residual))) if residual.size else 0.0
                            if self.duplex.observe_input(residual_rms):
                                print("[음성] 사용자 끼어들기 감지: TTS 재생 취소")
                            wake_buffer = np.array([], dtype=np.float32)
                            command_buffer = np.array([], dtype=np.float32)
                            continue
                        if time.monotonic() < self._ignore_input_until:
                            wake_buffer = np.array([], dtype=np.float32)
                            command_buffer = np.array([], dtype=np.float32)
                            command_wake_audio = np.array([], dtype=np.float32)
                            command_prefix = ""
                            listening = False
                            silence_started = None
                            listening_started = None
                            wake_voice_chunks = []
                            voiced_seconds = 0.0
                            continue
                        if len(noise_samples) < 10:
                            noise_samples.append(rms)
                            if len(noise_samples) == 10:
                                speech_threshold = max(
                                    self.MIN_SPEECH_RMS,
                                    min(
                                        self.MAX_SPEECH_RMS_THRESHOLD,
                                        float(np.percentile(noise_samples, 20)) * 3,
                                    ),
                                )
                                print(f"[마이크] 자동 음성 임계값: {speech_threshold:.6f}")
                        if self.audio_processor:
                            amplitude, bands = self.audio_processor._analyze_audio(chunk, native_rate)
                            self.audio_processor.audio_update.emit(amplitude, bands, False)
                        audio16 = self._to_16khz(chunk, native_rate)
                        if not listening:
                            now = time.monotonic()
                            wake_buffer = np.concatenate((wake_buffer, audio16))[-32000:]
                            wake_voice_chunks.append(rms >= speech_threshold)
                            wake_voice_chunks = wake_voice_chunks[-20:]
                            if len(wake_buffer) >= 32000 and now - last_wake_check >= 1.5:
                                last_wake_check = now
                                if sum(wake_voice_chunks) < 2:
                                    continue
                                wake_result = self._transcribe_audio(wake_buffer)
                                text = (self._select_wake_candidate(wake_result) or
                                        self._trusted_transcription_text(wake_result)).lower()
                                wake_command = self.extract_wake_command(text)
                                if wake_command is not None:
                                    print(f"[웨이크워드] 감지: {text}")
                                    trailing_silence = 0
                                    for active in reversed(wake_voice_chunks):
                                        if active:
                                            break
                                        trailing_silence += 1
                                    if wake_command and trailing_silence >= 5:
                                        wake_command = self._correct_registry_command(wake_command)
                                        print(f"[전송] 호출어 포함 음성 명령: {wake_command}")
                                        if self.on_text_detected:
                                            self._record_voice_metric(
                                                "stt_false_wake", 0.0,
                                                event="wake_command_dispatched", text=wake_command[:120],
                                            )
                                            self._record_voice_metric(
                                                "stt_echo", 0.0,
                                                event="command_dispatched_outside_tts",
                                            )
                                            self.on_text_detected(wake_command)
                                        wake_buffer = np.array([], dtype=np.float32)
                                        wake_voice_chunks = []
                                        continue
                                    listening = True
                                    command_prefix = wake_command
                                    command_buffer = np.array([], dtype=np.float32)
                                    command_wake_audio = wake_buffer.copy()
                                    listening_started = now
                                    silence_started = None
                                    voiced_seconds = 0.0
                                    wake_buffer = np.array([], dtype=np.float32)
                                    wake_voice_chunks = []
                                elif text:
                                    print(f"[마이크] 첫 단어가 호출어가 아니어서 무시: {text}")
                        else:
                            command_buffer = np.concatenate((command_buffer, audio16))
                            if rms >= speech_threshold:
                                silence_started = None
                                voiced_seconds += len(chunk) / native_rate
                            elif silence_started is None:
                                silence_started = time.monotonic()
                            now = time.monotonic()
                            silent_long_enough = (
                                silence_started is not None
                                and now - silence_started >= Config.MICROPHONE_SILENCE_SECONDS
                            )
                            command_timed_out = (
                                listening_started is not None
                                and now - listening_started >= Config.MICROPHONE_MAX_COMMAND_SECONDS
                            )
                            if (silent_long_enough or command_timed_out) and len(command_buffer) >= 4800:
                                reason = (
                                    f"{Config.MICROPHONE_SILENCE_SECONDS:g}초 무음"
                                    if silent_long_enough else "최대 발화 시간"
                                )
                                print(f"[마이크] 명령 종료 감지: {reason}")
                                if not command_prefix and voiced_seconds < 0.25:
                                    print("[마이크] 실제 발화가 없는 입력을 폐기했습니다.")
                                    text = ""
                                else:
                                    full_utterance = np.concatenate((command_wake_audio, command_buffer))
                                    full_utterance = self._trim_trailing_silence(full_utterance)
                                    transcription = (
                                        self._trusted_transcription_text(
                                            self._transcribe_audio(full_utterance)
                                        ) if full_utterance.size else ""
                                    )
                                    recovered = self.extract_wake_command(transcription)
                                    text = recovered if recovered is not None else command_prefix
                                    if text:
                                        text = self._correct_registry_command(text)
                                if text and self.on_text_detected:
                                    print(f"[전송] 음성 인식 결과: {text}")
                                    self._record_voice_metric(
                                        "stt_false_wake", 0.0,
                                        event="wake_command_dispatched", text=text[:120],
                                    )
                                    self._record_voice_metric(
                                        "stt_echo", 0.0,
                                        event="command_dispatched_outside_tts",
                                    )
                                    self.on_text_detected(text)
                                elif not text:
                                    self._record_voice_metric(
                                        "stt_false_wake", 1.0,
                                        event="wake_accepted_without_command",
                                        voiced_seconds=round(voiced_seconds, 3),
                                    )
                                listening = False
                                command_prefix = ""
                                command_buffer = np.array([], dtype=np.float32)
                                command_wake_audio = np.array([], dtype=np.float32)
                                silence_started = None
                                listening_started = None
                                voiced_seconds = 0.0
            except Exception as exc:
                self._stream_error = str(exc)
                should_recover = self.running and self._stream_ready.is_set() and not self._manual_stop
                self.running = False
                self._stream_ready.set()
                print(f"[마이크] 입력 장치 오류: {exc}")
                if should_recover and self._recovery_attempts < 5:
                    self._recovery_attempts += 1
                    delay = min(4.0, 0.25 * (2 ** (self._recovery_attempts - 1)))
                    def recover():
                        time.sleep(delay)
                        if not self._manual_stop and not self.running:
                            print("[마이크] 장치 변경·절전 복귀 자동 재연결 시도")
                            print(self.start_continuous_listen(self.on_text_detected, self.audio_processor))
                    threading.Thread(target=recover, daemon=True).start()
        
        self.continuous_listen_thread = threading.Thread(target=continuous_detect, daemon=True)
        self.continuous_listen_thread.start()
        if not self._stream_ready.wait(timeout=5):
            self.running = False
            return "오류: 마이크 입력 장치 초기화 시간이 초과되었습니다."
        if self._stream_error:
            return f"오류: 마이크 입력 장치를 열 수 없습니다: {self._stream_error}"
        wake_word = get_assistant_settings().wake_word
        return (f"[성공] 마이크 연결: {self.microphone_info['name']} "
                f"(장치 {self.microphone_device}). '{wake_word}'라고 불러주세요.")

    def stop_continuous_listen(self) -> str:
        # Also cancel a start that is still loading, before any stream exists.
        self._manual_stop = True
        if not self.running:
            return "음성 감지가 실행 중이지 않습니다."
        
        self._manual_stop = True
        self.running = False
        if self.continuous_listen_thread:
            self.continuous_listen_thread.join(timeout=2)
        
        return "[성공] 음성 감지가 중지되었습니다."

    def start_wakeword_detection(self) -> str:
        with self._listener_start_lock:
            return self._start_wakeword_detection_locked()

    def _start_wakeword_detection_locked(self) -> str:
        if not SOUND_AVAILABLE:
            return "오류: sounddevice, whisper가 설치되지 않았습니다."
        if self.running:
            return "웨이크워드 감지가 이미 실행 중입니다."
        self._manual_stop = False
        try:
            self.ensure_stt_model(retry=True)
        except RuntimeError as exc:
            return f"오류: {exc}"
        if self._manual_stop:
            return "웨이크워드 감지 시작을 취소했습니다."
        self.running = True
        
        def detect_wakeword():
            print(f"[마이크] 웨이크워드 감지 시작... '{get_assistant_settings().wake_word}' 라고 말하세요!")
            info, native_rate = self._select_microphone()
            while self.running:
                try:
                    duration = 2
                    recording = sd.rec(
                        int(duration * native_rate), samplerate=native_rate, channels=1,
                        dtype="float32", device=info["index"]
                    )
                    sd.wait()
                    audio = self._to_16khz(recording[:, 0], native_rate)
                    result = self._transcribe_audio(audio)
                    text = self._trusted_transcription_text(result).lower()
                    
                    if self.extract_wake_command(text) is not None:
                        print(f"\n[웨이크워드] 감지! '{text}'")
                        self._on_wakeword_detected()
                
                except Exception as e:
                    print(f"웨이크워드 감지 오류: {e}")
                    time.sleep(1)
        
        self.wakeword_thread = threading.Thread(target=detect_wakeword, daemon=True)
        self.wakeword_thread.start()
        
        return f"[성공] 웨이크워드 감지가 시작되었습니다! '{get_assistant_settings().wake_word}' 라고 말해보세요."

    def _on_wakeword_detected(self):
        print(f"[{get_assistant_settings().assistant_name}] 네? 어떤 도움이 필요하신가요?")

    def stop_wakeword_detection(self) -> str:
        self._manual_stop = True
        if not self.running:
            return "웨이크워드 감지가 실행 중이지 않습니다."
        
        self.running = False
        if self.wakeword_thread:
            self.wakeword_thread.join(timeout=2)
        
        return "[성공] 웨이크워드 감지가 중지되었습니다."

    def start_clap_detection(self) -> str:
        if getattr(self, "_closed", False):
            return "오류: 음성 런타임이 종료되었습니다."
        if not SOUND_AVAILABLE:
            return "오류: sounddevice, librosa가 설치되지 않았습니다."
        
        if self.running:
            return "박수 감지가 이미 실행 중입니다."
        
        self.running = True
        self.clap_count = 0
        self.last_clap_time = 0
        
        def detect_clap():
            print("[마이크] 박수 감지 시작... 박수를 쳐보세요!")
            info, native_rate = self._select_microphone()
            
            def audio_callback(indata, frames, callback_time, status):
                if status:
                    print(status)
                
                audio = indata[:, 0]
                energy = np.sum(audio ** 2) / len(audio)
                energy_db = 10 * np.log10(energy + 1e-10)
                
                if energy_db > -10:
                    current_time = time.time()
                    if current_time - self.last_clap_time > 0.3:
                        self.clap_count += 1
                        self.last_clap_time = current_time
                        print(f"[마이크] 박수 감지 (총 {self.clap_count}번)")
                        
                        if self.clap_count >= 2:
                            self._on_clap_detected()
                            self.clap_count = 0
            
            with sd.InputStream(
                callback=audio_callback, channels=1, samplerate=native_rate,
                device=info["index"], dtype="float32"
            ):
                while self.running:
                    time.sleep(0.1)
        
        self.clap_thread = threading.Thread(target=detect_clap, daemon=True)
        self.clap_thread.start()
        
        return "✅ 박수 감지가 시작되었습니다! 두 번 박수를 쳐보세요."

    def _on_clap_detected(self):
        print("[마이크] 두 번 박수 감지")

    def stop_clap_detection(self) -> str:
        if not self.running:
            return "박수 감지가 실행 중이지 않습니다."
        
        self.running = False
        if self.clap_thread:
            self.clap_thread.join(timeout=2)
        
        return "✅ 박수 감지가 중지되었습니다."

    def shutdown(self) -> None:
        """Do not let a late model loader reopen input after the app closes."""
        self._closed = True
        self._manual_stop = True
        self.running = False
        for worker in (self.continuous_listen_thread, self.wakeword_thread, self.clap_thread):
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=2)


# Singleton instance
_hardware_manager = None


def get_hardware_manager() -> HardwareManager:
    global _hardware_manager
    if _hardware_manager is None:
        _hardware_manager = HardwareManager()
    return _hardware_manager
