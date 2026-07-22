import os
import numpy as np
import threading
import time
import queue
import re
import math
import json
from pathlib import Path
from config import Config


try:
    import sounddevice as sd
    import whisper
    import librosa
    import torch
    from scipy.signal import resample_poly
    SOUND_AVAILABLE = True
except ImportError:
    SOUND_AVAILABLE = False


class HardwareManager:
    MIN_SPEECH_RMS = 0.0005
    MAX_SPEECH_RMS_THRESHOLD = 0.003

    def __init__(self):
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
        
        if SOUND_AVAILABLE:
            # GPU 사용 가능 여부 확인
            requested_device = Config.WHISPER_DEVICE
            if requested_device == "auto":
                requested_device = "cuda" if torch.cuda.is_available() else "cpu"
            if requested_device == "cuda" and torch.cuda.is_available():
                self.device = "cuda"
                print(f"[GPU] CUDA를 사용합니다! (GPU: {torch.cuda.get_device_name(0)})")
            elif requested_device == "mps" and torch.backends.mps.is_available():
                self.device = "mps"
                print("[GPU] Apple Silicon MPS를 사용합니다!")
            else:
                self.device = "cpu"
                print("[CPU] GPU를 사용할 수 없어 CPU를 사용합니다.")
            
            self.whisper_model_name = Config.WHISPER_MODEL
            print(f"[STT] Whisper 모델 로딩: {self.whisper_model_name}")
            self.whisper_model = whisper.load_model(self.whisper_model_name, device=self.device)

    def set_output_active(self, active: bool, cooldown: float = 0.5) -> None:
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
        compact_wake_word = Config.WAKE_WORD.casefold()
        if normalized.startswith(compact_wake_word) and len(normalized) > len(compact_wake_word):
            remainder = normalized[len(compact_wake_word):].lstrip(" ,.!?，。！？")
            if remainder:
                return remainder
        match = re.match(r"^([^\s]+)(?:\s+(.*))?$", normalized)
        if not match:
            return None
        first_word = re.sub(r"[^0-9a-zA-Z가-힣]", "", match.group(1))
        if first_word != Config.WAKE_WORD:
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
            for _plugin, intent in get_plugin_registry().get_all_intents():
                vocabulary.extend(intent.utterance_hints)
                vocabulary.extend(intent.execution_hints)
        except Exception:
            pass
        return list(dict.fromkeys(vocabulary))[:100]

    def _whisper_prompt(self) -> str:
        vocabulary = self._speech_vocabulary()
        if not vocabulary:
            return Config.WHISPER_INITIAL_PROMPT
        return (
            f"{Config.WHISPER_INITIAL_PROMPT} "
            f"프로그램 이름과 사용자 별칭: {', '.join(vocabulary)}."
        )

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

    def _transcribe_audio(self, audio: np.ndarray) -> dict:
        return self.whisper_model.transcribe(
            self._normalize_audio(audio),
            language="ko",
            task="transcribe",
            temperature=0,
            beam_size=5,
            condition_on_previous_text=False,
            initial_prompt=self._whisper_prompt(),
            suppress_blank=True,
        )

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
        if not SOUND_AVAILABLE:
            return "오류: sounddevice, whisper가 설치되지 않았습니다."
        
        if self.running:
            return "이미 음성 감지가 실행 중입니다."
        
        self.running = True
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
                        if self._output_active.is_set() or time.monotonic() < self._ignore_input_until:
                            wake_buffer = np.array([], dtype=np.float32)
                            command_buffer = np.array([], dtype=np.float32)
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
                                text = self._transcribe_audio(wake_buffer)["text"].strip().lower()
                                wake_command = self.extract_wake_command(text)
                                if wake_command is not None:
                                    print(f"[웨이크워드] 감지: {text}")
                                    trailing_silence = 0
                                    for active in reversed(wake_voice_chunks):
                                        if active:
                                            break
                                        trailing_silence += 1
                                    if wake_command and trailing_silence >= 5:
                                        print(f"[전송] 호출어 포함 음성 명령: {wake_command}")
                                        if self.on_text_detected:
                                            self.on_text_detected(wake_command)
                                        wake_buffer = np.array([], dtype=np.float32)
                                        wake_voice_chunks = []
                                        continue
                                    listening = True
                                    command_prefix = wake_command
                                    command_buffer = np.array([], dtype=np.float32)
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
                                    text = self._transcribe_audio(command_buffer)["text"].strip()
                                    text = " ".join(
                                        part for part in (command_prefix, text) if part
                                    ).strip()
                                if text and self.on_text_detected:
                                    print(f"[전송] 음성 인식 결과: {text}")
                                    self.on_text_detected(text)
                                listening = False
                                command_prefix = ""
                                command_buffer = np.array([], dtype=np.float32)
                                silence_started = None
                                listening_started = None
                                voiced_seconds = 0.0
            except Exception as exc:
                self._stream_error = str(exc)
                self.running = False
                self._stream_ready.set()
                print(f"[마이크] 입력 장치 오류: {exc}")
        
        self.continuous_listen_thread = threading.Thread(target=continuous_detect, daemon=True)
        self.continuous_listen_thread.start()
        if not self._stream_ready.wait(timeout=5):
            self.running = False
            return "오류: 마이크 입력 장치 초기화 시간이 초과되었습니다."
        if self._stream_error:
            return f"오류: 마이크 입력 장치를 열 수 없습니다: {self._stream_error}"
        return (f"[성공] 마이크 연결: {self.microphone_info['name']} "
                f"(장치 {self.microphone_device}). '자비스'라고 불러주세요.")

    def stop_continuous_listen(self) -> str:
        if not self.running:
            return "음성 감지가 실행 중이지 않습니다."
        
        self.running = False
        if self.continuous_listen_thread:
            self.continuous_listen_thread.join(timeout=2)
        
        return "[성공] 음성 감지가 중지되었습니다."

    def start_wakeword_detection(self) -> str:
        if not SOUND_AVAILABLE:
            return "오류: sounddevice, whisper가 설치되지 않았습니다."
        
        if self.running:
            return "웨이크워드 감지가 이미 실행 중입니다."
        
        self.running = True
        
        def detect_wakeword():
            print("[마이크] 웨이크워드 감지 시작... '자비스' 라고 말하세요!")
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
                    text = result["text"].strip().lower()
                    
                    if self.extract_wake_command(text) is not None:
                        print(f"\n[웨이크워드] 감지! '{text}'")
                        self._on_wakeword_detected()
                
                except Exception as e:
                    print(f"웨이크워드 감지 오류: {e}")
                    time.sleep(1)
        
        self.wakeword_thread = threading.Thread(target=detect_wakeword, daemon=True)
        self.wakeword_thread.start()
        
        return "[성공] 웨이크워드 감지가 시작되었습니다! '자비스' 라고 말해보세요."

    def _on_wakeword_detected(self):
        print("[자비스] 네? 어떤 도움이 필요하신가요?")

    def stop_wakeword_detection(self) -> str:
        if not self.running:
            return "웨이크워드 감지가 실행 중이지 않습니다."
        
        self.running = False
        if self.wakeword_thread:
            self.wakeword_thread.join(timeout=2)
        
        return "[성공] 웨이크워드 감지가 중지되었습니다."

    def start_clap_detection(self) -> str:
        if not SOUND_AVAILABLE:
            return "오류: sounddevice, librosa가 설치되지 않았습니다."
        
        if self.running:
            return "박수 감지가 이미 실행 중입니다."
        
        self.running = True
        self.clap_count = 0
        self.last_clap_time = 0
        
        def detect_clap():
            print("👏 박수 감지 시작... 박수를 쳐보세요!")
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
                        print(f"👏 박수 감지! (총 {self.clap_count}번)")
                        
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
        print("🎵 두 번 박수 감지! 무슨 일을 도와드릴까요?")

    def stop_clap_detection(self) -> str:
        if not self.running:
            return "박수 감지가 실행 중이지 않습니다."
        
        self.running = False
        if self.clap_thread:
            self.clap_thread.join(timeout=2)
        
        return "✅ 박수 감지가 중지되었습니다."


# Singleton instance
_hardware_manager = None


def get_hardware_manager() -> HardwareManager:
    global _hardware_manager
    if _hardware_manager is None:
        _hardware_manager = HardwareManager()
    return _hardware_manager
