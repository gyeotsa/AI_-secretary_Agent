"""Local custom TTS voice discovery and GPT-SoVITS process management."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
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
        self.last_error = ""
        self.log_path = self._resolve_log_path()
        self._startup_lock = threading.Lock()
        self._shutdown_requested = threading.Event()

    def _resolve(self, value: str) -> Path:
        return (self.profile_dir / value).resolve()

    def _resolve_log_path(self) -> Path:
        configured = str(self.profile.get("log_path") or "").strip()
        if configured:
            return self._resolve(configured)
        project_root = next(
            (
                parent
                for parent in (self.profile_dir, *self.profile_dir.parents)
                if (parent / "config.py").is_file() or (parent / ".git").exists()
            ),
            self.profile_dir,
        )
        voice_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(self.profile.get("id") or "voice"))
        return project_root / "data" / "logs" / "tts" / f"gpt-sovits-{voice_id}.log"

    def _ready(self) -> bool:
        try:
            with urlopen(f"{self.base_url}/docs", timeout=1) as response:
                return response.status == 200
        except (OSError, URLError):
            return False

    def _runtime_paths(self) -> tuple[Path, Path, Path, Path, Path]:
        required_keys = ("runtime_root", "python", "config", "reference_audio")
        missing_keys = [key for key in required_keys if not str(self.profile.get(key) or "").strip()]
        if missing_keys:
            raise RuntimeError(
                "GPT-SoVITS 음성 프로필에 필수 설정이 없습니다: " + ", ".join(missing_keys)
            )
        runtime_root = self._resolve(str(self.profile["runtime_root"]))
        python = self._resolve(str(self.profile["python"]))
        api_script = runtime_root / "api_v2.py"
        config_value = Path(str(self.profile["config"]))
        config = config_value if config_value.is_absolute() else runtime_root / config_value
        reference_audio = self._resolve(str(self.profile["reference_audio"]))
        required_paths = {
            "GPT-SoVITS 런타임": runtime_root,
            "GPT-SoVITS 전용 Python": python,
            "GPT-SoVITS API": api_script,
            "GPT-SoVITS 설정": config,
            "참조 음성": reference_audio,
        }
        missing_paths = [f"{label}={path}" for label, path in required_paths.items() if not path.exists()]
        if missing_paths:
            raise RuntimeError("필수 TTS 파일을 찾을 수 없습니다: " + "; ".join(missing_paths))
        return runtime_root, python, api_script, config.resolve(), reference_audio

    def _log_tail(self, max_lines: int = 30, max_chars: int = 5_000) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        tail = "\n".join(lines[-max_lines:]).strip()
        return tail[-max_chars:]

    def _failure(self, message: str) -> RuntimeError:
        tail = self._log_tail()
        detail = f"{message} 로그: {self.log_path}"
        if tail:
            detail += f"\n--- GPT-SoVITS 로그 마지막 부분 ---\n{tail}"
        self.last_error = detail
        return RuntimeError(detail)

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
        if self._shutdown_requested.is_set():
            raise RuntimeError("GPT-SoVITS 종료가 요청되어 새 음성 서버를 시작하지 않습니다.")
        if self._ready():
            self.last_error = ""
            return
        with self._startup_lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("GPT-SoVITS 종료가 요청되어 새 음성 서버를 시작하지 않습니다.")
            if self._ready():
                self.last_error = ""
                return
            try:
                runtime_root, python, api_script, config, _reference_audio = self._runtime_paths()
            except Exception as exc:
                self.last_error = str(exc)
                raise

            if self.process is None or self.process.poll() is not None:
                if self._shutdown_requested.is_set():
                    raise RuntimeError("GPT-SoVITS 종료가 요청되어 새 음성 서버를 시작하지 않습니다.")
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process_env = self._build_process_env(runtime_root)
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with self.log_path.open("a", encoding="utf-8", errors="replace") as log_file:
                        log_file.write(
                            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"GPT-SoVITS 시작: voice={self.profile.get('id', '')}, port={self.port}\n"
                        )
                        log_file.flush()
                        self.process = subprocess.Popen(
                            [
                                str(python),
                                "-s",
                                str(api_script),
                                "-a",
                                "127.0.0.1",
                                "-p",
                                str(self.port),
                                "-c",
                                str(config),
                            ],
                            cwd=runtime_root,
                            env=process_env,
                            stdout=log_file,
                            stderr=subprocess.STDOUT,
                            creationflags=creationflags,
                        )
                except OSError as exc:
                    raise self._failure(f"GPT-SoVITS 프로세스를 시작하지 못했습니다: {exc}.") from exc

            process = self.process
            deadline = time.monotonic() + max(0.1, float(timeout))
            while time.monotonic() < deadline:
                if self._shutdown_requested.is_set():
                    self.shutdown()
                    raise RuntimeError("GPT-SoVITS 로딩 중 애플리케이션 종료가 요청되었습니다.")
                if self._ready():
                    self.last_error = ""
                    return
                if process.poll() is not None:
                    raise self._failure(
                        f"GPT-SoVITS가 종료됐습니다(code={process.returncode})."
                    )
                time.sleep(0.5)
            message = (
                "GPT-SoVITS 모델이 제한 시간 안에 준비되지 않았습니다. "
                "백그라운드 로딩은 계속되며 다음 음성 요청에서 다시 확인합니다. "
                f"로그: {self.log_path}"
            )
            self.last_error = message
            raise TimeoutError(message)

    def shutdown(self, timeout: float = 8.0) -> None:
        """Stop only the GPT-SoVITS process started by this client."""
        self._shutdown_requested.set()
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=max(0.1, float(timeout)))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        except OSError:
            pass

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
