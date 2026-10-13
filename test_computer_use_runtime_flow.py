"""Computer Use tool selection, input bounds and independent completion checks."""
import json
from unittest.mock import Mock, patch

import pytest

from core.computer_use import ComputerUseModel, ComputerUseRuntime, Observation, validate_decision
from core.plugin import PluginRegistry
from core.semantic_request import SemanticRequestInterpreter
from core.semantic_request import SemanticDecision
from core.tool_result import ToolRunStatus
from plugins.computer_use import ComputerUsePlugin
from test_computer_use import CLICK, DONE, Screen, ScriptedModel


def test_native_action_contract_is_bounded_to_observed_screen():
    observation = Screen().observe()
    observation.context["windows"] = {"w1": {"title": "Test document"}}
    for action in [
        {"action": "hotkey", "key": "Ctrl+S"},
        {"action": "double_click", "target": "e1"},
        {"action": "right_click", "target": "e1"},
        {"action": "switch_window", "target": "w1"},
        {"action": "drag", "x": 0, "y": 0, "to_x": 99, "to_y": 99},
    ]:
        assert validate_decision({"observation_id": observation.id, "reason": "test", **action},
                                 observation, allow_coordinates=True)["action"] == action["action"]
    for action in [
        {"action": "hotkey", "key": "Ctrl+L"},
        {"action": "press", "target": "e1", "key": "Ctrl+S"},
        {"action": "double_click", "target": "missing"},
        {"action": "switch_window", "target": "missing"},
        {"action": "drag", "x": 0, "y": 0, "to_x": 100, "to_y": 99},
    ]:
        with pytest.raises(ValueError):
            validate_decision({"observation_id": observation.id, "reason": "test", **action},
                              observation, allow_coordinates=True)
    with pytest.raises(ValueError):
        validate_decision({"observation_id": observation.id, "reason": "test",
                           "action": "move", "x": 1, "y": 1}, observation)


def test_native_target_actions_constrain_model_and_validation():
    from core.codex_client import CodexClient
    screen = Screen().observe()
    screen.targets["e1"]["actions"] = ["fill", "press"]
    client = Mock(spec=CodexClient)
    ComputerUseModel(client, provider="cloud").decide("입력", screen, [], 30)
    assert client.chat_structured.call_args.args[0][-1] == {"role": "user", "content": "입력"}
    variants = client.chat_structured.call_args.kwargs["json_schema"]["oneOf"]
    assert not any(v["properties"]["action"]["const"] == "click" for v in variants)
    assert any(v["properties"]["action"]["const"] == "fill" for v in variants)
    assert screen.payload()["targets"]["e1"]["actions"] == ["fill", "press"]
    with pytest.raises(ValueError, match="지원하지 않는"):
        validate_decision({"observation_id": screen.id, "reason": "test", **CLICK}, screen)


def test_native_model_targets_use_image_bounds_and_observed_control_semantics():
    observation = Screen().observe()
    observation.context["backend"] = "windows"
    observation.targets["e1"].update(bounds=[5, 10, 20, 30], rectangle=[-100, 400, -70, 440],
                                      automation_id="TabViewScrollNextButton", class_name="Button",
                                      control_identity={"token": "host-only"})
    target = observation.payload()["targets"]["e1"]
    assert target["bounds"] == [5, 10, 20, 30] and "rectangle" not in target
    assert target["automation_id"] == "TabViewScrollNextButton" and target["class_name"] == "Button"
    assert "control_identity" not in target


def test_action_history_preserves_supported_hotkey_without_input_content():
    screen = Screen()
    model = ScriptedModel({"action": "hotkey", "key": "Ctrl+N"}, DONE)
    result = ComputerUseRuntime(screen, model, lambda *args: True).run("새 문서")
    assert result.succeeded and result.evidence[0].data["steps"][0]["key"] == "Ctrl+N"


