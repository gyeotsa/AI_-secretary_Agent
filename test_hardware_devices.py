import threading
import time

import numpy as np

import core.hardware as hardware
import core.multimodal as multimodal


def test_default_whisper_model_is_medium():
    assert hardware.Config.WHISPER_MODEL == "medium"


def test_microphone_command_end_defaults():
    assert hardware.Config.MICROPHONE_SILENCE_SECONDS == 2.0
    assert hardware.Config.MICROPHONE_MAX_COMMAND_SECONDS == 15.0
    assert hardware.HardwareManager.MIN_SPEECH_RMS == 0.0005


def test_only_exact_first_wake_word_opens_voice_command():
    assert hardware.HardwareManager.extract_wake_command("자비스 디코 켜") == "디코 켜"
    assert hardware.HardwareManager.extract_wake_command("자비스, 디코 꺼줘") == "디코 꺼줘"
    assert hardware.HardwareManager.extract_wake_command("디코 켜 자비스") is None
    assert hardware.HardwareManager.extract_wake_command("안녕 자비스 디코 켜") is None
    assert hardware.HardwareManager.extract_wake_command("자비 디코 켜") is None
    assert hardware.HardwareManager.extract_wake_command("감사합니다") is None
    assert hardware.HardwareManager.extract_wake_command("자비스디코 켜") == "디코 켜"


def test_whisper_prompt_uses_registered_app_aliases(monkeypatch):
    manager = _bare_hardware_manager()
    monkeypatch.setattr(manager, "_speech_vocabulary", lambda: ["디스코드", "디코"])
    prompt = manager._whisper_prompt()
    assert "디스코드" in prompt
    assert "디코" in prompt


def test_tts_output_suspends_microphone_and_adds_cooldown():
    manager = _bare_hardware_manager()
    manager.set_output_active(True)
    assert manager._output_active.is_set()
    assert manager._ignore_input_until == float("inf")

    manager.set_output_active(False, cooldown=0.5)
    assert not manager._output_active.is_set()
    assert manager._ignore_input_until > time.monotonic()


def _bare_hardware_manager():
    manager = hardware.HardwareManager.__new__(hardware.HardwareManager)
    manager.microphone_device = None
    manager.microphone_info = None
    manager.running = False
    manager.continuous_listen_thread = None
    manager._stream_ready = threading.Event()
    manager._stream_error = ""
    manager._output_active = threading.Event()
    manager._ignore_input_until = 0.0
    manager.audio_processor = None
    manager.on_text_detected = None
    return manager


def test_microphone_auto_selects_default_native_rate(monkeypatch):
    manager = _bare_hardware_manager()
    devices = [
        {"index": 3, "name": "USB microphone", "max_input_channels": 1, "default_samplerate": 48000.0},
        {"index": 7, "name": "Built-in array", "max_input_channels": 2, "default_samplerate": 44100.0},
    ]
    monkeypatch.setattr(manager, "list_input_devices", lambda: devices)
    monkeypatch.setattr(hardware.Config, "MICROPHONE_DEVICE", "auto")
    monkeypatch.setattr(hardware.sd.default, "device", (7, 0))
    checked = {}
    monkeypatch.setattr(hardware.sd, "check_input_settings", lambda **kwargs: checked.update(kwargs))

    selected, rate = manager._select_microphone()

    assert selected["index"] == 7
    assert rate == 44100
    assert checked["device"] == 7


def test_microphone_can_be_selected_by_name(monkeypatch):
    manager = _bare_hardware_manager()
    devices = [
        {"index": 3, "name": "USB microphone", "max_input_channels": 1, "default_samplerate": 48000.0},
        {"index": 7, "name": "Built-in array", "max_input_channels": 2, "default_samplerate": 44100.0},
    ]
    monkeypatch.setattr(manager, "list_input_devices", lambda: devices)
    monkeypatch.setattr(hardware.Config, "MICROPHONE_DEVICE", "usb")
    monkeypatch.setattr(hardware.sd, "check_input_settings", lambda **kwargs: None)

    selected, rate = manager._select_microphone()

    assert (selected["index"], rate) == (3, 48000)


def test_audio_is_resampled_to_whisper_rate():
    audio = np.arange(48000, dtype=np.float32)
    converted = hardware.HardwareManager._to_16khz(audio, 48000)
    assert converted.dtype == np.float32
    assert len(converted) == 16000


