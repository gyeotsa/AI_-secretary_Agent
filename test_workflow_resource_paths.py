"""Application recipes must not depend on the user's selected directory."""
import json
import sys
from pathlib import Path

from core.workflow_runtime import WorkflowRuntime


def test_default_recipes_work_outside_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runtime = WorkflowRuntime(db_path=str(tmp_path / "runs.db"))
    assert runtime.config_path.is_absolute()
    assert runtime.match_trigger("안녕?") is None
    assert runtime.match_trigger("업무 시작") == "work_start"
    assert not (tmp_path / "config").exists()


def test_explicit_relative_recipe_override_is_respected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("custom.json").write_text(json.dumps({"presets": [
        {"id": "custom", "trigger": ["테스트 시작"], "steps": []}
    ]}), encoding="utf-8")
    runtime = WorkflowRuntime("custom.json", db_path="runs.db")
    assert runtime.match_trigger("테스트 시작") == "custom"


def test_bundled_recipe_root_is_respected(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "workflows.json").write_text(
        '{"presets": []}', encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    runtime = WorkflowRuntime(db_path=str(tmp_path / "runs.db"))
    assert runtime.config_path == tmp_path / "config" / "workflows.json"
    assert runtime.match_trigger("안녕?") is None
