import sys
import numpy as np
import sounddevice as sd
import tempfile
import os
from scipy.io import wavfile
from PyQt6.QtCore import QObject, pyqtSignal


class AudioProcessor(QObject):
    """실제 오디오 데이터를 분석해서 진폭(Amplitude)과 주파수 대역별 에너지를 전달하는 클래스"""
    
    # 오디오 데이터가 업데이트될 때마다 발생하는 신호
    # (진폭 레벨 0-100, 주파수 대역별 에너지 리스트, speaking 여부)
    audio_update = pyqtSignal(float, list, bool)
    
    def __init__(self):
        super().__init__()
        self._is_running = False
        self._is_speaking = False  # 자비스가 말하는 중인지
        self._sample_rate = 44100  # 기본 샘플 레이트
        self._chunk_size = 1024  # 한 번에 처리할 샘플 수
        
    def play_and_analyze_tts(self, wav_path: str):
        """WAV 파일을 재생하면서 오디오 데이터를 분석합니다 (자비스 TTS용)"""
        try:
            self._is_speaking = True
            self._is_running = True
            
            # WAV 파일 읽기
            sr, data = wavfile.read(wav_path)
            self._sample_rate = sr
            
            # 스테레오면 모노로 변환
            if len(data.shape) > 1:
                data = data.mean(axis=1)
            
            # 데이터를 float32로 정규화 (-1 ~ 1)
            data = data.astype(np.float32) / np.max(np.abs(data))
            
            # 오디오 재생 + 분석
            def callback(outdata, frames, time, status):
                if status:
                    print(status, file=sys.stderr)
                nonlocal data
                if len(data) < frames:
                    # 남은 데이터가 부족하면 0으로 채우기
                    outdata[:len(data), 0] = data
                    outdata[len(data):, 0] = 0
                    data = np.array([])
                else:
                    outdata[:, 0] = data[:frames]
                    data = data[frames:]
                
                # 현재 프레임 분석
                if len(outdata[:, 0]) > 0:
                    amplitude, freq_bands = self._analyze_audio(outdata[:, 0], sr)
                    self.audio_update.emit(amplitude, freq_bands, True)
            
            with sd.OutputStream(samplerate=sr, channels=1, callback=callback, blocksize=self._chunk_size):
                sd.wait()
                
        except Exception as e:
            print(f"[AudioProcessor] TTS 분석 오류: {e}")
        finally:
            self._is_speaking = False
            self._is_running = False
            # 마지막으로 0 레벨 신호 보내기
            self.audio_update.emit(0.0, [], False)
    
    def start_listen_analysis(self):
        """사용자 음성 입력을 실시간으로 분석합니다 (STT용)"""
        try:
            self._is_running = True
            self._is_speaking = False
            
            def callback(indata, frames, time, status):
                if status:
                    print(status, file=sys.stderr)
                # 현재 프레임 분석
                amplitude, freq_bands = self._analyze_audio(indata[:, 0], self._sample_rate)
                self.audio_update.emit(amplitude, freq_bands, False)
            
            self._stream = sd.InputStream(samplerate=self._sample_rate, channels=1, 
                                         callback=callback, blocksize=self._chunk_size)
            self._stream.start()
            
        except Exception as e:
            print(f"[AudioProcessor] 음성 입력 분석 오류: {e}")
    
    def stop_listen_analysis(self):
        """사용자 음성 분석을 중지합니다"""
        if hasattr(self, '_stream'):
            self._stream.stop()
            self._stream.close()
        self._is_running = False
        self.audio_update.emit(0.0, [], False)
    
    def _analyze_audio(self, audio_data: np.ndarray, sample_rate: int) -> tuple[float, list]:
        """
        오디오 데이터를 분석해서 진폭과 주파수 대역별 에너지를 반환합니다
        
        Returns:
            amplitude: 0-100 사이의 진폭 레벨
            freq_bands: 주파수 대역별 에너지 리스트 [저음, 중음, 고음]
        """
        # 1. 진폭 계산 (RMS)
        rms = np.sqrt(np.mean(np.square(audio_data)))
        amplitude = min(100.0, rms * 300)  # 0-100으로 스케일링
        
        # 2. FFT로 주파수 분석
        n_fft = len(audio_data)
        fft_data = np.fft.fft(audio_data)
        fft_magnitude = np.abs(fft_data[:n_fft // 2])
        freqs = np.fft.fftfreq(n_fft, d=1/sample_rate)[:n_fft // 2]
        
        # 3. 주파수 대역별 에너지 계산
        # 저음 (20-300Hz), 중음 (300-3000Hz), 고음 (3000-20000Hz)
        bands = [
            (20, 300),
            (300, 3000),
            (3000, 20000)
        ]
        
        freq_bands = []
        for low, high in bands:
            mask = (freqs >= low) & (freqs < high)
            band_energy = np.mean(fft_magnitude[mask]) if np.any(mask) else 0
            freq_bands.append(min(100.0, band_energy * 10))  # 0-100으로 스케일링
        
        return amplitude, freq_bands


# 싱글톤 인스턴스
_audio_processor = None


def get_audio_processor() -> AudioProcessor:
    global _audio_processor
    if _audio_processor is None:
        _audio_processor = AudioProcessor()
    return _audio_processor
