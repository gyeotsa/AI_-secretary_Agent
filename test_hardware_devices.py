import threading
import time

import numpy as np
import pytest
from types import SimpleNamespace

import core.hardware as hardware


@pytest.fixture(autouse=True)
def _stable_wake_word(monkeypatch):
    """Hardware tests must not depend on the user's persisted runtime identity."""
    monkeypatch.setattr(
        hardware, "get_assistant_settings",
        lambda: SimpleNamespace(wake_word="자비스", assistant_name="자비스"),
    )


def test_default_stt_uses_faster_whisper_large_v3():
    assert hardware.Config.STT_ENGINE == "faster-whisper"
    assert hardware.Config.WHISPER_MODEL == "large-v3"
    assert hardware.Config.WHISPER_COMPUTE_TYPE == "int8_float16"


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
    hotwords = manager._whisper_hotwords()
    assert "디스코드" in hotwords
    assert "디코" in hotwords


def test_whisper_hotwords_stay_within_decoder_budget(monkeypatch):
    monkeypatch.setattr(
        hardware.HardwareManager,
        "_speech_vocabulary",
        staticmethod(lambda: [f"긴어휘{index}" for index in range(200)]),
    )
    assert len(hardware.HardwareManager._whisper_hotwords(120)) <= 120


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
    manager._listener_start_lock = threading.Lock()
    manager._listener_generation = 0
    manager._listener_stop_event = threading.Event()
    manager._input_stream = None
    manager._closed = False
    manager.wakeword_thread = None
    manager.clap_thread = None
    manager.continuous_listen_thread = None
    manager._stream_ready = threading.Event()
    manager._stream_error = ""
    manager._output_active = threading.Event()
    manager._ignore_input_until = 0.0
    manager.stt_engine = "openai-whisper"
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


def test_trailing_silence_is_removed_before_final_transcription():
    voice = np.ones(1600, dtype=np.float32) * 0.01
    silence = np.zeros(32000, dtype=np.float32)
    trimmed = hardware.HardwareManager._trim_trailing_silence(
        np.concatenate((voice, silence))
    )
    assert len(trimmed) < len(voice) + len(silence)
    assert len(trimmed) >= len(voice)


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


def test_faster_whisper_rejects_low_confidence_hallucination():
    manager = _bare_hardware_manager()
    manager.stt_engine = "faster-whisper"
    result = {
        "text": "감사합니다",
        "segments": [{"avg_logprob": -1.4, "no_speech_prob": 0.8, "compression_ratio": 1.0}],
    }
    assert manager._trusted_transcription_text(result) == ""


def test_faster_whisper_accepts_confident_command():
    manager = _bare_hardware_manager()
    manager.stt_engine = "faster-whisper"
    result = {
        "text": "자비스 디코 종료해",
        "segments": [{"avg_logprob": -0.2, "no_speech_prob": 0.05, "compression_ratio": 1.1}],
    }
    assert manager._trusted_transcription_text(result) == "자비스 디코 종료해"


def test_faster_whisper_rejects_confident_repetition_loop():
    manager = _bare_hardware_manager()
    manager.stt_engine = "faster-whisper"
    result = {
        "text": "코드, 아니스 코드, 아니스 코드.",
        "segments": [{"avg_logprob": -0.15, "no_speech_prob": 0.03, "compression_ratio": 1.1}],
    }
    assert manager._trusted_transcription_text(result) == ""


def test_faster_whisper_keeps_valid_command_with_reused_term():
    manager = _bare_hardware_manager()
    manager.stt_engine = "faster-whisper"
    result = {
        "text": "자비스 파일 열고 파일 저장해줘",
        "segments": [{"avg_logprob": -0.2, "no_speech_prob": 0.05, "compression_ratio": 1.1}],
    }
    assert manager._trusted_transcription_text(result) == "자비스 파일 열고 파일 저장해줘"


def test_hangul_phoneme_similarity_prefers_kyeo_over_kkeo():
    heard = hardware.HardwareManager._hangul_jamo(
        hardware.HardwareManager._speech_stem("케어")
    )
    launch = hardware.HardwareManager._hangul_jamo("켜")
    close = hardware.HardwareManager._hangul_jamo("꺼")
    assert hardware.HardwareManager._edit_similarity(heard, launch) > (
        hardware.HardwareManager._edit_similarity(heard, close)
    )


def test_stt_registry_correction_loads_intents_without_tool_executor(monkeypatch):
    import core.plugin as plugin_runtime

    registry = plugin_runtime.PluginRegistry()
    monkeypatch.setattr(plugin_runtime, "_plugin_registry", registry)

    corrected = hardware.HardwareManager._correct_registry_command("메모장 케어")

    assert corrected == "메모장 켜"
    assert registry.get_all_intents()


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


