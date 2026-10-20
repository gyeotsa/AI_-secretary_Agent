import json
import time
from pathlib import Path

import pytest

from core.diagnostics_runtime import DiagnosticsRuntime
from core.task_contracts import (
    AcceptanceCriterion,
    ContractStatus,
    ResourceBudget,
    SupervisorRuntime,
    TaskContractStore,
)
from core.tool_result import Evidence, ToolRunResult
from core.workflow_runtime import MorningBriefService, WorkflowRuntime


class _ToolExecutor:
    def __init__(self):
        self.calls = []

    def execute_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "get_weather":
            return ToolRunResult.successful(
                tool_name=name,
                raw_output=json.dumps({"temperature": 27, "retrieved_at": "live-now"}),
                evidence=[Evidence("weather", "Open-Meteo 응답")],
            )
        if name == "calendar_read_range":
            return ToolRunResult.successful(
                tool_name=name,
                raw_output="일정 1개",
                evidence=[Evidence("calendar_events", "실시간 일정", {
                    "events": [{"title": "회의", "start": "09:00"}],
                    "retrieved_at": "calendar-live-now",
                })],
            )
        return ToolRunResult.successful(
            tool_name=name, raw_output="ok", evidence=[Evidence("runtime", "실행됨")],
        )


def test_supervisor_enforces_evidence_budget_timeout_and_cancel(tmp_path):
    supervisor = SupervisorRuntime(TaskContractStore(tmp_path / "contracts.db"))
    contract = supervisor.create_contract(
        goal="검증된 산출물 생성", specialist="tester",
        acceptance_criteria=[AcceptanceCriterion("file", "파일", "artifact")],
        resource_budget=ResourceBudget(timeout_seconds=0.01, ram_mb=1),
    )
    supervisor.begin_attempt(contract)
    with pytest.raises(TimeoutError):
        with supervisor.resource_guard(contract):
            time.sleep(0.02)
    failed = supervisor.verify(contract, artifacts=[], evidence=[])
    assert failed.status == ContractStatus.FAILED
    assert "검수 근거 누락" in failed.failure_reason

    cancellable = supervisor.create_contract(goal="취소 가능", specialist="tester")
    cancelled = supervisor.request_cancel(cancellable.contract_id)
    assert cancelled.status == ContractStatus.CANCELLED
    assert cancelled.cancel_requested is True


def test_workflow_resumes_exact_approval_boundary_and_redacts_secrets(tmp_path):
    config_path = tmp_path / "workflows.json"
    config_path.write_text(json.dumps({"presets": [{
        "id": "test", "label": "테스트", "trigger": ["테스트 시작"],
        "steps": [
            {"id": "before", "action": "tool", "tool": "get_time", "input": {}},
            {"id": "approval", "action": "memory_maintenance", "requires_approval": True},
            {"id": "after", "action": "tool", "tool": "get_time", "input": {}},
        ],
    }]}), encoding="utf-8")
    tools = _ToolExecutor()
    maintenance = []
    runtime = WorkflowRuntime(
        str(config_path), tool_executor=tools,
        memory_maintenance=lambda: maintenance.append("done") or {"saved": True},
        db_path=str(tmp_path / "runs.db"),
    )
    run = runtime.execute("test", context={"api_token": "secret", "visible": "ok"})
    assert run["status"] == "awaiting_approval"
    assert [item["step_id"] for item in run["results"]] == ["before", "approval"]
    assert run["context"]["api_token"] == "[REDACTED]"
    assert len(tools.calls) == 1

    resumed = runtime.resume(run["run_id"], approve=True)
    assert resumed["status"] == "completed"
    assert [item["step_id"] for item in resumed["results"]] == ["before", "approval", "after"]
    assert maintenance == ["done"]
    assert len(tools.calls) == 2
    assert runtime.match_trigger("테스트 시작해줘") == "test"


def test_morning_brief_uses_live_tool_evidence(monkeypatch):
    monkeypatch.setenv("JARVIS_CALENDAR_PROVIDER", "google")
    monkeypatch.setenv("JARVIS_CALENDAR_ACCOUNT", "owner@example.com")
    tools = _ToolExecutor()
    brief = MorningBriefService(tool_executor=tools).build(location="서울")
    sections = {item["key"]: item for item in brief["sections"]}
    assert sections["weather"]["status"] == "available"
    assert sections["weather"]["value"]["temperature"] == 27
    assert sections["calendar"]["value"][0]["title"] == "회의"
    assert sections["calendar"]["updated_at"] == "calendar-live-now"
    assert ("calendar_read_range", {"provider": "google", "account": "owner@example.com"}) in tools.calls


