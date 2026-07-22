import os
import numpy as np
import threading
import time
import queue
from config import Config


try:
    import sounddevice as sd
    import whisper
    import librosa
    import torch
    SOUND_AVAILABLE = True
except ImportError:
    SOUND_AVAILABLE = False


class HardwareManager:
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
        size = max(1, round(len(audio) * 16000 / source_rate))
        source = np.linspace(0.0, 1.0, len(audio), endpoint=False)
        target = np.linspace(0.0, 1.0, size, endpoint=False)
        return np.interp(target, source, audio).astype(np.float32)

    def start_continuous_listen(self, on_text_callback, audio_processor=None) -> str:
        """지속적인 음성 감지 시작 (웨이크워드/박수 감지 포함)"""
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
                    last_voice = time.monotonic()
                    last_wake_check = 0.0
                    noise_samples = []
                    speech_threshold = 0.0005
                    clap_count = 0
                    last_clap = 0.0
                    while self.running:
                        chunk, overflowed = stream.read(chunk_size)
                        chunk = chunk[:, 0].copy()
                        if overflowed:
                            print("[마이크] 입력 버퍼 overflow 감지")
                        rms = float(np.sqrt(np.mean(chunk * chunk)))
                        if len(noise_samples) < 10:
                            noise_samples.append(rms)
                            if len(noise_samples) == 10:
                                speech_threshold = max(0.0003, float(np.median(noise_samples)) * 4)
                                print(f"[마이크] 자동 음성 임계값: {speech_threshold:.6f}")
                        if self.audio_processor:
                            amplitude, bands = self.audio_processor._analyze_audio(chunk, native_rate)
                            self.audio_processor.audio_update.emit(amplitude, bands, False)
                        audio16 = self._to_16khz(chunk, native_rate)
                        if not listening:
                            now = time.monotonic()
                            peak = float(np.max(np.abs(chunk)))
                            clap_threshold = max(0.05, speech_threshold * 20)
                            if peak >= clap_threshold and now - last_clap >= 0.25:
                                clap_count = clap_count + 1 if now - last_clap <= 1.2 else 1
                                last_clap = now
                                if clap_count >= 2:
                                    print("[박수] 두 번 감지, 음성 청취 시작")
                                    listening = True
                                    command_prefix = ""
                                    command_buffer = np.array([], dtype=np.float32)
                                    last_voice = now
                                    clap_count = 0
                                    wake_buffer = np.array([], dtype=np.float32)
                                    continue
                            wake_buffer = np.concatenate((wake_buffer, audio16))[-32000:]
                            if len(wake_buffer) >= 32000 and now - last_wake_check >= 1.5:
                                last_wake_check = now
                                text = self.whisper_model.transcribe(wake_buffer, language="ko")["text"].strip().lower()
                                if "자비스" in text or "자비" in text:
                                    print(f"[웨이크워드] 감지: {text}")
                                    listening = True
                                    command_prefix = text.replace("자비스", "").replace("자비", "").strip()
                                    command_buffer = np.array([], dtype=np.float32)
                                    last_voice = now
                                    wake_buffer = np.array([], dtype=np.float32)
                        else:
                            command_buffer = np.concatenate((command_buffer, audio16))
                            if rms >= speech_threshold:
                                last_voice = time.monotonic()
                            if time.monotonic() - last_voice >= 2.0 and len(command_buffer) >= 4800:
                                text = self.whisper_model.transcribe(command_buffer, language="ko")["text"].strip()
                                text = " ".join(part for part in (command_prefix, text) if part).strip()
                                if text and self.on_text_detected:
                                    print(f"[전송] 음성 인식 결과: {text}")
                                    self.on_text_detected(text)
                                listening = False
                                command_prefix = ""
                                command_buffer = np.array([], dtype=np.float32)
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
                    result = self.whisper_model.transcribe(audio, language="ko")
                    text = result["text"].strip().lower()
                    
                    if "자비스" in text or "자비" in text:
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