def test_stop_aborts_input_and_discards_transcription_without_stopping_tts(monkeypatch):
    manager = _bare_hardware_manager()
    manager.whisper_model = object()
    entered, release = threading.Event(), threading.Event()
    received = []

    class Stream:
        aborted = False
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def read(self, frames):
            return np.full((frames, 1), 0.01, dtype=np.float32), False

        def abort(self):
            self.aborted = True

    stream = Stream()
    monkeypatch.setattr(hardware, "SOUND_AVAILABLE", True)
    monkeypatch.setattr(manager, "_select_microphone", lambda: ({"index": 1, "name": "test mic"}, 16000))
    manager.microphone_info = {"index": 1, "name": "test mic"}
    monkeypatch.setattr(hardware.sd, "InputStream", lambda **kwargs: stream)
    monkeypatch.setattr(hardware.sd, "stop", lambda *a, **k: pytest.fail("mic OFF must not stop TTS playback"))
    monkeypatch.setattr(manager, "_select_wake_candidate", lambda result: "자비스 테스트 명령")

    def transcribe(audio):
        entered.set()
        assert release.wait(3)
        return {"text": "자비스 테스트 명령"}

    monkeypatch.setattr(manager, "_transcribe_audio", transcribe)
    assert "성공" in manager.start_continuous_listen(received.append)
    assert manager.microphone_active and entered.wait(3)
    stopper = threading.Thread(target=manager.stop_continuous_listen)
    stopper.start()
    assert manager._listener_stop_event.wait(3)
    release.set()
    stopper.join(3)
    assert not stopper.is_alive()
    assert stream.aborted and stream.closed
    assert not manager.running and not manager.microphone_active and received == []


def test_stop_during_device_open_closes_stream_without_starting_it(monkeypatch):
    manager = _bare_hardware_manager()
    manager.whisper_model = object()
    entered, release = threading.Event(), threading.Event()

    class Stream:
        closed = False

        def __init__(self, **kwargs):
            entered.set()
            assert release.wait(3)

        def close(self):
            self.closed = True

        def __enter__(self):
            pytest.fail("cancelled device must not start recording")

    streams = []

    def create_stream(**kwargs):
        stream = Stream(**kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(hardware, "SOUND_AVAILABLE", True)
    monkeypatch.setattr(manager, "_select_microphone", lambda: ({"index": 1, "name": "test mic"}, 16000))
    monkeypatch.setattr(hardware.sd, "InputStream", create_stream)
    results = []
    starter = threading.Thread(target=lambda: results.append(manager.start_continuous_listen(lambda text: None)))
    starter.start()
    assert entered.wait(3)
    stopper = threading.Thread(target=manager.stop_continuous_listen)
    stopper.start()
    assert manager._listener_stop_event.wait(3)
    release.set()
    starter.join(3)
    stopper.join(3)
    assert not starter.is_alive() and not stopper.is_alive()
    assert streams[0].closed and "취소" in results[0]
    assert not manager.running and not manager.microphone_active


def test_stop_cancels_start_waiting_for_start_lock(monkeypatch):
    manager = _bare_hardware_manager()
    entered, release = threading.Event(), threading.Event()

    class StartLock:
        def __enter__(self):
            entered.set()
            assert release.wait(3)

        def __exit__(self, *args):
            pass

    manager._listener_start_lock = StartLock()
    monkeypatch.setattr(manager, "ensure_stt_model", lambda **kwargs: pytest.fail("cancelled start loaded STT"))
    results = []
    starter = threading.Thread(target=lambda: results.append(manager.start_continuous_listen(lambda text: None)))
    starter.start()
    assert entered.wait(3)
    manager.stop_continuous_listen()
    release.set()
    starter.join(3)
    assert not starter.is_alive() and "취소" in results[0]


def test_microphone_can_restart_after_stop(monkeypatch):
    manager = _bare_hardware_manager()
    manager.whisper_model = object()

    class Stream:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def read(self, frames):
            time.sleep(0.01)
            return np.zeros((frames, 1), dtype=np.float32), False

        def abort(self):
            pass

    monkeypatch.setattr(hardware, "SOUND_AVAILABLE", True)
    manager.microphone_info = {"index": 1, "name": "test mic"}
    monkeypatch.setattr(manager, "_select_microphone", lambda: (manager.microphone_info, 16000))
    monkeypatch.setattr(hardware.sd, "InputStream", lambda **kwargs: Stream())
    for _ in range(2):
        assert "성공" in manager.start_continuous_listen(lambda text: None)
        stream = manager._input_stream
        assert manager.microphone_active
        manager.stop_continuous_listen()
        assert stream.closed and not manager.microphone_active
    assert manager._listener_generation == 2


@pytest.mark.parametrize("method", ["start_wakeword_detection", "start_clap_detection"])
def test_legacy_detectors_report_device_failure_and_can_stop(monkeypatch, method):
    manager = _bare_hardware_manager()
    manager.whisper_model = object()
    monkeypatch.setattr(hardware, "SOUND_AVAILABLE", True)
    monkeypatch.setattr(manager, "_select_microphone", lambda: (_ for _ in ()).throw(RuntimeError("no test input")))
    assert getattr(manager, method)().startswith("오류:")
    worker = manager.wakeword_thread if "wakeword" in method else manager.clap_thread
    worker.join(3)
    assert not worker.is_alive() and manager._stream_error == "no test input"
    assert not manager.running and not manager.microphone_active
    manager.stop_continuous_listen()
