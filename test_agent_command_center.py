import json
import time
from pathlib import Path

import pytest

from core.diagnostics_runtime import DiagnosticsRuntime
from core.gesture_runtime import GestureRuntime
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


class _Point:
    def __init__(self, x=0.5, y=1.0):
        self.x = x
        self.y = y


def _hand(extended=(), thumb=False):
    points = [_Point() for _ in range(21)]
    for tip, pip in zip((8, 12, 16, 20), (6, 10, 14, 18)):
        points[pip].y = 0.5
        points[tip].y = 0.2 if tip in extended else 0.8
    points[2].y, points[3].y, points[4].y = (0.6, 0.4, 0.2) if thumb else (0.2, 0.4, 0.6)
    return points


def _motion_hand(*, center_x=0.5, open_hand=True):
    points = [_Point(center_x, 0.65) for _ in range(21)]
    points[0] = _Point(center_x, 0.82)
    points[5] = _Point(center_x - 0.10, 0.63)
    points[9] = _Point(center_x, 0.55)
    points[13] = _Point(center_x + 0.06, 0.63)
    points[17] = _Point(center_x + 0.11, 0.68)
    for offset, (tip, pip) in enumerate(zip((8, 12, 16, 20), (6, 10, 14, 18))):
        points[pip] = _Point(center_x + (offset - 1.5) * 0.045, 0.52)
        points[tip] = _Point(center_x + (offset - 1.5) * 0.07, 0.20 if open_hand else 0.72)
    points[4] = _Point(center_x - (0.18 if open_hand else 0.02), 0.53)
    return points


def test_gesture_classifier_maps_only_explicit_hand_shapes():
    assert GestureRuntime._classify(_hand((8, 12, 16, 20))) == "stop_tts"
    assert GestureRuntime._classify(_hand((), thumb=True)) == "approve"
    assert GestureRuntime._classify(_hand(())) == "cancel"
    assert GestureRuntime._classify(_hand((8,))) == "switch_workspace"
    assert GestureRuntime._classify(_hand((8, 12))) == ""


def test_gesture_commands_are_opt_in_and_never_open_workspace_by_default():
    runtime = GestureRuntime(actions={"switch_workspace": lambda: None})
    assert runtime._recognizers == []


def test_opt_in_gesture_command_requires_stable_hold_and_release():
    calls = []
    runtime = GestureRuntime(enable_command_gestures=True, actions={"approve": lambda: calls.append("approve")})
    thumb = _hand((), thumb=True)
    runtime._recognize_discrete(thumb, timestamp=1.0)
    runtime._recognize_discrete(thumb, timestamp=1.5)
    assert calls == []
    runtime._recognize_discrete(thumb, timestamp=1.81)
    runtime._recognize_discrete(thumb, timestamp=2.8)
    assert calls == ["approve"]
    runtime._recognize_discrete(_hand((8, 12)), timestamp=3.0)
    runtime._recognize_discrete(thumb, timestamp=4.0)
    runtime._recognize_discrete(thumb, timestamp=4.81)
    assert calls == ["approve", "approve"]


def test_gesture_motion_preserves_continuous_zoom_and_swipe_speed():
    runtime = GestureRuntime()
    closed = runtime._motion_sample(_motion_hand(open_hand=False), timestamp=1.0)
    runtime._smooth_motion = None
    runtime._previous_center = None
    opened = runtime._motion_sample(_motion_hand(open_hand=True), timestamp=2.0)
    assert opened.openness > closed.openness
    assert opened.zoom > closed.zoom

    runtime._smooth_motion = None
    runtime._previous_center = None
    runtime._motion_sample(_motion_hand(center_x=0.28), timestamp=3.0)
    candidate = runtime._motion_sample(_motion_hand(center_x=0.48), timestamp=3.05)
    assert candidate.swipe_phase == "candidate"
    assert candidate.swipe_velocity == 0.0
    fast_swipe = runtime._motion_sample(_motion_hand(center_x=0.68), timestamp=3.10)
    assert fast_swipe.swipe_phase == "started"
    assert fast_swipe.swipe_velocity > 0.0


def test_interface_plugin_controls_live_bridge_and_reports_evidence():
    from core.interface_control import get_interface_control_bridge
    from plugins.interface_control import InterfaceControlPlugin

    bridge = get_interface_control_bridge()
    calls = []
    bridge.register("set_gesture_camera", lambda enabled: {
        "running": bool(enabled), "enabled": True, "error": "", "camera_index": 0,
    })
    bridge.register("get_gesture_status", lambda: {
        "running": True, "enabled": True, "error": "", "camera_index": 0,
    })
    bridge.register("open_surface", lambda surface: calls.append(surface) or {"surface": surface})
    plugin = InterfaceControlPlugin()
    try:
        slots = plugin.extract_slots("interface.gesture_camera", "카메라 꺼줘", {})
        assert slots == {"enabled": False}
        result = plugin.execute_tool("set_gesture_camera_control", slots)
        assert result.succeeded
        assert "꺼짐" in result.raw_output
        opened = plugin.execute_tool("open_interface_surface", {"surface": "permissions"})
        assert opened.succeeded
        assert calls == ["permissions"]
    finally:
        for name in ("set_gesture_camera", "get_gesture_status", "open_surface"):
            bridge.unregister(name)


def test_brain_orbit_uses_gesture_speed_and_real_surface_signals(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from ui.brain_orbit import BrainOrbitWidget

    app = QApplication.instance() or QApplication([])
    widget = BrainOrbitWidget()
    widget.resize(900, 360)
    initial_zoom = widget.target_zoom
    widget.apply_gesture_motion({
        "zoom": 1.0, "zoom_active": False, "velocity_x": 1.5,
        "swipe_velocity": 1.5, "timestamp": 1.0, "hand_count": 1,
        "gesture_hand_count": 1, "tracking_state": "tracking",
    })
    widget.apply_gesture_motion({
        "zoom": 0.1, "zoom_active": True, "gesture_mode_active": True,
        "swipe_velocity": 0.0, "timestamp": 1.1, "hand_count": 2,
        "gesture_hand_count": 2, "tracking_state": "tracking",
    })
    widget.apply_gesture_motion({
        "zoom": 1.0, "zoom_active": True, "gesture_mode_active": True,
        "swipe_velocity": 0.0, "timestamp": 1.2, "hand_count": 2,
        "gesture_hand_count": 2, "tracking_state": "tracking",
    })
    assert widget.target_zoom > initial_zoom
    assert widget.zoom_response > 0.13
    assert widget.angular_velocity < 0.0
    widget.set_camera_status({"running": True, "error": ""})
    assert widget.camera_running is True
    widget.close()
    app.processEvents()


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
    dialog._control_selected("취소")
    assert supervisor.store.get(contract.contract_id).status == ContractStatus.CANCELLED
    dialog.close()
    app.processEvents()