def test_diagnostics_never_marks_an_unrun_probe_passed(tmp_path, monkeypatch):
    runtime = DiagnosticsRuntime(report_path=str(tmp_path / "report.json"))
    passed = lambda: ("passed", "실제 검사 성공", {"executed": True}, "")
    monkeypatch.setattr(runtime, "_runtime", passed)
    monkeypatch.setattr(runtime, "_ollama", passed)
    monkeypatch.setattr(runtime, "_cuda", passed)
    monkeypatch.setattr(runtime, "_memory", passed)
    monkeypatch.setattr(runtime, "_workspace", passed)
    monkeypatch.setattr(runtime, "_plugins", passed)
    monkeypatch.setattr(runtime, "_tool_flow", passed)
    monkeypatch.setattr(runtime, "_scheduler_soak", passed)
    monkeypatch.setattr(runtime, "_microphone", passed)
    monkeypatch.setattr(runtime, "_speaker", lambda *, live: passed())
    report = runtime.run(scope="core", live=False)
    assert report["summary"]["status"] == "passed"
    assert all(item["evidence"].get("executed") for item in report["probes"])
    assert Path(runtime.report_path).is_file()


def test_interface_plugin_controls_live_bridge_and_reports_evidence():
    from core.interface_control import get_interface_control_bridge
    from plugins.interface_control import InterfaceControlPlugin

    bridge = get_interface_control_bridge()
    calls = []
    bridge.register("open_surface", lambda surface: calls.append(surface) or {"surface": surface})
    plugin = InterfaceControlPlugin()
    try:
        slots = plugin.extract_slots("interface.open_surface", "지식 그래프 열어줘", {})
        assert slots == {"surface": "knowledge_graph"}
        assert {tool.name for tool in plugin.get_tools()} == {"open_interface_surface"}
        opened = plugin.execute_tool("open_interface_surface", {"surface": "permissions"})
        assert opened.succeeded
        assert calls == ["permissions"]
    finally:
        bridge.unregister("open_surface")


def test_command_center_is_live_and_contract_cancel_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from ui.command_center import CommandCenterDialog

    app = QApplication.instance() or QApplication([])
    supervisor = SupervisorRuntime(TaskContractStore(tmp_path / "ui-contracts.db"))
    contract = supervisor.create_contract(goal="UI 제어", specialist="tester")

    class Runtime:
        def __init__(self):
            self.supervisor = supervisor

        def snapshot(self):
            return {
                "updated_at": "now", "workspace": {}, "contracts": supervisor.snapshot(),
                "dialogue_tasks": [], "teams": [], "plan_steps": [],
                "system": {"ram_percent": 10}, "gpu": {"active_requests": []},
                "plugins": [], "permissions": [],
                "diagnostics": {"summary": {"status": "not_run"}, "probes": []},
                "models": [], "automation": {"scheduler": {}, "jobs": []},
                "observer": {"status": "stopped"}, "workflow_runs": [], "actions": [],
                "artifacts": [], "acceptance": {"scenarios": []}, "events": [],
            }

    dialog = CommandCenterDialog(Runtime())
    assert dialog.task_table.columnCount() == 7
    assert dialog.task_table.item(0, 0).text() == contract.contract_id
    dialog.task_table.selectRow(0)
    assert "목표: UI 제어" in dialog.detail.toPlainText()
    assert '"contract_id"' not in dialog.detail.toPlainText()
    dialog._async_completed({"status": "completed", "summary": "자료 확인을 마쳤습니다.",
                             "data": {"access_token": "hidden-test-value", "count": 2}})
    assert "상태: 완료" in dialog.detail.toPlainText()
    assert "자료 확인을 마쳤습니다." in dialog.detail.toPlainText()
    assert "hidden-test-value" not in dialog.detail.toPlainText()
    dialog.task_table.selectRow(0)
    dialog._control_selected("취소")
    assert supervisor.store.get(contract.contract_id).status == ContractStatus.CANCELLED
    dialog.close()
    app.processEvents()
