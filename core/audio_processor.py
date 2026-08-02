import sys
import numpy as np
import sounddevice as sd
import time
from scipy.io import wavfile
from PyQt6.QtCore import QObject, pyqtSignal

try:
    import av
except ImportError:
    av = None


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
        self._chunk_size = 4096  # 한 번에 처리할 샘플 수 (음성 끊김 방지)
        from core.voice_runtime import get_voice_duplex_controller
        self.duplex = get_voice_duplex_controller()
        self.duplex.interrupt_callback = self.cancel_playback

    def cancel_playback(self):
        self.duplex.cancel_event.set()
        try:
            sd.stop()
        except Exception:
            pass
        
    def play_and_analyze_tts(self, wav_path: str):
        """WAV 파일을 재생하면서 오디오 데이터를 분석합니다 (자비스 TTS용)"""
        completed = False
        try:
            self._is_speaking = True
            self._is_running = True
            
            sr, data = self._read_tts_audio(wav_path)
            self._sample_rate = sr
            
            # 원본 음량과 채널을 보존한 float32 재생 데이터로 변환한다.
            if np.issubdtype(data.dtype, np.integer):
                scale = float(max(abs(np.iinfo(data.dtype).min), np.iinfo(data.dtype).max))
                playback_data = data.astype(np.float32) / scale
            else:
                playback_data = np.clip(data.astype(np.float32), -1.0, 1.0)
            if not np.any(playback_data):
                raise ValueError("TTS generated a silent WAV file")

            analysis_data = playback_data.mean(axis=1) if playback_data.ndim > 1 else playback_data
            # sd.play의 내부 콜백에는 복사 작업만 남기고 FFT/Qt 시그널은 이 스레드에서
            # 낮은 주기로 처리해 출력 underflow와 끊김을 방지한다.
            sd.play(playback_data, sr, blocking=False)
            analysis_window = max(512, int(sr * 0.05))
            started_at = time.monotonic()
            self.duplex.start_output()
            while not self.duplex.cancel_event.is_set():
                position = int((time.monotonic() - started_at) * sr)
                if position >= len(analysis_data):
                    break
                chunk = analysis_data[position:position + analysis_window]
                if len(chunk):
                    amplitude, freq_bands = self._analyze_audio(chunk, sr)
                    self.duplex.update_output(float(np.sqrt(np.mean(chunk * chunk))))
                    self.duplex.update_output_samples(chunk)
                    self.audio_update.emit(amplitude, freq_bands, True)
                time.sleep(0.05)
            sd.wait()
            completed = not self.duplex.cancel_event.is_set()
                
        except Exception as e:
            print(f"[AudioProcessor] TTS 분석 오류: {e}")
        finally:
            self._is_speaking = False
            self._is_running = False
            self.duplex.finish_output()
            # 마지막으로 0 레벨 신호 보내기
            self.audio_update.emit(0.0, [], False)
        return completed

    def play_streaming_tts(self, pcm_chunks, prebuffer_seconds: float = 1.0):
        """Play GPT-SoVITS PCM with enough initial audio to prevent underflow."""
        stream = None
        pending = b""
        buffered = bytearray()
        stream_format = None
        completed = False
        try:
            self._is_speaking = True
            self._is_running = True
            self.duplex.start_output()
            for sample_rate, channels, sample_width, chunk in pcm_chunks:
                if self.duplex.cancel_event.is_set():
                    break
                if sample_width != 2:
                    raise ValueError(f"지원하지 않는 스트림 샘플 폭: {sample_width}")
                current_format = (sample_rate, channels, sample_width)
                if stream_format is None:
                    stream_format = current_format
                elif current_format != stream_format:
                    raise ValueError("TTS 스트림 도중 오디오 형식이 변경되었습니다.")
                self._sample_rate = sample_rate
                frame_bytes = sample_width * channels
                data = pending + chunk
                complete = len(data) - (len(data) % frame_bytes)
                pending = data[complete:]
                if complete:
                    buffered.extend(data[:complete])

                prebuffer_bytes = max(
                    frame_bytes,
                    int(max(0.0, prebuffer_seconds) * sample_rate) * frame_bytes,
                )
                if stream is None and len(buffered) >= prebuffer_bytes:
                    stream = sd.RawOutputStream(
                        samplerate=sample_rate,
                        channels=channels,
                        dtype="int16",
                    )
                    stream.start()
                if stream is None:
                    continue
                pcm = bytes(buffered)
                buffered.clear()
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)
                if len(samples):
                    # Publish the reference before the blocking device write starts;
                    # otherwise the microphone sees the first playback block while
                    # echo suppression still has an empty reference.
                    amplitude, freq_bands = self._analyze_audio(samples, sample_rate)
                    self.duplex.update_output(float(np.sqrt(np.mean(samples * samples))))
                    self.duplex.update_output_samples(samples)
                    self.audio_update.emit(amplitude, freq_bands, True)
                stream.write(pcm)
            if stream is None and buffered and stream_format is not None:
                sample_rate, channels, _sample_width = stream_format
                stream = sd.RawOutputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                )
                stream.start()
                pcm = bytes(buffered)
                buffered.clear()
                stream.write(pcm)
            if stream is None and self.duplex.cancel_event.is_set():
                completed = False
            elif stream is None:
                raise ValueError("GPT-SoVITS가 빈 음성 스트림을 반환했습니다.")
            else:
                completed = not self.duplex.cancel_event.is_set()
        except Exception as exc:
            print(f"[AudioProcessor] 스트리밍 TTS 오류: {exc}")
            raise
        finally:
            if stream is not None:
                try:
                    stream.stop()
                finally:
                    stream.close()
            self._is_speaking = False
            self._is_running = False
            self.duplex.finish_output()
            self.audio_update.emit(0.0, [], False)
        return completed

    @staticmethod
    def _read_tts_audio(media_path: str):
        """로컬 WAV와 온라인 Neural TTS MP3를 공통 배열로 읽는다."""
        if str(media_path).casefold().endswith(".wav"):
            return wavfile.read(media_path)
        if av is None:
            raise RuntimeError("MP3 TTS 재생을 위해 av 패키지가 필요합니다.")
        chunks = []
        sample_rate = 0
        with av.open(media_path) as container:
            for frame in container.decode(audio=0):
                sample_rate = frame.sample_rate
                chunk = frame.to_ndarray()
                if chunk.ndim == 2:
                    chunk = chunk.T
                chunks.append(chunk)
        if not chunks or not sample_rate:
            raise ValueError("TTS 오디오를 디코딩하지 못했습니다.")
        return sample_rate, np.concatenate(chunks, axis=0)
    
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
