import sys
from pathlib import Path

from main_qt import get_runtime_warning


def test_suite_runs_with_project_virtual_environment():
    expected = (Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe").resolve()
    assert Path(sys.executable).resolve() == expected
    assert get_runtime_warning() == ""