def test_file_completion_requires_readback_not_model_assertion():
    screen, model = Screen(), ScriptedModel(CLICK, DONE)
    model.verdict["evidence"] = "Only visible in canvas"
    readback = Mock(return_value={"path": "test.txt", "sha256": "verified-file-hash"})
    result = ComputerUseRuntime(screen, model, lambda *a: True).run("입력 후 저장", completion_check=readback)
    assert result.succeeded and readback.call_count == 1
    assert result.evidence[-1].kind == "file_readback"

    model = ScriptedModel(CLICK, DONE)
    readback = Mock(side_effect=ValueError("저장 파일이 요청 내용과 다릅니다."))
    result = ComputerUseRuntime(Screen(), model, lambda *a: True).run("입력 후 저장", completion_check=readback)
    assert result.status == ToolRunStatus.UNVERIFIED


def test_visual_quote_is_read_twice_and_changed_image_is_not_success():
    for changed in (False, True):
        screen, model = Screen(), ScriptedModel(CLICK, DONE)
        model.verdict["evidence"] = "완료 화면"
        calls = []
        def read_visible_text(observation, remaining):
            calls.append(observation.id)
            if changed and len(calls) == 2:
                original = screen.observe
                def observe():
                    latest = original()
                    latest.screenshot = b"different screen"
                    return latest
                screen.observe = observe
            return "완료 화면"
        model.read_visible_text = read_visible_text
        result = ComputerUseRuntime(screen, model, lambda *a: True).run("완료 화면 확인")
        assert len(calls) == 2 and result.succeeded is (not changed)


def test_explicit_local_provider_never_selects_cloud_and_passes_local_only():
    client = Mock()
    client.chat_structured.return_value = json.dumps({"observation_id": "1", "reason": "test", **CLICK})
    with patch("core.llm.OllamaClient", return_value=client), \
            patch("core.codex_client.CodexClient", side_effect=AssertionError("no cloud")):
        model = ComputerUseModel(provider="local", workflow_context="검증된 작업 순서")
    reservation = Mock()
    reservation.__enter__ = Mock(return_value=None)
    reservation.__exit__ = Mock(return_value=False)
    with patch("core.gpu_scheduler.get_gpu_resource_queue") as queue:
        queue.return_value.budget_mb = 2048
        queue.return_value.reserve.return_value = reservation
        model.decide("입력", Screen().observe(), [], 30)
    assert client.chat_structured.call_args.kwargs["local_only"] is True
    assert "검증된 작업 순서" in client.chat_structured.call_args.args[0][1]["content"]


def test_selected_local_client_keeps_local_transport_guard():
    from core.llm import OllamaClient
    client = Mock(spec=OllamaClient)
    client.chat_structured.return_value = json.dumps({"observation_id": "1", "reason": "test", **CLICK})
    model = ComputerUseModel(client)
    reservation = Mock()
    reservation.__enter__ = Mock(return_value=None)
    reservation.__exit__ = Mock(return_value=False)
    with patch("core.gpu_scheduler.get_gpu_resource_queue") as queue:
        queue.return_value.budget_mb = 2048
        queue.return_value.reserve.return_value = reservation
        model.decide("입력", Screen().observe(), [], 30)
    assert client.chat_structured.call_args.kwargs["local_only"] is True


