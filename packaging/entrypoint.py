"""Minimal frozen-app bootstrap.

Keep packaging verification independent from JARVIS' optional ML and automation
stack.  Normal launches still execute ``main_qt`` exactly as before, while the
packaging smoke path proves that the frozen launcher, sibling runtime, icon and
writable report path are healthy without importing Qt or the ML stack.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _resource_path(relative_path: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return bundle_root / relative_path


def _argument_value(prefix: str) -> str:
    for argument in sys.argv[1:]:
        if argument.startswith(prefix):
            return argument[len(prefix):]
    return ""


def _run_packaging_smoke() -> int:
    icon = _resource_path("assets/jarvis.ico")
    if not icon.is_file() or icon.stat().st_size <= 0:
        raise RuntimeError("배포 아이콘을 불러오지 못했습니다.")
    if getattr(sys, "frozen", False):
        runtime = Path(sys.executable).with_name("JARVIS-runtime.exe")
        if not runtime.is_file() or runtime.stat().st_size <= 0:
            raise RuntimeError(f"JARVIS runtime executable is missing: {runtime}")

    report_path = (
        _argument_value("--packaging-smoke-report=")
        or os.getenv("JARVIS_PACKAGING_SMOKE_REPORT", "").strip()
    )
    if report_path:
        Path(report_path).write_text("OK", encoding="utf-8")
    return 0


if __name__ == "__main__":
    if "--packaging-smoke" in sys.argv or os.getenv("JARVIS_PACKAGING_SMOKE") == "1":
        raise SystemExit(_run_packaging_smoke())
    if getattr(sys, "frozen", False):
        runtime = Path(sys.executable).with_name("JARVIS-runtime.exe")
        if not runtime.is_file():
            raise SystemExit(f"JARVIS runtime executable is missing: {runtime}")
        subprocess.Popen([str(runtime), *sys.argv[1:]], cwd=str(runtime.parent))
    else:
        repo_root = Path(__file__).resolve().parents[1]
        subprocess.Popen([sys.executable, str(repo_root / "main_qt.py"), *sys.argv[1:]], cwd=str(repo_root))
