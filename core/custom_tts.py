"""Local custom TTS voice discovery and GPT-SoVITS process management."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


VOICE_ROOT = Path("data/voices")


def split_tts_text(text: str, max_chars: int = 90) -> list[str]:
    """Split speech at natural boundaries so playback can begin early."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    sentences = re.findall(r".+?(?:[.!?。！？]+(?=\s|$)|$)", normalized)
    chunks: list[str] = []
    for sentence in (item.strip() for item in sentences if item.strip()):
        while len(sentence) > max_chars:
            boundary = sentence.rfind(" ", 0, max_chars + 1)
            if boundary < max_chars // 2:
                boundary = max_chars
            chunks.append(sentence[:boundary].strip())
            sentence = sentence[boundary:].strip()
        if sentence:
            chunks.append(sentence)
    return chunks or [normalized]


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

    @staticmethod
    def _build_process_env(runtime_root: Path) -> dict[str, str]:
        """Expose bundled NLP resources to the isolated GPT-SoVITS process."""
        process_env = os.environ.copy()
        nltk_data = runtime_root / "nltk_data"
        nltk_data.mkdir(parents=True, exist_ok=True)
        existing_nltk_data = process_env.get("NLTK_DATA", "")
        process_env["NLTK_DATA"] = os.pathsep.join(
            value for value in (str(nltk_data), existing_nltk_data) if value
        )
        return process_env

    def ensure_running(self, timeout: float = 60.0) -> None:
        if self._ready():
            return
        runtime_root = self._resolve(self.profile["runtime_root"])
        python = self._resolve(self.profile["python"])
        if not python.exists() or not runtime_root.exists():
            raise RuntimeError("GPT-SoVITS 런타임 또는 전용 Python을 찾을 수 없습니다.")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process_env = self._build_process_env(runtime_root)
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
            env=process_env,
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
        payload = self._build_payload(text)
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
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            Path(output).unlink(missing_ok=True)
            raise RuntimeError(f"GPT-SoVITS HTTP {exc.code}: {detail}") from exc
        except Exception:
            Path(output).unlink(missing_ok=True)
            raise

    def _build_payload(self, text: str) -> dict:
        clean_text = unicodedata.normalize("NFC", str(text))
        clean_text = "".join(
            character for character in clean_text
            if character in "\n\t" or unicodedata.category(character)[0] != "C"
        ).strip()
        if not clean_text:
            clean_text = "네, 듣고 있어요."
        return {
            "text": clean_text,
            "text_lang": self.profile.get("language", "ko"),
            "ref_audio_path": str(self._resolve(self.profile["reference_audio"])),
            "prompt_text": self.profile["reference_text"],
            "prompt_lang": self.profile.get("language", "ko"),
            "text_split_method": "cut5",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": False,
            "sample_steps": int(self.profile.get("sample_steps", 32)),
            "speed_factor": float(self.profile.get("speed_factor", 1.0)),
        }

    def stream_pcm(self, text: str):
        """Yield streamed PCM as (sample_rate, channels, sample_width, bytes)."""
        self.ensure_running()
        payload = self._build_payload(text)
        payload["streaming_mode"] = int(self.profile.get("streaming_mode", 2))
        request = Request(
            f"{self.base_url}/tts",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=180) as response:
                header = response.read(44)
                if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                    raise RuntimeError("GPT-SoVITS 스트림의 WAV 헤더가 올바르지 않습니다.")
                channels = int.from_bytes(header[22:24], "little")
                sample_rate = int.from_bytes(header[24:28], "little")
                sample_width = int.from_bytes(header[34:36], "little") // 8
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    yield sample_rate, channels, sample_width, chunk
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GPT-SoVITS HTTP {exc.code}: {detail}") from exc