def test_local_accessible_controls_use_reasoning_and_visual_verification_uses_vision():
    vision, reasoning = Mock(), Mock()
    with patch("core.llm.OllamaClient", side_effect=lambda role: vision if role == "computer_use" else reasoning):
        model = ComputerUseModel(provider="local")
    observation = Screen().observe()
    observation.context.update(backend="windows", actions=["click", "hotkey", "done", "blocked"])
    reservation = Mock(__enter__=Mock(return_value=None), __exit__=Mock(return_value=False))
    with patch("core.gpu_scheduler.get_gpu_resource_queue") as queue:
        queue.return_value.budget_mb = 2048
        queue.return_value.reserve.return_value = reservation
        model.decide("새 문서", observation, [], 120)
        assert reasoning.chat_structured.call_count == 1 and not vision.chat_structured.called
        assert not any(m.get("images") for m in reasoning.chat_structured.call_args.args[0])
        assert reasoning.chat_structured.call_args.kwargs["local_only"] is True
        model.verify("새 문서", observation, 120)
        assert any(m.get("images") for m in vision.chat_structured.call_args.args[0])
        observation.context["actions"].append("coordinate_click")
        model.decide("좌표 조작", observation, [], 120)
        assert vision.chat_structured.call_count == 2 and reasoning.chat_structured.call_count == 1


def test_selected_local_reasoning_remains_local_after_gpt_toggle():
    from core.llm import OllamaClient, SelectedLLMClient
    vision = OllamaClient("computer_use")
    selected = SelectedLLMClient.__new__(SelectedLLMClient)
    with patch.object(selected, "_active_client", return_value=vision), \
            patch("core.llm.get_local_llm_client", return_value=selected):
        model = ComputerUseModel(provider="selected")
    assert model.llm is vision and isinstance(model.action_llm, OllamaClient)
    observation = Screen().observe()
    observation.context.update(backend="windows", actions=["click", "hotkey", "done", "blocked"])
    reservation = Mock(__enter__=Mock(return_value=None), __exit__=Mock(return_value=False))
    with patch("core.llm.is_gpt_enabled", return_value=True), \
            patch("core.codex_client.CodexClient.__init__", side_effect=AssertionError("no cloud")), \
            patch.object(model.action_llm, "chat_structured", return_value={}) as chat, \
            patch("core.gpu_scheduler.get_gpu_resource_queue") as queue:
        queue.return_value.budget_mb = 2048
        queue.return_value.reserve.return_value = reservation
        model.decide("새 문서", observation, [], 120)
    assert chat.call_args.kwargs["local_only"] is True and not any(m.get("images") for m in chat.call_args.args[0])


@pytest.mark.parametrize("remaining, expected", [(300, 120), (25, 25)])
def test_vision_request_can_finish_cold_start_but_cannot_extend_task(remaining, expected):
    from core.codex_client import CodexClient
    client = Mock(spec=CodexClient)
    model = ComputerUseModel(client, provider="cloud")
    with patch("core.computer_use.time.monotonic", return_value=10):
        model.decide("입력", Screen().observe(), [], remaining)
    assert client.chat_structured.call_args.kwargs["request_timeout"] == expected


def test_anise_can_select_computer_use_without_user_naming_the_tool():
    registry = PluginRegistry()
    registry.register_plugin(ComputerUsePlugin())
    raw = '메모장에 새 문서를 열고 "아니스 입력 확인"을 입력한 뒤 저장해줘'
    data = {"relation": "new", "operation": "execute", "tool_names": ["computer_use_run"],
            "slots": {"goal": raw, "app": "메모장", "backend": "windows"}, "confidence": .99,
            "needs_clarification": False, "clarification_question": "", "control_scope": "current"}
    model = Mock()
    model.chat_structured.return_value = json.dumps(data, ensure_ascii=False)
    try:
        result = SemanticRequestInterpreter(model, registry).interpret(raw)
        assert result.grounded and result.tool_names == ("computer_use_run",)
        assert result.slots["goal"] == raw and not result.needs_clarification
    finally:
        registry.shutdown()


def test_file_verification_cannot_shorten_the_users_quoted_body():
    registry = PluginRegistry()
    registry.register_plugin(ComputerUsePlugin())
    raw = '메모장에 "전체 본문"을 입력하고 "C:/work/test.txt"에 저장해줘'
    data = {"relation": "new", "operation": "execute", "tool_names": ["computer_use_run"],
            "slots": {"goal": raw, "app": "메모장", "verify_file": "C:/work/test.txt",
                      "expected_text": "전체"}, "confidence": .99,
            "needs_clarification": False, "clarification_question": "", "control_scope": "current"}
    try:
        interpreter = SemanticRequestInterpreter(None, registry)
        result = interpreter._validate(raw, [], {}, json.dumps(data, ensure_ascii=False),
                                       {"computer_use_run"})
        assert not result.grounded and result.reason == "ungrounded_literal:expected_text"
    finally:
        registry.shutdown()


