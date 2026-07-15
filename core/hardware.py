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
        
        if SOUND_AVAILABLE:
            # GPU 사용 가능 여부 확인
            if torch.cuda.is_available():
                self.device = "cuda"
                print(f"[GPU] CUDA를 사용합니다! (GPU: {torch.cuda.get_device_name(0)})")
            elif torch.backends.mps.is_available():
                self.device = "mps"
                print("[GPU] Apple Silicon MPS를 사용합니다!")
            else:
                print("[CPU] GPU를 사용할 수 없어 CPU를 사용합니다.")
            
            self.whisper_model = whisper.load_model("base", device=self.device)

    def start_continuous_listen(self, on_text_callback, audio_processor=None) -> str:
        """지속적인 음성 감지 시작 (웨이크워드/박수 감지 포함)"""
        if not SOUND_AVAILABLE:
            return "오류: sounddevice, whisper가 설치되지 않았습니다."
        
        if self.running:
            return "이미 음성 감지가 실행 중입니다."
        
        self.running = True
        self.on_text_detected = on_text_callback
        self.audio_processor = audio_processor
        
        def continuous_detect():
            print("[마이크] 지속적인 음성 감지 시작...")
            fs = 16000
            chunk_duration = 0.1  # 0.1초마다 오디오 수집
            chunk_size = int(chunk_duration * fs)
            
            silence_threshold = 2  # 2초 무음 시 전송 (보스 요청)
            silence_start = None
            current_text = ""
            is_listening = False  # 웨이크워드/박수 감지 후 청취 모드
            last_clap_time_local = 0
            clap_count_local = 0
            
            # 오디오 버퍼 (최근 2초치 데이터 저장)
            audio_buffer = np.array([], dtype=np.float32)
            buffer_max_size = int(2 * fs)  # 2초
            
            while self.running:
                try:
                    # 0.1초 오디오 청크 수집
                    chunk = sd.rec(chunk_size, samplerate=fs, channels=1, dtype='float32')
                    sd.wait()
                    chunk = chunk.flatten()
                    
                    # 버퍼에 추가
                    audio_buffer = np.concatenate((audio_buffer, chunk))
                    if len(audio_buffer) > buffer_max_size:
                        audio_buffer = audio_buffer[-buffer_max_size:]
                    
                    # 사운드바 업데이트
                    if self.audio_processor:
                        try:
                            amplitude, freq_bands = self.audio_processor._analyze_audio(chunk, fs)
                            self.audio_processor.audio_update.emit(amplitude, freq_bands, False)
                        except Exception as e:
                            print(f"[사운드바 업데이트 오류]: {e}")
                    
                    if not is_listening:
                        # 웨이크워드/박수 감지 모드 - 버퍼에 쌓인 2초 데이터 사용
                        
                        # 1. 웨이크워드 감지 (버퍼의 2초 데이터 사용)
                        if len(audio_buffer) >= buffer_max_size:
                            result = self.whisper_model.transcribe(audio_buffer, language="ko")
                            text = result["text"].strip().lower()
                            
                            if "자비스" in text or "자비" in text:
                                print(f"\n[웨이크워드] 감지! '{text}'")
                                is_listening = True
                                # "자비스"나 "자비" 텍스트를 제외한 나머지 텍스트를 current_text에 추가
                                cleaned_text = text.replace("자비스", "").replace("자비", "").strip()
                                if cleaned_text:
                                    current_text = cleaned_text
                                    print(f"[명령] 감지된 명령: {current_text}")
                                    silence_start = None
                                else:
                                    current_text = ""
                                    silence_start = None
                                # 버퍼 초기화
                                audio_buffer = np.array([], dtype=np.float32)
                                continue
                        
                        # 2. 박수 감지 (에너지 기반 - 현재 청크 사용)
                        energy = np.sum(chunk ** 2) / len(chunk)
                        energy_db = 10 * np.log10(energy + 1e-10)
                        
                        if energy_db > -10:
                            current_time = time.time()
                            if current_time - last_clap_time_local > 0.3:
                                clap_count_local += 1
                                last_clap_time_local = current_time
                                print(f"[박수] 감지! (총 {clap_count_local}번)")
                                
                                if clap_count_local >= 2:
                                    print("[박수] 두 번 박수 감지! 음성 청취 시작!")
                                    is_listening = True
                                    current_text = ""
                                    silence_start = None
                                    clap_count_local = 0
                                    # 버퍼 초기화
                                    audio_buffer = np.array([], dtype=np.float32)
                    else:
                        # 청취 모드 - 계속 버퍼에 쌓으면서 주기적으로 음성 인식
                        # 0.5초마다 한 번씩 인식 (과도한 CPU 사용 방지)
                        if len(audio_buffer) >= int(0.5 * fs):
                            result = self.whisper_model.transcribe(audio_buffer, language="ko")
                            text = result["text"].strip()
                            
                            if text:
                                current_text += " " + text
                                current_text = current_text.strip()
                                print(f"[음성] 감지된 텍스트: {current_text}")
                                silence_start = None  # 무음 타이머 리셋
                                # 버퍼 초기화 (새로운 음성 수집 시작)
                                audio_buffer = np.array([], dtype=np.float32)
                            else:
                                # 무음 감지
                                if silence_start is None:
                                    silence_start = time.time()
                                elif time.time() - silence_start > silence_threshold:
                                    # 2초 무음 시 텍스트 전송
                                    if current_text:
                                        print(f"[전송] 텍스트 전송: {current_text}")
                                        if self.on_text_detected:
                                            self.on_text_detected(current_text)
                                    # 리셋
                                    current_text = ""
                                    silence_start = None
                                    is_listening = False
                                    audio_buffer = np.array([], dtype=np.float32)
                except Exception as e:
                    print(f"음성 감지 오류: {e}")
                    time.sleep(1)
        
        self.continuous_listen_thread = threading.Thread(target=continuous_detect, daemon=True)
        self.continuous_listen_thread.start()
        
        return "[성공] 지속적인 음성 감지가 시작되었습니다! '자비스' 라고 부르거나 두 번 박수를 쳐보세요."

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
            while self.running:
                try:
                    duration = 2
                    fs = 16000
                    recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='float32')
                    sd.wait()
                    
                    result = self.whisper_model.transcribe(recording.flatten(), language="ko")
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
            fs = 44100
            
            def audio_callback(indata, frames, time, status):
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
            
            with sd.InputStream(callback=audio_callback, channels=1, samplerate=fs):
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
