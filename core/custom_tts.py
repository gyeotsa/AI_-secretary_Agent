"""Local custom TTS voice discovery and GPT-SoVITS process management."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


VOICE_ROOT = Path("data/voices")


def load_custom_voice_profiles(root: Path = VOICE_ROOT) -> list[dict]:
    profiles: list[dict] = []
    if not root.exists():
        return profiles
    for path in sorted(root.glob("*/profile.json")):
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
            profile["_profile_path"] = str(path.resolve())
            if profile.get("provider") == "gpt-sovits" and profile.get("id"):
                profiles.append(profile)
        except (OSError, ValueError, TypeError) as exc:
            print(f"[TTS] 커스텀 음성 프로필 무시: {path}: {exc}")
    return profiles


class GPTSoVITSClient:
    def __init__(self, profile: dict):
        self.profile = profile
        self.profile_dir = Path(profile["_profile_path"]).parent
        self.port = int(profile.get("port", 9881))
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    def _resolve(self, value: str) -> Path:
        return (self.profile_dir / value).resolve()

    def _ready(self) -> bool:
        try:
            with urlopen(f"{self.base_url}/docs", timeout=1) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    def ensure_running(self, timeout: float = 60.0) -> None:
        if self._ready():
            return
        runtime_root = self._resolve(self.profile["runtime_root"])
        python = self._resolve(self.profile["python"])
        if not python.exists() or not runtime_root.exists():
            raise RuntimeError("GPT-SoVITS 런타임 또는 전용 Python을 찾을 수 없습니다.")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [
                str(python),
                "-s",
                "api_v2.py",
                "-a",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-c",
                str(self.profile["config"]),
            ],
            cwd=runtime_root,
            creationflags=creationflags,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ready():
                return
            if self.process.poll() is not None:
                raise RuntimeError(f"GPT-SoVITS가 종료됐습니다(code={self.process.returncode}).")
            time.sleep(0.5)
        raise TimeoutError("GPT-SoVITS 모델 로딩 시간이 초과됐습니다.")

    def synthesize(self, text: str) -> str:
        self.ensure_running()
        payload = {
            "text": text,
            "text_lang": self.profile.get("language", "ko"),
            "ref_audio_path": str(self._resolve(self.profile["reference_audio"])),
            "prompt_text": self.profile["reference_text"],
            "prompt_lang": self.profile.get("language", "ko"),
            "text_split_method": "cut5",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
            "sample_steps": 32,
        }
        request = Request(
            f"{self.base_url}/tts",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        fd, output = tempfile.mkstemp(prefix="jarvis-anis-", suffix=".wav")
        os.close(fd)
        try:
            with urlopen(request, timeout=180) as response:
                audio = response.read()
            if len(audio) < 1_000:
                raise RuntimeError("GPT-SoVITS가 비어 있는 오디오를 반환했습니다.")
            Path(output).write_bytes(audio)
            return output
        except Exception:
            Path(output).unlink(missing_ok=True)
            raise
