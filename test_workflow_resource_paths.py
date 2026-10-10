"""Application recipes must not depend on the user's selected directory."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.plugin import PluginRegistry
from core.tool_result import Evidence, ToolRunResult
from core.workflow_runtime import WorkflowRuntime
from plugins.browser_mail import BrowserMailPlugin


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


@pytest.mark.parametrize("text, preset, providers", [
    ("메일 현황 알려줘", "mail_brief", ["naver", "gmail"]),
    ("네이버와 구글 메일을 새로 수집해서 현황 알려줘", "mail_brief", ["naver", "gmail"]),
    ("네이버 메일 현황 알려줘", "naver_mail_brief", ["naver"]),
    ("네이버 메일을 새로 수집해서 현황 알려줘", "naver_mail_brief", ["naver"]),
    ("구글 메일 현황 알려줘", "gmail_mail_brief", ["gmail"]),
    ("구글 메일을 새로 수집해서 현황 알려줘", "gmail_mail_brief", ["gmail"]),
    ("Gmail 현황 알려줘", "gmail_mail_brief", ["gmail"]),
])
def test_mail_presets_collect_requested_providers_and_present_verified_summary(tmp_path, text, preset, providers):
    summary = "메일 확인 결과\n신규 수신 2건 · 회신 검토(AI 추정) 1건 · 미발송 초안 3건"
    brief = Mock()
    brief.collect.return_value = {"providers": [{"provider": name, "list_count": 4}
                                               for name in providers], "summary": summary}
    plugin = BrowserMailPlugin(Mock(), brief)
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    tools = SimpleNamespace(plugin_registry=registry,
                            execute_tool=Mock(side_effect=plugin.execute_tool))
    runtime = WorkflowRuntime(tool_executor=tools, db_path=str(tmp_path / "runs.db"))

    assert runtime.match_trigger(text) == preset
    run = runtime.execute(preset)
    tools.execute_tool.assert_called_once_with("browser_mail_collect_summary", {"providers": providers})
    assert brief.collect.call_args.kwargs["providers"] == providers
    assert run["status"] == "completed"
    assert json.loads(run["results"][0]["output"]["raw_output"]) == brief.collect.return_value
    assert runtime.present_run(run) == summary
    assert runtime.present_run(runtime.get_run(run["run_id"])) == summary


@pytest.mark.parametrize("text", [
    '"메일 현황 알려줘"', "'메일 현황 알려줘'", "메일 현황 알려줘라는 말은 무슨 뜻이야?",
    "메일 현황 알려줘 하지 마", "메일 현황을 알려주지 마", "메일 현황 알려줘 말고 대화하자",
    "네이버와 구글 메일을 새로 수집해서 현황 알려줘라는 요청을 실행하지 마",
    "메일 현황 알려줘. 그리고 메일을 보내줘", "Gmail 현황 알려줘라고 인용했어",
])
def test_mail_trigger_never_matches_quotes_negation_or_compound_instructions(tmp_path, text):
    runtime = WorkflowRuntime(db_path=str(tmp_path / "runs.db"))
    assert runtime.match_trigger(text) is None


@pytest.mark.parametrize("result", [
    ToolRunResult.failed(tool_name="browser_mail_collect_summary", error="메일 연결 확인 불가"),
    ToolRunResult.unverified(tool_name="browser_mail_collect_summary", raw_output="확인 불가"),
])
def test_failed_or_unverified_mail_never_uses_success_presentation(tmp_path, result):
    registry = Mock()
    tools = SimpleNamespace(plugin_registry=registry, execute_tool=Mock(return_value=result))
    runtime = WorkflowRuntime(tool_executor=tools, db_path=str(tmp_path / "runs.db"))
    run = runtime.execute("mail_brief")
    assert run["status"] == "failed"
    assert run["results"][0]["status"] == "failed"
    registry.present_result.assert_not_called()
    assert "실패" in runtime.present_run(run)


def test_only_opted_in_steps_use_actual_result_tool_presentation(tmp_path):
    recipe = tmp_path / "custom.json"
    recipe.write_text(json.dumps({"presets": [{"id": "test", "steps": [
        {"id": "plain", "action": "tool", "tool": "plain", "input": {}},
        {"id": "formatted", "action": "tool", "tool": "alias", "input": {}, "present_result": True},
    ]}]}), encoding="utf-8")
    result = ToolRunResult.successful(tool_name="actual", raw_output="raw fixture",
                                      evidence=[Evidence("fixture", "확인됨")])
    registry = Mock()
    registry.present_result.return_value = "확인된 결과"
    tools = SimpleNamespace(plugin_registry=registry, execute_tool=Mock(return_value=result))
    runtime = WorkflowRuntime(str(recipe), tool_executor=tools, db_path=str(tmp_path / "runs.db"))
    run = runtime.execute("test")
    assert "presentation" not in run["results"][0]["output"]
    registry.present_result.assert_called_once_with("actual", "raw fixture")
    assert run["results"][1]["output"]["presentation"] == "확인된 결과"
    assert runtime.present_run(run) != "확인된 결과"


@pytest.mark.parametrize("status, step_status, presentation", [
    ("failed", "completed", "완료 문구"), ("completed", "failed", "완료 문구"),
    ("completed", "completed", " "), ("completed", "completed", None),
])
def test_direct_presentation_requires_completed_tool_and_nonempty_text(status, step_status, presentation):
    run = {"status": status, "results": [{"action": "tool", "status": step_status,
                                          "output": {"presentation": presentation}}]}
    assert WorkflowRuntime.present_run(run) != presentation
