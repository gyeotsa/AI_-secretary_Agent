"""Voice initialization contracts without recording or downloading models."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from types import SimpleNamespace
import threading

import numpy as np
import pytest

import core.hardware as hardware


@pytest.fixture
def manager(monkeypatch):
    import core.gpu_scheduler as scheduler
    monkeypatch.setattr(hardware, "SOUND_AVAILABLE", True)
    monkeypatch.setattr(hardware.Config, "WHISPER_DEVICE", "cpu")
    monkeypatch.setattr(scheduler, "get_gpu_resource_queue", lambda: SimpleNamespace(reserve=lambda *a, **k: nullcontext()))
    monkeypatch.setattr(hardware, "_cuda_device_count", lambda: pytest.fail("CPU initialization must not probe CUDA"))
    return hardware.HardwareManager()


def test_constructor_never_loads_model_or_probes_gpu(monkeypatch):
    monkeypatch.setattr(hardware, "SOUND_AVAILABLE", True)
    monkeypatch.setattr(hardware, "_cuda_device_count", lambda: pytest.fail("eager GPU probe"))
    monkeypatch.setattr(hardware.HardwareManager, "_load_stt_model", lambda *a: pytest.fail("eager model load"))
    value = hardware.HardwareManager()
    assert value.whisper_model is None and value.stt_state == "not_loaded"
    assert not value.running


def test_first_use_loads_once_and_reuses_model(manager, monkeypatch):
    calls, model = [], object()
    monkeypatch.setattr(manager, "_load_stt_model", lambda: calls.append(1) or model)
    assert manager.ensure_stt_model() is model
    assert manager.ensure_stt_model(retry=True) is model
    assert calls == [1] and manager.stt_state == "ready"


def test_concurrent_first_use_shares_loader(manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, model = [], object()
    def load():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return model
    monkeypatch.setattr(manager, "_load_stt_model", load)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(manager.ensure_stt_model)
        assert entered.wait(3)
        others = [pool.submit(manager.ensure_stt_model) for _ in range(2)]
        release.set()
        assert all(f.result(timeout=3) is model for f in [first, *others])
    assert calls == [1]


def test_failure_cached_until_explicit_retry(manager, monkeypatch):
    calls, model = [], object()
    def load():
        calls.append(manager.stt_engine)
        if len(calls) == 1:
            manager.stt_engine = "openai-whisper"
            raise ValueError("synthetic load failure")
        return model
    monkeypatch.setattr(manager, "_load_stt_model", load)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic load failure"):
            manager.ensure_stt_model()
    assert len(calls) == 1 and manager.stt_state == "failed"
    assert manager.ensure_stt_model(retry=True) is model
    assert calls == [hardware.Config.STT_ENGINE] * 2


@pytest.mark.parametrize("queued", [True, False])
def test_direct_transcription_loads_without_microphone(manager, monkeypatch, queued):
    class Model:
        def transcribe(self, audio, **kwargs):
            assert kwargs["language"] == "ko"
            return {"text": "테스트"}
    def load():
        manager.stt_engine = "openai-whisper"
        return Model()
    monkeypatch.setattr(manager, "_load_stt_model", load)
    monkeypatch.setattr(manager, "_select_microphone", lambda: pytest.fail("transcription must not open a microphone"))
    transcribe = manager._transcribe_audio if queued else manager._transcribe_audio_unqueued
    assert transcribe(np.zeros(1600, dtype=np.float32))["text"] == "테스트"
    assert manager.stt_state == "ready"


@pytest.mark.parametrize("continuous", [True, False])
def test_stop_while_loading_never_opens_microphone(manager, monkeypatch, continuous):
    entered, release = threading.Event(), threading.Event()
    def load():
        entered.set()
        assert release.wait(3)
        return object()
    monkeypatch.setattr(manager, "_load_stt_model", load)
    monkeypatch.setattr(manager, "_select_microphone", lambda: pytest.fail("cancelled start opened microphone"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.start_continuous_listen, lambda text: None) if continuous else pool.submit(manager.start_wakeword_detection)
        assert entered.wait(3)
        manager.stop_continuous_listen() if continuous else manager.stop_wakeword_detection()
        release.set()
        assert "취소" in future.result(timeout=3)
    assert not manager.running


def test_shutdown_during_load_discards_model(manager, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def load():
        entered.set()
        assert release.wait(3)
        return object()
    monkeypatch.setattr(manager, "_load_stt_model", load)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.ensure_stt_model)
        assert entered.wait(3)
        manager.shutdown()
        release.set()
        with pytest.raises(RuntimeError, match="종료"):
            future.result(timeout=3)
    assert manager.whisper_model is None and not manager.running
    with pytest.raises(RuntimeError, match="종료"):
        manager.ensure_stt_model(retry=True)


def test_missing_audio_libraries_remain_text_only(monkeypatch):
    monkeypatch.setattr(hardware, "SOUND_AVAILABLE", False)
    value = hardware.HardwareManager()
    assert value.stt_state == "unavailable" and value.whisper_model is None
    assert value.start_continuous_listen(lambda text: None).startswith("오류:")


def test_microphone_worker_reports_via_signal_not_direct_ui():
    from main_qt import JarvisApp
    sent, displayed = [], []
    app = JarvisApp.__new__(JarvisApp)
    app.hardware_manager = SimpleNamespace(start_continuous_listen=lambda *a: "ready")
    app.audio_processor = None
    app.signals = SimpleNamespace(microphone_status=SimpleNamespace(emit=sent.append))
    app.window = SimpleNamespace(show_assistant_text=displayed.append)
    app._start_continuous_listen()
    assert sent == ["ready"] and displayed == []
    app._on_microphone_status(sent[0])
    assert displayed == ["ready"]
    app._runtime_shutdown_started = True
    app._on_microphone_status("late status")
    assert displayed == ["ready"]