def test_audio_normalization_raises_quiet_signal_without_clipping():
    audio = np.array([-0.001, 0.0, 0.001], dtype=np.float32)
    normalized = hardware.HardwareManager._normalize_audio(audio)
    assert np.max(np.abs(normalized)) > np.max(np.abs(audio))
    assert np.max(np.abs(normalized)) <= 1.0


def test_whisper_uses_deterministic_korean_command_options(monkeypatch):
    manager = _bare_hardware_manager()
    captured = {}

    class Model:
        def transcribe(self, audio, **kwargs):
            captured.update(kwargs)
            return {"text": "테스트"}

    manager.whisper_model = Model()
    monkeypatch.setattr(manager, "_whisper_prompt", lambda: "동적 어휘")
    manager._transcribe_audio(np.ones(1600, dtype=np.float32) * 0.001)

    assert captured["language"] == "ko"
    assert captured["temperature"] == 0
    assert captured["beam_size"] == 5
    assert captured["condition_on_previous_text"] is False
    assert captured["initial_prompt"] == "동적 어휘"


def test_continuous_listener_submits_after_silence(monkeypatch):
    manager = _bare_hardware_manager()

    class FakeModel:
        calls = 0

        def transcribe(self, audio, **kwargs):
            self.calls += 1
            return {"text": "자비스 테스트 명령" if self.calls == 1 else ""}

    class FakeStream:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, frames):
            self.calls += 1
            time.sleep(0.01)
            level = 0.01 if 10 < self.calls <= 20 else 0.0
            return np.full((frames, 1), level, dtype=np.float32), False

    manager.whisper_model = FakeModel()
    def select_microphone():
        manager.microphone_device = 1
        manager.microphone_info = {"index": 1, "name": "test mic"}
        return manager.microphone_info, 16000

    monkeypatch.setattr(manager, "_select_microphone", select_microphone)
    monkeypatch.setattr(hardware.sd, "InputStream", lambda **kwargs: FakeStream())
    monkeypatch.setattr(hardware.Config, "MICROPHONE_SILENCE_SECONDS", 0.15)
    monkeypatch.setattr(hardware.Config, "MICROPHONE_MAX_COMMAND_SECONDS", 1.0)
    received = []

    result = manager.start_continuous_listen(received.append)
    deadline = time.monotonic() + 2
    while not received and time.monotonic() < deadline:
        time.sleep(0.02)
    manager.stop_continuous_listen()

    assert "성공" in result
    assert received == ["테스트 명령"]


class _FakeCapture:
    def __init__(self, opened, frame=None):
        self.opened = opened
        self.frame = frame
        self.released = False

    def isOpened(self):
        return self.opened

    def read(self):
        return (self.frame is not None, self.frame)

    def release(self):
        self.released = True


def test_camera_falls_back_to_working_device_and_backend(monkeypatch, tmp_path):
    manager = multimodal.MultimodalManager()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    captures = []

    def fake_capture(index, backend):
        capture = _FakeCapture(index == 1 and backend == 22, frame if index == 1 and backend == 22 else None)
        captures.append(capture)
        return capture

    monkeypatch.setattr(multimodal.Config, "CAMERA_INDEX", "auto")
    monkeypatch.setattr(manager, "_camera_backends", lambda: [("FIRST", 11), ("SECOND", 22)])
    monkeypatch.setattr(multimodal.cv2, "VideoCapture", fake_capture)
    monkeypatch.setattr(multimodal.cv2, "imwrite", lambda path, image: True)
    monkeypatch.setattr(multimodal.time, "sleep", lambda _: None)
    monkeypatch.setattr(manager.safety, "validate_path", lambda path: (True, ""))

    result = manager.capture_camera_frame(str(tmp_path / "capture.jpg"))

    assert "카메라 1(SECOND, 640x480)" in result
    assert all(capture.released for capture in captures)


def test_invalid_camera_index_configuration_is_reported(monkeypatch):
    manager = multimodal.MultimodalManager()
    monkeypatch.setattr(multimodal.Config, "CAMERA_INDEX", "front")
    result = manager.capture_camera_frame()
    assert "CAMERA_INDEX는 auto 또는 0 이상의 정수" in result
