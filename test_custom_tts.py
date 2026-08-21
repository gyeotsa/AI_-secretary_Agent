import json
from pathlib import Path
from urllib.error import HTTPError
import io

import pytest
import wave

from core.custom_tts import GPTSoVITSClient, load_custom_voice_profiles, split_tts_text
from core.tools import ToolExecutor
from core.tts_settings import TTSSettingsManager


def test_custom_profile_discovery(tmp_path):
    profile_dir = tmp_path / "Anis"
    profile_dir.mkdir()
    (profile_dir / "profile.json").write_text(
        json.dumps(
            {
                "id": "Anis",
                "name": "Anis",
                "provider": "gpt-sovits",
            }
        ),
        encoding="utf-8",
    )
    profiles = load_custom_voice_profiles(tmp_path)
    assert [item["id"] for item in profiles] == ["Anis"]


def test_tts_settings_lists_and_selects_custom_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.tts_settings.load_custom_voice_profiles",
        lambda: [{"id": "Anis", "name": "Anis", "language": "ko"}],
    )
    manager = TTSSettingsManager(str(tmp_path / "tts.json"))
    monkeypatch.setattr(manager, "_list_local_voices", lambda: [])
    monkeypatch.setattr(manager, "_list_edge_voices", lambda: [])
    assert manager.select_voice("gpt-sovits:Anis")
    assert manager.selected_custom_voice == "Anis"


def test_gpt_sovits_client_resolves_paths_from_profile(tmp_path):
    profile_path = tmp_path / "voices" / "Anis" / "profile.json"
    profile_path.parent.mkdir(parents=True)
    client = GPTSoVITSClient(
        {
            "_profile_path": str(profile_path),
            "port": 9988,
        }
    )
    assert client._resolve("../../runtime") == (tmp_path / "runtime").resolve()


def test_gpt_sovits_process_uses_bundled_nltk_data(tmp_path, monkeypatch):
    previous = str(tmp_path / "shared-nltk")
    monkeypatch.setenv("NLTK_DATA", previous)
    runtime = tmp_path / "runtime"

    process_env = GPTSoVITSClient._build_process_env(runtime)

    assert process_env["NLTK_DATA"].split(__import__("os").pathsep) == [
        str(runtime / "nltk_data"),
        previous,
    ]
    assert (runtime / "nltk_data").is_dir()


def test_tts_text_is_split_at_sentence_boundaries():
    assert split_tts_text("첫 문장입니다. 두 번째예요! 마지막 질문인가요?") == [
        "첫 문장입니다.",
        "두 번째예요!",
        "마지막 질문인가요?",
    ]


def test_gpt_sovits_stream_exposes_pcm_before_full_response(tmp_path, monkeypatch):
    profile_path = tmp_path / "Anis" / "profile.json"
    profile_path.parent.mkdir()
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"wav")
    client = GPTSoVITSClient(
        {
            "_profile_path": str(profile_path),
            "port": 9988,
            "language": "ko",
            "reference_audio": "../reference.wav",
            "reference_text": "참조 문장",
            "streaming_mode": 2,
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(32000)
        output.writeframes(b"\x01\x00" * 100)

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        "core.custom_tts.urlopen", lambda *_args, **_kwargs: Response(wav_buffer.getvalue())
    )
    chunks = list(client.stream_pcm("안녕"))
    assert chunks
    assert chunks[0][:3] == (32000, 1, 2)
    assert b"".join(item[3] for item in chunks) == b"\x01\x00" * 100


def test_gpt_sovits_http_error_includes_server_detail(tmp_path, monkeypatch):
    profile_path = tmp_path / "Anis" / "profile.json"
    profile_path.parent.mkdir()
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"wav")
    client = GPTSoVITSClient(
        {
            "_profile_path": str(profile_path),
            "port": 9988,
            "language": "ko",
            "reference_audio": "../reference.wav",
            "reference_text": "참조 문장",
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)

    def fail(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1:9988/tts",
            400,
            "Bad Request",
            {},
            io.BytesIO('{"message":"잘못된 참조 오디오"}'.encode()),
        )

    monkeypatch.setattr("core.custom_tts.urlopen", fail)
    with pytest.raises(RuntimeError, match="잘못된 참조 오디오"):
        client.synthesize("안녕")


def test_selected_custom_voice_does_not_fall_back_to_windows_voice(monkeypatch):
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.tts_settings = type(
        "Settings", (), {"selected_custom_voice": "Anis", "selected_edge_voice": ""}
    )()
    monkeypatch.setattr(
        executor, "_speak_with_custom_tts", lambda *_args: "TTS 오류: Anis 합성 실패"
    )
    monkeypatch.setattr(
        executor,
        "_speak_with_windows_tts",
        lambda *_args: pytest.fail("Heami fallback must not run"),
    )

    assert executor._speak_text_locked("안녕") == "TTS 오류: Anis 합성 실패"