@pytest.mark.parametrize("app, accepted", [("notepad", True), ("notepad.exe", True),
                                          ("word", False), ("C:/evil/notepad.exe", False)])
def test_computer_app_alias_normalization_preserves_requested_app(app, accepted):
    registry = PluginRegistry()
    registry.register_plugin(ComputerUsePlugin())
    raw = '메모장에 "테스트"를 입력해줘'
    data = {"relation": "new", "operation": "execute", "tool_names": ["computer_use_run"],
            "slots": {"goal": raw, "app": app}, "confidence": .99,
            "needs_clarification": False, "clarification_question": "", "control_scope": "current"}
    try:
        with patch("plugins.windows_control.WindowsControlPlugin._aliases", return_value={"메모장": "notepad.exe"}):
            result = SemanticRequestInterpreter(None, registry)._validate(
                raw, [], {}, json.dumps(data), {"computer_use_run"})
        assert result.grounded is accepted
        if accepted:
            assert result.slots["app"] == "메모장" and result.slots["goal"] == raw
        else:
            assert result.reason == "ungrounded_literal:app"
    finally:
        registry.shutdown()


@pytest.mark.parametrize("reason", ["ungrounded_literal:app", "semantic_user_content_omitted"])
def test_invalid_action_contract_does_not_invent_a_missing_information_question(reason):
    registry = PluginRegistry()
    registry.register_plugin(ComputerUsePlugin())
    try:
        interpreter = SemanticRequestInterpreter(Mock(), registry)
        interpreter._interpret = Mock(return_value=SemanticDecision("메모장에 입력해줘", reason=reason))
        interpreter._recover_dialogue = Mock(side_effect=AssertionError("contract errors are not missing information"))
        result = interpreter.interpret("메모장에 입력해줘")
        assert result.reason == reason and not result.needs_clarification
        interpreter._recover_dialogue.assert_not_called()
    finally:
        registry.shutdown()


def test_open_document_tool_cannot_drop_requested_text_while_asking_a_question():
    from plugins.office_editing import OfficeEditingPlugin
    registry = PluginRegistry()
    registry.register_plugin(OfficeEditingPlugin())
    raw = '메모장에 "전체 본문"을 입력한 뒤 "C:/work/test.txt"에 저장해줘'
    data = {"relation": "new", "operation": "execute", "tool_names": ["office_com_open"],
            "slots": {}, "confidence": .99, "needs_clarification": True,
            "clarification_question": "Word 파일 경로는 무엇인가요?", "control_scope": "current"}
    try:
        result = SemanticRequestInterpreter(None, registry)._validate(
            raw, [], {}, json.dumps(data), {"office_com_open"})
        assert not result.grounded and result.reason == "semantic_user_content_omitted"
    finally:
        registry.shutdown()


def test_canvas_can_click_same_position_again_after_visible_page_change():
    class Canvas(Screen):
        def observe(self):
            observation = super().observe()
            observation.text = "검색 결과: 아니스"
            observation.screenshot = str(len(self.actions)).encode()
            return observation

        def execute(self, decision, check):
            check()
            self.actions.append(decision)

    click = {"action": "coordinate_click", "x": 10, "y": 10}
    canvas = Canvas()
    result = ComputerUseRuntime(canvas, ScriptedModel(click, click, DONE), lambda *args: True).run(
        "두 화면을 진행", allow_coordinates=True)
    assert result.succeeded and len(canvas.actions) == 2
