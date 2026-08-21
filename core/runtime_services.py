"""배포본에 포함된 로컬 서비스와 모델 경로를 자동 구성한다."""

import os
from pathlib import Path
import subprocess
import sys
import time

import requests


class RuntimeServiceManager:
    def __init__(self):
        self.bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        self.ollama_process = None

    def configure_model_environment(self):
        if not getattr(sys, "frozen", False):
            return
        os.environ.setdefault("HF_HOME", str(self.bundle_root / "models" / "huggingface"))
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            str(self.bundle_root / "runtime" / "ms-playwright"),
        )

    @staticmethod
    def _ollama_ready() -> bool:
        try:
            return requests.get("http://127.0.0.1:11434/api/version", timeout=1).ok
        except requests.RequestException:
            return False

    def ensure_ollama(self, timeout: float = 30.0) -> bool:
        if self._ollama_ready():
            return True
        executable = self.bundle_root / "runtime" / "ollama" / "ollama.exe"
        models = self.bundle_root / "models" / "ollama"
        if not executable.is_file():
            print(f"[Runtime] 내장 Ollama 실행 파일이 없습니다: {executable}")
            return False
        env = os.environ.copy()
        env["OLLAMA_MODELS"] = str(models)
        env["OLLAMA_HOST"] = "127.0.0.1:11434"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.ollama_process = subprocess.Popen(
            [str(executable), "serve"],
            cwd=str(executable.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ollama_ready():
                print("[Runtime] 내장 Ollama 서버 준비 완료")
                return True
            if self.ollama_process.poll() is not None:
                break
            time.sleep(0.25)
        print("[Runtime] 내장 Ollama 서버를 시작하지 못했습니다.")
        return False

    def stop(self):
        if self.ollama_process is not None and self.ollama_process.poll() is None:
            self.ollama_process.terminate()
            try:
                self.ollama_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ollama_process.kill()


_runtime_service_manager = None


def get_runtime_service_manager() -> RuntimeServiceManager:
    global _runtime_service_manager
    if _runtime_service_manager is None:
        _runtime_service_manager = RuntimeServiceManager()
    return _runtime_service_manager
