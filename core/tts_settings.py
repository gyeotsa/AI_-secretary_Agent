"""Windows TTS 음성 검색 및 영구 선택 설정."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Optional

try:
    import asyncio
    import edge_tts
except ImportError:
    asyncio = None
    edge_tts = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None


@dataclass(frozen=True)
class TTSVoice:
    id: str
    name: str
    languages: str = ""
    provider: str = "windows"


class TTSSettingsManager:
    def __init__(self, storage_path: str = "data/tts_settings.json"):
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.selected_voice_id = ""
        self.selected_voice_name = ""
        self.selected_provider = "windows"
        self._voice_cache: Optional[list[TTSVoice]] = None
        self._load()

    def _load(self):
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            self.selected_voice_id = str(data.get("voice_id", ""))
            self.selected_voice_name = str(data.get("voice_name", ""))
            self.selected_provider = str(data.get("provider", "windows"))
        except (OSError, ValueError, TypeError) as exc:
            print(f"[TTS] 음성 설정 로드 실패: {exc}")

    def _save(self):
        payload = {
            "voice_id": self.selected_voice_id,
            "voice_name": self.selected_voice_name,
            "provider": self.selected_provider,
        }
        self.storage_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def list_voices(self, refresh: bool = False) -> list[TTSVoice]:
        if self._voice_cache is not None and not refresh:
            return list(self._voice_cache)
        local_voices = self._list_local_voices()
        online_voices = self._list_edge_voices()
        self._voice_cache = local_voices + online_voices
        return list(self._voice_cache)

    def _list_local_voices(self) -> list[TTSVoice]:
        engine = None
        if pyttsx3 is not None:
            try:
                engine = pyttsx3.init()
                voices = [
                    TTSVoice(
                        id=str(voice.id),
                        name=str(voice.name),
                        languages=", ".join(str(item) for item in (voice.languages or [])),
                    )
                    for voice in engine.getProperty("voices")
                ]
                if voices:
                    return voices
            except Exception as exc:
                print(f"[TTS] pyttsx3 음성 목록 조회 실패, Windows 목록으로 전환: {exc}")
            finally:
                if engine is not None:
                    try:
                        engine.stop()
                    except Exception:
                        pass
        return self._list_windows_voices()

    @staticmethod
    def _list_edge_voices() -> list[TTSVoice]:
        if edge_tts is None or asyncio is None:
            return []
        try:
            voices = asyncio.run(edge_tts.list_voices())
            return [
                TTSVoice(
                    id=f"edge:{voice['ShortName']}",
                    name=str(voice.get("FriendlyName") or voice["ShortName"]),
                    languages=f"{voice.get('Locale', 'ko-KR')} · {voice.get('Gender', '')} · 온라인 Neural",
                    provider="edge",
                )
                for voice in voices
                if str(voice.get("Locale", "")).casefold() == "ko-kr"
            ]
        except Exception as exc:
            print(f"[TTS] Edge 한국어 음성 목록 조회 실패, 오프라인 음성만 사용: {exc}")
            return []

    @staticmethod
    def _list_windows_voices() -> list[TTSVoice]:
        if os.name != "nt":
            return []
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { @($s.GetInstalledVoices() | ForEach-Object { $i=$_.VoiceInfo; "
            "[pscustomobject]@{Name=$i.Name;Culture=$i.Culture.Name;Gender=$i.Gender.ToString()} "
            "}) | ConvertTo-Json -Compress } finally { $s.Dispose() }"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return []
            payload = json.loads(completed.stdout)
            if isinstance(payload, dict):
                payload = [payload]
            return [
                TTSVoice(
                    id=f"system-speech:{item['Name']}",
                    name=str(item["Name"]),
                    languages=" · ".join(
                        value for value in (str(item.get("Culture", "")), str(item.get("Gender", ""))) if value
                    ),
                )
                for item in payload
            ]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            print(f"[TTS] Windows 음성 목록 조회 실패: {exc}")
            return []

    def select_voice(self, voice_id: str, voice_name: str = "") -> bool:
        voices = self.list_voices()
        selected: Optional[TTSVoice] = next((voice for voice in voices if voice.id == voice_id), None)
        if selected is None:
            return False
        self.selected_voice_id = selected.id
        self.selected_voice_name = selected.name or voice_name
        self.selected_provider = selected.provider
        self._save()
        return True

    def resolve_voice(self, voices) -> Optional[object]:
        if self.selected_voice_id:
            selected = next(
                (
                    voice for voice in voices
                    if str(voice.id) == self.selected_voice_id
                    or str(voice.name) == self.selected_voice_name
                ),
                None,
            )
            if selected is not None:
                return selected
        return next(
            (
                voice for voice in voices
                if "ko" in str(voice.languages).casefold() or "korean" in str(voice.name).casefold()
            ),
            None,
        )

    @property
    def selected_edge_voice(self) -> str:
        if self.selected_provider == "edge" and self.selected_voice_id.startswith("edge:"):
            return self.selected_voice_id.split(":", 1)[1]
        return ""


_tts_settings_manager = None


def get_tts_settings_manager() -> TTSSettingsManager:
    global _tts_settings_manager
    if _tts_settings_manager is None:
        _tts_settings_manager = TTSSettingsManager()
    return _tts_settings_manager
