from types import SimpleNamespace

import numpy as np
from scipy.io import wavfile

import core.audio_processor as audio_module
import core.tts_settings as settings_module
from core.audio_processor import AudioProcessor
from core.tts_settings import TTSSettingsManager


class FakeEngine:
    def __init__(self, voices):
        self.voices = voices

    def getProperty(self, name):
        assert name == "voices"
        return self.voices

    def stop(self):
        pass


def test_voice_selection_is_persisted(monkeypatch, tmp_path):
    voices = [
        SimpleNamespace(id="voice-a", name="Voice A", languages=["en-US"]),
        SimpleNamespace(id="voice-b", name="Voice B", languages=["ko-KR"]),
    ]
    monkeypatch.setattr(settings_module, "pyttsx3", SimpleNamespace(init=lambda: FakeEngine(voices)))
    path = tmp_path / "tts.json"
    manager = TTSSettingsManager(str(path))

    assert manager.select_voice("voice-b", "Voice B") is True
    restored = TTSSettingsManager(str(path))
    assert restored.selected_voice_id == "voice-b"
    assert restored.selected_voice_name == "Voice B"
    assert restored.resolve_voice(voices).id == "voice-b"


def test_unknown_voice_is_not_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings_module,
        "pyttsx3",
        SimpleNamespace(init=lambda: FakeEngine([])),
    )
    manager = TTSSettingsManager(str(tmp_path / "tts.json"))
    monkeypatch.setattr(manager, "_list_windows_voices", lambda: [])
    assert manager.select_voice("missing") is False


def test_windows_voice_fallback_is_used_when_pyttsx_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_module, "load_custom_voice_profiles", lambda: [])
    monkeypatch.setattr(
        settings_module,
        "pyttsx3",
        SimpleNamespace(init=lambda: (_ for _ in ()).throw(RuntimeError("COM unavailable"))),
    )
    manager = TTSSettingsManager(str(tmp_path / "tts.json"))
    fallback = [settings_module.TTSVoice("system-speech:Heami", "Heami", "ko-KR")]
    monkeypatch.setattr(manager, "_list_windows_voices", lambda: fallback)
    assert manager.list_voices() == fallback


def test_online_korean_voices_are_merged_with_local_voices(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_module, "load_custom_voice_profiles", lambda: [])
    manager = TTSSettingsManager(str(tmp_path / "tts.json"))
    local = [settings_module.TTSVoice("local", "Local Korean", "ko-KR")]
    online = [settings_module.TTSVoice("edge:ko-KR-Test", "Online Korean", "ko-KR", "edge")]
    monkeypatch.setattr(manager, "_list_local_voices", lambda: local)
    monkeypatch.setattr(manager, "_list_edge_voices", lambda: online)

    assert manager.list_voices(refresh=True) == local + online
    assert manager.select_voice("edge:ko-KR-Test") is True
    assert manager.selected_edge_voice == "ko-KR-Test"


def test_voice_address_is_saved_per_voice_and_personalizes_output(monkeypatch, tmp_path):
    manager = TTSSettingsManager(str(tmp_path / "tts.json"))
    voices = [
        settings_module.TTSVoice("gpt-sovits:Anis", "Anis", provider="gpt-sovits",
                                 default_address="지휘관님"),
        settings_module.TTSVoice("local", "Local Korean", "ko-KR"),
    ]
    monkeypatch.setattr(manager, "list_voices", lambda refresh=False: voices)

    assert manager.select_voice("gpt-sovits:Anis")
    assert manager.selected_address == "지휘관님"
    assert manager.personalize_address("알겠습니다, 보스.") == "알겠습니다, 지휘관님."
    assert manager.set_voice_address("gpt-sovits:Anis", "대장님")

    restored = TTSSettingsManager(str(tmp_path / "tts.json"))
    monkeypatch.setattr(restored, "list_voices", lambda refresh=False: voices)
    assert restored.selected_address == "대장님"


def test_voice_addresses_are_independent(monkeypatch, tmp_path):
    manager = TTSSettingsManager(str(tmp_path / "tts.json"))
    voices = [
        settings_module.TTSVoice("voice-a", "A"),
        settings_module.TTSVoice("voice-b", "B"),
    ]
    monkeypatch.setattr(manager, "list_voices", lambda refresh=False: voices)
    assert manager.set_voice_address("voice-a", "선생님")
    assert manager.set_voice_address("voice-b", "대장님")
    assert manager.get_voice_address("voice-a") == "선생님"
    assert manager.get_voice_address("voice-b") == "대장님"


def test_tts_playback_uses_nonblocking_continuous_player(monkeypatch, tmp_path):
    path = tmp_path / "voice.wav"
    stereo = np.array([[1000, -1000], [500, -500], [0, 0]], dtype=np.int16)
    wavfile.write(path, 24000, stereo)
    calls = []
    monkeypatch.setattr(
        audio_module.sd,
        "play",
        lambda data, rate, blocking: calls.append((data.copy(), rate, blocking)),
    )
    monkeypatch.setattr(audio_module.sd, "wait", lambda: calls.append("wait"))

    AudioProcessor().play_and_analyze_tts(str(path))

    played, rate, blocking = calls[0]
    assert played.shape == stereo.shape
    assert rate == 24000
    assert blocking is False
    assert calls[-1] == "wait"


def test_streaming_tts_prebuffers_and_preserves_all_pcm(monkeypatch):
    calls = []

    class FakeRawStream:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def start(self):
            calls.append("start")

        def write(self, data):
            calls.append(("write", bytes(data)))

        def stop(self):
            calls.append("stop")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(audio_module.sd, "RawOutputStream", FakeRawStream)
    processor = AudioProcessor()
    processor.play_streaming_tts(iter([
        (32000, 1, 2, b"\x01\x00" * 20),
        (32000, 1, 2, b"\x02\x00" * 20),
    ]), prebuffer_seconds=0.001)
    assert calls[0] == ("init", {"samplerate": 32000, "channels": 1, "dtype": "int16"})
    assert [item for item in calls if isinstance(item, tuple) and item[0] == "write"] == [
        ("write", b"\x01\x00" * 20 + b"\x02\x00" * 20),
    ]
    assert calls[-2:] == ["stop", "close"]
