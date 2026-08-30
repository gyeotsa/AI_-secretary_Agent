import os
import subprocess
import sys
from pathlib import Path


def test_packaging_smoke_bypasses_full_runtime_import(tmp_path):
    report = tmp_path / "smoke.txt"
    env = os.environ.copy()
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "packaging/entrypoint.py",
            "--packaging-smoke",
            f"--packaging-smoke-report={report}",
        ],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert report.read_text(encoding="utf-8") == "OK"


def test_pyinstaller_uses_minimal_bootstrap():
    spec = Path("packaging/Jarvis.spec").read_text(encoding="utf-8")
    assert '"packaging", "entrypoint.py"' in spec
    assert '[os.path.join(repo_root, "main_qt.py")]' in spec
    assert 'name="JARVIS-runtime"' in spec
    assert 'name="JARVIS"' in spec
    assert "launcher_a = Analysis" in spec
    assert "runtime_a = Analysis" in spec
    assert "filter_submodules=runtime_submodule" in spec
    assert '"pywinauto.linux"' in spec
    assert '"jamo", "g2pk"' in spec
    assert 'poppler_icu_names = {"icuuc.dll", "icudt78.dll", "icuin78.dll"}' in spec
    launcher = Path("packaging/entrypoint.py").read_text(encoding="utf-8")
    assert "PyQt6" not in launcher


def test_runtime_has_independent_qt_smoke_contract():
    source = Path("main_qt.py").read_text(encoding="utf-8")
    assert '"--runtime-smoke" in sys.argv' in source
    assert '"--runtime-smoke-report="' in source
    assert "QApplication(sys.argv)" in source
    assert "smoke_icon.isNull()" in source


def test_offline_release_bundles_every_default_role_model():
    script = Path("packaging/build_release.ps1").read_text(encoding="utf-8")
    for model in (
        "qwen2.5-coder:7b-instruct",
        "qwen2.5:7b-instruct",
        "gemma3:4b",
        "qwen2.5vl:7b",
    ):
        assert model in script
    assert "$missingPaths" in script
    assert "$payloadRoot) { Remove-Item" in script
    assert script.index("$missingPaths") < script.index("$payloadRoot) { Remove-Item")
