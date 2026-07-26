import json
from pathlib import Path

from core.custom_tts import GPTSoVITSClient, load_custom_voice_profiles
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
