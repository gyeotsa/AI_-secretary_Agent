import os
from pathlib import Path

from core.runtime_services import RuntimeServiceManager


def test_frozen_runtime_configures_bundled_model_and_browser_paths(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    for name in ("HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "PLAYWRIGHT_BROWSERS_PATH"):
        monkeypatch.delenv(name, raising=False)

    manager = RuntimeServiceManager()
    manager.configure_model_environment()

    assert Path(os.environ["HF_HOME"]) == tmp_path / "models" / "huggingface"
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == tmp_path / "runtime" / "ms-playwright"
