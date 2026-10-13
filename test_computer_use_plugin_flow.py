"""Tool discovery, task permission and private procedural-memory boundaries."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from core import computer_use_memory
from core.tool_result import Evidence, ToolRunResult, ToolRunStatus
from core.turn_context import TurnExecutionContext, bind_turn_context
from plugins import computer_use


def test_generic_tool_defaults_to_windows_without_a_manual_window():
    schema = computer_use.ComputerUsePlugin().get_tools()[0].input_schema
    validate = Draft202012Validator(schema)
    assert not list(validate.iter_errors({"goal": "메모장에서 내용 입력"}))
    assert schema["properties"]["backend"]["default"] == "windows"
    assert schema["properties"]["model_provider"]["default"] == "selected"
    for bad in ({"goal": "입력", "window_title": ""},
                {"goal": "검색", "backend": "browser"},
                {"goal": "저장", "verify_file": "output.txt"},
                {"goal": "열기", "app_arguments": ["file.txt"]}):
        assert list(validate.iter_errors(bad))


@pytest.fixture
def plugin_flow(monkeypatch):
    from core import computer_use_backends, llm, permission, productization
    manager = Mock()
    manager.request_permission.return_value = True
    manager.request_once.return_value = True
    monkeypatch.setattr(permission, "get_permission_manager", lambda: manager)
    monkeypatch.setattr(productization.SafeModeManager, "enabled", lambda self: False)
    monkeypatch.setattr(llm, "is_gpt_enabled", lambda: False)
    backend = Mock()
    backend.app_name = "notepad.exe"
    backend.__enter__ = Mock(return_value=backend)
    backend.__exit__ = Mock(return_value=False)
    factory = Mock(return_value=backend)
    monkeypatch.setattr(computer_use_backends, "WindowsComputerBackend", factory)
    model = Mock()
    monkeypatch.setattr(computer_use, "ComputerUseModel", model)
    retrieve = Mock(return_value="verified reference procedure")
    remember = Mock(return_value="procedure-id")
    bus = Mock()
    monkeypatch.setattr(computer_use, "retrieve_workflow", retrieve)
    monkeypatch.setattr(computer_use, "remember_workflow", remember)
    monkeypatch.setattr(computer_use, "get_event_bus", lambda: bus)
    status = SimpleNamespace(value=ToolRunStatus.SUCCEEDED)

    class Runtime:
        def __init__(self, backend, model, approve, checkpoint, progress):
            self.approve, self.checkpoint, self.progress = approve, checkpoint, progress
            self.last_observation = SimpleNamespace(
                targets={"field": {"name": "Private input", "role": "edit", "value": ""},
                         "save": {"name": "Save", "role": "button"}},
                context={"title": "notepad"},
            )

        def run(self, goal, **kwargs):
            status.received_goal = goal
            for step, decision in enumerate((
                    {"action": "fill", "target": "field", "text": "private-request-literal"},
                    {"action": "hotkey", "key": "Ctrl+S"},
                    {"action": "click", "target": "save"})):
                self.checkpoint()
                if not self.approve(goal, self.last_observation, decision):
                    return ToolRunResult.cancelled(tool_name="computer_use_run", message="denied")
                self.progress({"step": step, "action": decision["action"],
                               "target": decision.get("target", ""), "status": "dispatching"})
            return ToolRunResult("computer_use_run", status.value, "완료 private-request-literal",
                                 evidence=[Evidence("computer_use", "verified", {"evidence": "private-request-literal"})])

    monkeypatch.setattr(computer_use, "ComputerUseRuntime", Runtime)
    return SimpleNamespace(manager=manager, factory=factory, model=model, remember=remember,
                           retrieve=retrieve, bus=bus, status=status)


def test_normal_actions_share_only_this_task_approval_and_selected_model(plugin_flow, tmp_path, monkeypatch):
    monkeypatch.setattr(computer_use.Config.API_CONFIG, "DB_PATH", str(tmp_path / "memory.db"), raising=False)
    with bind_turn_context(TurnExecutionContext("turn-1", "session-1")):
        result = computer_use.ComputerUsePlugin().execute_tool("computer_use_run", {"goal": "내용 입력 후 저장"})
    assert result.succeeded
    assert plugin_flow.manager.request_once.call_count == 1
    assert plugin_flow.manager.request_once.call_args.args[0] == "computer_control"
    assert plugin_flow.model.call_args.kwargs == {
        "provider": "local", "workflow_context": "verified reference procedure"}
    assert plugin_flow.factory.call_args.args == ("",)
    assert plugin_flow.factory.call_args.kwargs["app"] == ""
    assert "private-request-literal" not in str(result.to_dict())
    assert not result.artifacts
    assert not list(tmp_path.rglob("*.png"))
    steps = plugin_flow.remember.call_args.args[2]
    assert steps[1] == {"action": "hotkey", "key": "Ctrl+S"}
    assert steps[2]["control"] == "저장"
    assert "private-request-literal" not in str(steps)
    events = [call.args[0] for call in plugin_flow.bus.publish.call_args_list]
    assert [events[0].type, events[-1].type] == ["computer_use.started", "computer_use.finished"]
    assert all(event.data["turn_id"] == "turn-1" for event in events)


def test_saved_file_goal_distinguishes_document_body_from_destination(plugin_flow, tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(computer_use.SafetyLayer, "validate_path", lambda path: (True, ""))
    path = tmp_path / "output.txt"
    body = '첫 줄\n둘째 줄 "원문"'
    result = computer_use.ComputerUsePlugin().execute_tool("computer_use_run", {
        "goal": "새 문서를 작성하고 지정한 파일로 저장해줘", "app": "메모장",
        "verify_file": str(path), "expected_text": body,
    })
    assert result.succeeded
    assert "저장할 파일 경로: " + json.dumps(str(path), ensure_ascii=False) in plugin_flow.status.received_goal
    assert plugin_flow.status.received_goal.endswith("문서에 입력할 전체 내용: " + json.dumps(body, ensure_ascii=False))
    assert body not in str(plugin_flow.remember.call_args.args[2])


@pytest.mark.parametrize("gpt_selected, expected", [(False, "local"), (True, "cloud")])
def test_selected_provider_is_fixed_before_scope_approval(plugin_flow, monkeypatch, gpt_selected, expected):
    from core import llm
    selection = [gpt_selected]
    monkeypatch.setattr(llm, "is_gpt_enabled", lambda: selection[0])
    def approve(permission, description):
        selection[0] = not gpt_selected
        return True
    plugin_flow.manager.request_once.side_effect = approve
    result = computer_use.ComputerUsePlugin().execute_tool("computer_use_run", {"goal": "내용 입력"})
    assert result.succeeded
    assert plugin_flow.model.call_args.kwargs["provider"] == expected
    description = plugin_flow.manager.request_once.call_args.args[1]
    assert ("로컬 PC의 AI" if expected == "local" else "Codex 클라우드 모델로 전송") in description
    assert "화면 이미지와 접근성 메타데이터" in description


@pytest.mark.parametrize("provider, gpt_selected", [("local", True), ("cloud", False)])
def test_explicit_provider_and_disclosed_destination_override_current_selection(
        plugin_flow, monkeypatch, provider, gpt_selected):
    from core import llm
    monkeypatch.setattr(llm, "is_gpt_enabled", lambda: gpt_selected)
    result = computer_use.ComputerUsePlugin().execute_tool(
        "computer_use_run", {"goal": "내용 입력", "model_provider": provider})
    assert result.succeeded
    assert plugin_flow.model.call_args.kwargs["provider"] == provider
    description = plugin_flow.manager.request_once.call_args.args[1]
    assert ("로컬 PC의 AI" if provider == "local" else "Codex 클라우드 모델로 전송") in description
    assert "화면 이미지와 접근성 메타데이터" in description


def test_denied_task_never_opens_an_app_or_queries_memory(plugin_flow):
    plugin_flow.manager.request_once.return_value = False
    result = computer_use.ComputerUsePlugin().execute_tool("computer_use_run", {"goal": "내용 입력"})
    assert result.status == ToolRunStatus.CANCELLED
    plugin_flow.factory.assert_not_called()
    plugin_flow.retrieve.assert_not_called()
    plugin_flow.remember.assert_not_called()


def test_coordinates_require_separate_task_scope_and_failures_are_not_learned(plugin_flow):
    plugin_flow.status.value = ToolRunStatus.UNVERIFIED
    result = computer_use.ComputerUsePlugin().execute_tool(
        "computer_use_run", {"goal": "내용 입력", "allow_coordinates": True, "model_provider": "local"})
    assert result.status == ToolRunStatus.UNVERIFIED
    assert [call.args[0] for call in plugin_flow.manager.request_once.call_args_list] == [
        "computer_control", "coordinate_control"]
    assert plugin_flow.model.call_args.kwargs["provider"] == "local"
    plugin_flow.remember.assert_not_called()


def test_consequential_ui_actions_and_existing_text_still_require_confirmation():
    reason = computer_use.ComputerUsePlugin._confirmation_reason
    obs = SimpleNamespace(context={"title": "메일 작성"},
                          targets={"send": {"name": "보내기"}, "field": {"name": "본문", "value": "existing draft"}})
    assert reason(obs, {"action": "click", "target": "send"})
    assert reason(obs, {"action": "fill", "target": "field", "text": "replacement"})
    assert reason(obs, {"action": "press", "target": "field", "key": "Enter"})
    assert not reason(obs, {"action": "hotkey", "key": "Ctrl+S"})
    obs.targets["field"]["control_type"] = "Document"
    assert reason(obs, {"action": "hotkey", "key": "Ctrl+V"})
    assert reason(obs, {"action": "type_text", "text": "replacement"})


def test_saved_text_readback_requires_changed_exact_content(tmp_path, monkeypatch):
    path = tmp_path / "output.txt"
    path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(computer_use.SafetyLayer, "validate_path", lambda path: (True, ""))
    check, selected = computer_use.ComputerUsePlugin._file_check({
        "verify_file": str(path), "expected_text": "아니스\n저장 검증"})
    assert selected == path
    with pytest.raises(ValueError, match="변경되지"):
        check()
    path.write_text("wrong", encoding="utf-8")
    with pytest.raises(ValueError, match="일치하지"):
        check()
    path.write_bytes("아니스\r\n저장 검증".encode("utf-8"))
    assert check()["exact_text_match"]


def test_workflow_rag_stores_semantics_without_input_literals_or_screen_data(monkeypatch):
    from core import rag
    store, index = Mock(), Mock()
    store.remember.return_value = "record-1"
    monkeypatch.setattr(computer_use_memory, "get_knowledge_memory", lambda: store)
    monkeypatch.setattr(rag, "get_rag_manager", lambda: index)
    step = computer_use_memory.semantic_step("fill", {"role": "edit", "name": "private literal"})
    with bind_turn_context(TurnExecutionContext("turn-1", "session-1", memory_namespace="private-workspace")):
        computer_use_memory.remember_workflow("notepad.exe", '"private literal" 입력 후 저장', [step], "run-1")
        index.search_with_confidence.return_value = {"answerable": True, "results": [{"content": "safe procedure"}]}
        assert "safe procedure" in computer_use_memory.retrieve_workflow("notepad.exe", '"private literal" 입력 후 저장')
    record = store.remember.call_args.args[0]
    assert "private literal" not in record.content
    assert record.workspace_namespace == "private-workspace"
    assert index.add_text_document.call_args.kwargs["namespace"] == "private-workspace"
    assert index.search_with_confidence.call_args.args[0] == "notepad.exe 입력 저장"
    assert index.search_with_confidence.call_args.kwargs["metadata_filter"] == {
        "workflow_type": "computer_use", "app": "notepad.exe"}
