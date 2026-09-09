import json
from pathlib import Path
from urllib.error import HTTPError
import io
from types import SimpleNamespace

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
    monkeypatch.setattr("core.tools.get_assistant_settings", lambda: SimpleNamespace(tts_enabled=True))
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.tts_settings = type(
        "Settings", (), {"selected_custom_voice": "Anis", "selected_edge_voice": ""}
    )()
    monkeypatch.setattr(
        executor, "_speak_with_custom_tts", lambda *_args: "TTS 오류: Anis 합성 실패"
    )
    monkeypatch.setattr(
        executor,
        "_speak_with_windows_speech",
        lambda *_args: pytest.fail("Heami fallback must not run"),
    )

    assert executor._speak_text_locked("안녕") == "TTS 오류: Anis 합성 실패"


def _runtime_profile(tmp_path):
    project = tmp_path / "project"
    (project / "config.py").parent.mkdir(parents=True)
    (project / "config.py").write_text("", encoding="utf-8")
    profile_path = project / "data" / "voices" / "Anis" / "profile.json"
    profile_path.parent.mkdir(parents=True)
    runtime = project / "runtime"
    runtime.mkdir()
    (runtime / "api_v2.py").write_text("", encoding="utf-8")
    (runtime / "voice.yaml").write_text("", encoding="utf-8")
    python = project / "python.exe"
    python.write_bytes(b"python")
    reference = project / "reference.wav"
    reference.write_bytes(b"wav")
    return {
        "_profile_path": str(profile_path),
        "id": "Anis",
        "runtime_root": "../../../runtime",
        "python": "../../../python.exe",
        "config": "voice.yaml",
        "reference_audio": "../../../reference.wav",
        "reference_text": "참조 문장",
        "port": 9988,
    }


def test_gpt_sovits_early_exit_reports_bounded_log_tail(tmp_path, monkeypatch):
    client = GPTSoVITSClient(_runtime_profile(tmp_path))
    monkeypatch.setattr(client, "_ready", lambda: False)

    class FailedProcess:
        returncode = 101

        @staticmethod
        def poll():
            return 101

    def fake_popen(*_args, **kwargs):
        kwargs["stdout"].write("torch DLL 초기화 실패\n")
        kwargs["stdout"].flush()
        return FailedProcess()

    monkeypatch.setattr("core.custom_tts.subprocess.Popen", fake_popen)
    with pytest.raises(RuntimeError) as exc_info:
        client.ensure_running(timeout=0.1)

    message = str(exc_info.value)
    assert "code=101" in message
    assert "torch DLL 초기화 실패" in message
    assert str(client.log_path) in message
    assert client.last_error == message


def test_gpt_sovits_runtime_validation_lists_missing_file(tmp_path):
    profile = _runtime_profile(tmp_path)
    Path(profile["_profile_path"]).parents[3].joinpath("reference.wav").unlink()
    client = GPTSoVITSClient(profile)

    with pytest.raises(RuntimeError, match="참조 음성"):
        client._runtime_paths()


def test_gpt_sovits_shutdown_prevents_late_background_start(tmp_path, monkeypatch):
    client = GPTSoVITSClient(_runtime_profile(tmp_path))
    monkeypatch.setattr(client, "_ready", lambda: False)
    monkeypatch.setattr(
        "core.custom_tts.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("종료 뒤에는 TTS 프로세스를 시작하면 안 됩니다."),
    )

    client.shutdown()

    with pytest.raises(RuntimeError, match="종료가 요청"):
        client.ensure_running(timeout=0.1)


def test_prepare_custom_tts_publishes_runtime_status(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tools.get_assistant_settings", lambda: SimpleNamespace(tts_enabled=True))
    manager = TTSSettingsManager(str(tmp_path / "tts.json"))
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.tts_settings = manager
    executor._custom_tts_clients = {}
    profile = {"id": "Anis", "name": "Anis", "provider": "gpt-sovits"}
    fake_client = SimpleNamespace(
        log_path=tmp_path / "anis.log",
        ensure_running=lambda: None,
    )
    monkeypatch.setattr("core.tools.load_custom_voice_profiles", lambda: [profile])
    monkeypatch.setattr(executor, "_get_custom_tts_client", lambda *_args: fake_client)

    result = executor.prepare_selected_tts("gpt-sovits:Anis")

    assert result["ready"] is True
    assert manager.get_backend_status("gpt-sovits:Anis")["state"] == "ready"


def test_custom_tts_playback_failure_is_not_reported_as_user_cancellation(tmp_path, monkeypatch):
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"wav")
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.tts_settings = SimpleNamespace(
        selected_custom_voice="Anis",
        selected_address="지휘관님",
    )
    profile = {"id": "Anis", "streaming_mode": 0, "chunk_chars": 90}
    client = SimpleNamespace(synthesize=lambda _text: str(audio_path))
    monkeypatch.setattr("core.tools.load_custom_voice_profiles", lambda: [profile])
    monkeypatch.setattr(executor, "_get_custom_tts_client", lambda *_args: client)

    class FailingAudioProcessor:
        @staticmethod
        def play_and_analyze_tts(_path, *, raise_on_error=False):
            assert raise_on_error is True
            raise RuntimeError("오디오 장치 연결 실패")

    result = executor._speak_with_custom_tts("안녕", FailingAudioProcessor())

    assert result.startswith("TTS 오류:")
    assert "오디오 장치 연결 실패" in result
    assert "취소됨" not in result
