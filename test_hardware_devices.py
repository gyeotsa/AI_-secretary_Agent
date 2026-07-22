import threading

import numpy as np

import core.hardware as hardware
import core.multimodal as multimodal


def _bare_hardware_manager():
    manager = hardware.HardwareManager.__new__(hardware.HardwareManager)
    manager.microphone_device = None
    manager.microphone_info = None
    manager.running = False
    manager.continuous_listen_thread = None
    manager._stream_ready = threading.Event()
    manager._stream_error = ""
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
