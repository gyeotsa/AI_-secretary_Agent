"""Computer Use checks: no external accounts or real user applications are modified."""
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from core.computer_use import (ComputerUseModel, ComputerUseRuntime, Observation,
                               StaleObservation, validate_decision)
from core.permission import PermissionDecision, PermissionManager
from core.plugin import CancellationToken, PluginRegistry
from core.tool_result import ToolRunStatus
from plugins.computer_use import ComputerUsePlugin


class Screen:
    def __init__(self):
        self.actions = []
        self.text = "검색"
        self.reads = 0
        self.validate_action = Mock()
        self.restore_after_approval = Mock()

    def observe(self):
        self.reads += 1
        return Observation(str(self.reads), b"screen", self.text,
                           {"e1": {"name": "검색"}}, {"width": 100, "height": 100})

    def execute(self, decision, check):
        check()
        self.actions.append(decision)
        self.text = "검색 결과: 아니스"


class ScriptedModel:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.verdict = {"complete": True, "evidence": "검색 결과: 아니스", "reason": "검색 결과를 확인했습니다."}

    def decide(self, goal, observation, history, remaining):
        return {"observation_id": observation.id, "reason": "요청한 작업", **next(self.actions)}

    def verify(self, goal, observation, remaining):
        return self.verdict


CLICK = {"action": "click", "target": "e1"}
DONE = {"action": "done", "evidence": "검색 결과: 아니스"}


def test_observe_act_then_independent_readback_and_no_false_completion():
    screen = Screen()
    model = ScriptedModel(CLICK, DONE)
    approvals = []
    result = ComputerUseRuntime(screen, model, lambda *args: approvals.append(args) or True).run("아니스 검색")
    assert result.succeeded and len(screen.actions) == len(approvals) == 1
    assert screen.reads >= 4
    assert result.evidence[0].data["steps"][-1]["status"] == "verified"

    for verdict in ({"complete": False, "evidence": "검색", "reason": "미완료"},
                    {"complete": True, "evidence": "존재하지 않는 성공", "reason": "완료 주장"}):
        model = ScriptedModel(DONE)
        model.verdict = verdict
        result = ComputerUseRuntime(Screen(), model, lambda *a: True).run("아니스 검색")
        assert result.status == ToolRunStatus.UNVERIFIED


def test_changed_final_screen_is_not_success():
    screen = Screen()
    screen.text = "검색 결과: 아니스"
    model = ScriptedModel(DONE)
    def verify(*args):
        screen.text = "오류 발생"
        return model.verdict
    model.verify = verify
    assert not ComputerUseRuntime(screen, model, lambda *a: True).run("아니스 검색").succeeded


@pytest.mark.parametrize("change", ["checked", "value_hash"])
def test_changed_final_element_state_is_not_success(change):
    screen = Screen()
    reads = []
    def observe():
        reads.append(1)
        final = len(reads) >= 3
        return Observation(str(len(reads)), b"same screen", "알림 설정", {
            "e1": {"name": "알림 설정", "checked": not final if change == "checked" else True}},
            {"width": 100, "height": 100}, host_state={"value_hashes": {
                "e1": "changed" if final and change == "value_hash" else "original"}})
    screen.observe = observe
    model = ScriptedModel(DONE)
    model.verdict = {"complete": True, "evidence": "알림 설정", "reason": "켜짐 확인"}
    assert ComputerUseRuntime(screen, model, lambda *a: True).run("알림 설정 켜기").status == ToolRunStatus.UNVERIFIED


def test_deny_cancel_and_timeout_after_approval_never_dispatch():
    for scenario in ("deny", "cancel", "timeout"):
        screen, token = Screen(), CancellationToken()
        clock = [100.0]
        def approve(*args):
            if scenario == "cancel":
                token.cancel()
            if scenario == "timeout":
                clock[0] = 200.0
            return scenario != "deny"
        with patch("core.computer_use.time.monotonic", side_effect=lambda: clock[0]):
            result = ComputerUseRuntime(screen, ScriptedModel(CLICK), approve,
                                        token.raise_if_cancelled).run("검색", timeout_seconds=10)
        assert not screen.actions and not result.succeeded


def test_stale_target_is_reobserved_and_never_uses_previous_approval():
    screen, approvals = Screen(), []
    screen.validate_action.side_effect = [None, StaleObservation("changed"), None, None]
    result = ComputerUseRuntime(screen, ScriptedModel(CLICK, CLICK, DONE),
                                lambda *a: approvals.append(1) or True).run("아니스 검색")
    assert result.succeeded and len(approvals) == 2 and len(screen.actions) == 1
    assert result.evidence[0].data["steps"][0]["status"] == "stale_skipped"


def test_unknown_model_inputs_cannot_dispatch():
    screen = Screen()
    observation = screen.observe()
    bad = [
        {"action": "click", "target": "invented"},
        {"action": "click", "target": "e1", "script": "alert(1)"},
        {"action": "fill", "target": "e1"},
        {"action": "coordinate_click", "x": True, "y": 0},
        {"action": "coordinate_click", "x": 100, "y": 0},
        {"action": "press", "target": "e1", "key": "Control+L"},
        {"action": "switch_tab", "target": "missing"},
    ]
    for action in bad:
        with pytest.raises(ValueError):
            validate_decision({"observation_id": observation.id, "reason": "test", **action},
                              observation, allow_coordinates=True)
    with pytest.raises(ValueError):
        validate_decision({"observation_id": observation.id, "reason": "test",
                           "action": "coordinate_click", "x": 0, "y": 0}, observation)
    with pytest.raises(StaleObservation):
        validate_decision({"observation_id": "old", "reason": "test", **CLICK}, observation)


def test_uncertain_action_is_not_retried_and_repeated_screen_stops():
    screen = Screen()
    def uncertain(decision, check):
        screen.actions.append(decision)
        raise TimeoutError("action may have been delivered")
    screen.execute = uncertain
    result = ComputerUseRuntime(screen, ScriptedModel(CLICK, CLICK), lambda *a: True).run("검색")
    assert result.status == ToolRunStatus.UNVERIFIED and len(screen.actions) == 1

    screen = Screen()
    screen.execute = lambda decision, check: screen.actions.append(decision)
    result = ComputerUseRuntime(screen, ScriptedModel(CLICK, CLICK), lambda *a: True).run("검색")
    assert result.status == ToolRunStatus.UNVERIFIED and len(screen.actions) == 1


def test_step_budget_still_allows_final_verification_but_no_extra_action():
    screen = Screen()
    result = ComputerUseRuntime(screen, ScriptedModel(CLICK, DONE), lambda *a: True).run("아니스 검색", max_steps=1)
    assert result.succeeded and len(screen.actions) == 1
    screen = Screen()
    result = ComputerUseRuntime(screen, ScriptedModel(CLICK, CLICK), lambda *a: True).run("검색", max_steps=1)
    assert result.status == ToolRunStatus.PARTIAL and len(screen.actions) == 1


def test_one_time_confirmation_never_reuses_or_persists_allow():
    with tempfile.TemporaryDirectory() as directory:
        manager = PermissionManager(str(Path(directory) / "permissions.json"))
        manager.grant_permission("computer_control")
        before = manager.storage_path.read_bytes()
        callback = Mock(return_value=True)
        manager.set_request_callback(callback)
        assert manager.request_once("computer_control", "입력: 아니스")
        assert manager.request_once("computer_control", "클릭: 검색")
        assert callback.call_count == 2 and manager.storage_path.read_bytes() == before
        request = callback.call_args.args[0]
        assert not request.persist_decision and request.description == "클릭: 검색"
        callback.return_value = False
        assert not manager.request_once("computer_control", "deny")
        assert manager.permissions["computer_control"].decision == PermissionDecision.ALLOW
        manager.revoke_permission("computer_control")
        assert not manager.request_once("computer_control", "blocked")
        assert callback.call_count == 3


def test_plugin_contracts_and_natural_language_routing():
    from core.intent_router import IntentRouter
    registry = PluginRegistry()
    try:
        plugin = ComputerUsePlugin()
        registry.register_plugin(plugin)
        assert not registry.validate_contracts()
        contract = registry.get_capability("computer_use_run")
        assert contract.cancellable and not contract.automatic_retry_allowed
        assert registry.validate_tool_call("computer_use_run", {"goal": "검색", "backend": "browser"})
        assert registry.validate_tool_call("computer_use_run", {"goal": "입력", "backend": "windows", "window_title": ""})
        assert registry.validate_tool_call("computer_use_run", {"goal": "검색", "backend": "browser", "url": "https://example.org", "profile": "../escape"})
        for text, backend in [
            ("브라우저에서 직접 https://example.org 검색창에 아니스를 입력해줘", "browser"),
            ('"테스트 메모장" 창에서 직접 아니스를 입력해줘', "windows"),
        ]:
            resolution = IntentRouter(registry).resolve(text)
            assert resolution.tool_name == "computer_use_run" and resolution.slots["backend"] == backend
            assert resolution.slots["goal"] == text and resolution.ready
        followup = plugin.extract_slots("computer.use_windows", "제목 없음 - 메모장",
                                        {"goal": "본문에 아니스 입력", "backend": "windows"})
        assert followup["window_title"] == "제목 없음 - 메모장"
    finally:
        registry.shutdown()


def test_model_sends_image_and_schema_without_native_identity():
    screen = Screen().observe()
    screen.targets["e1"]["control_identity"] = {"token": "private-native-token"}
    screen.host_state = {"value_hashes": {"e1": "private-whole-value-hash"},
                         "focused_identity": {"token": "private-focused-token"}}
    client = Mock()
    client.chat_structured.return_value = json.dumps({"observation_id": screen.id, "reason": "test", **CLICK})
    class Reservation:
        def __enter__(self): pass
        def __exit__(self, *args): pass
    with patch("core.gpu_scheduler.get_gpu_resource_queue") as queue:
        queue.return_value.budget_mb = 1024
        queue.return_value.reserve.return_value = Reservation()
        model = ComputerUseModel(client)
        assert validate_decision(model.decide("검색", screen, [], 20), screen)["action"] == "click"
    call = client.chat_structured.call_args
    assert call.args[0][1]["images"] and call.kwargs["json_schema"]
    assert "private-native-token" not in json.dumps(call.args)
    assert "private-whole-value-hash" not in json.dumps(call.args)
    assert "private-focused-token" not in json.dumps(call.args)
    for variant in call.kwargs["json_schema"]["oneOf"]:
        fields = list(variant["properties"])
        if "target" in fields:
            assert fields.index("action") < fields.index("target")
            assert variant["properties"]["target"]["enum"] == ["e1"]


def test_codex_vision_bypasses_local_gpu_admission():
    from core.codex_client import CodexClient

    screen = Screen().observe()
    client = Mock(spec=CodexClient)
    client.chat_structured.return_value = json.dumps({"observation_id": screen.id, "reason": "test", **CLICK})
    with patch("core.gpu_scheduler.get_gpu_resource_queue", side_effect=AssertionError("cloud needs no GPU")) as queue:
        model = ComputerUseModel(client)
        assert validate_decision(model.decide("검색", screen, [], 20), screen)["action"] == "click"
        queue.assert_not_called()
    assert 0 < client.chat_structured.call_args.kwargs["request_timeout"] <= 20
    assert client.chat_structured.call_args.kwargs["json_schema"]


def test_windows_revalidates_identity_value_and_foreground_before_input():
    from core.computer_use_backends import WindowsComputerBackend, _png
    backend = WindowsComputerBackend("테스트 메모장")
    backend.window = SimpleNamespace(handle=100, process_id=200)
    identity = {"stable": True, "token": "native-original"}
    target = {"name": "입력", "automation_id": "edit", "rectangle": [0, 0, 100, 100],
              "control_type": "Edit", "control_identity": identity, "value": "이전 값"}
    wrapper = SimpleNamespace(element_info=SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=False)))
    backend.controls, backend.targets = {"e1": wrapper}, {"e1": target}
    automation = Mock()
    automation.verify_foreground.return_value = SimpleNamespace(process_id=200)
    automation.verify_accessibility_control.return_value = target
    automation._read_wrapper_value.return_value = "이전 값"
    backend.automation = automation
    observation = Observation("1", _png(Image.new("RGB", (100, 100))), "이전 값", {"e1": target},
                              {"width": 100, "height": 100, "actions": ["fill", "press"]},
                              host_state={"value_hashes": {"e1": hashlib.sha256("이전 값".encode()).hexdigest()}})
    decision = {"action": "fill", "target": "e1", "text": "아니스"}
    backend.validate_action(observation, decision)
    backend.execute(decision, lambda: None)
    assert automation.set_accessibility_text.call_args.kwargs["expected_identity"] is identity
    automation.set_accessibility_text.reset_mock()
    automation._read_wrapper_value.return_value = "사용자가 새로 쓴 내용"
    with pytest.raises(StaleObservation):
        backend.validate_action(observation, decision)
    automation.set_accessibility_text.assert_not_called()
    automation.verify_foreground.side_effect = RuntimeError("다른 앱으로 포커스 이동")
    with pytest.raises(RuntimeError):
        backend.execute(decision, lambda: None)
    automation.set_accessibility_text.assert_not_called()


def test_browser_full_value_hash_detects_hidden_suffix_without_exposing_it():
    from core.computer_use_backends import BrowserComputerBackend, _element_state, _value_hash
    full_value = "A" * 1000 + "private suffix"
    info = {"connected": True, "name": "입력", "value": full_value[:1000],
            "type": "text", "secret": False, "href": "", "options": []}
    element = Mock()
    element.evaluate.side_effect = lambda *_a: {**info, "_full_value": full_value}
    visible, fingerprint = _element_state(element)
    assert "_full_value" not in visible and fingerprint == _value_hash(full_value)
    observation = Observation("1", b"frame", visible["value"], {"e1": visible}, {
        "url": "https://example.org", "actions": ["fill"]}, host_state={"value_hashes": {"e1": fingerprint}})
    assert "private suffix" not in json.dumps(observation.payload())
    assert fingerprint not in json.dumps(observation.payload())
    backend = BrowserComputerBackend.__new__(BrowserComputerBackend)
    backend.page = SimpleNamespace(url="https://example.org")
    backend._check_navigation = lambda *_a: None
    backend.elements = {"e1": element}
    decision = {"action": "fill", "target": "e1", "text": "replacement"}
    backend.validate_action(observation, decision)
    full_value = "A" * 1000 + "new user suffix"
    with pytest.raises(StaleObservation, match="전체"):
        backend.validate_action(observation, decision)
    element.fill.assert_not_called()


def test_windows_full_value_hash_detects_hidden_suffix():
    from core.computer_use_backends import WindowsComputerBackend, _value_hash
    backend = WindowsComputerBackend("테스트")
    backend.window = SimpleNamespace(handle=100, process_id=200)
    backend._root = Mock()
    backend.automation = Mock()
    full_value = "A" * 1000 + "private suffix"
    target = {"name": "입력", "automation_id": "edit", "rectangle": [0, 0, 100, 100],
              "control_type": "Edit", "control_identity": {"stable": True, "token": "edit"},
              "value": full_value[:1000]}
    wrapper = SimpleNamespace(element_info=SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=False)))
    backend.controls = {"e1": wrapper}
    backend.automation.verify_accessibility_control.return_value = target
    backend.automation._read_wrapper_value.return_value = full_value
    observation = Observation("1", b"frame", target["value"], {"e1": target}, {"actions": ["fill"]},
                              host_state={"value_hashes": {"e1": _value_hash(full_value)}})
    decision = {"action": "fill", "target": "e1", "text": "replacement"}
    backend.validate_action(observation, decision)
    backend.automation._read_wrapper_value.return_value = "A" * 1000 + "new user suffix"
    with pytest.raises(StaleObservation, match="전체"):
        backend.validate_action(observation, decision)
    backend.automation.set_accessibility_text.assert_not_called()
    assert "private suffix" not in json.dumps(observation.payload())
    assert _value_hash(full_value) not in json.dumps(observation.payload())


def test_windows_global_typing_pins_observed_focus_after_approval_and_before_dispatch():
    from core.computer_use_backends import WindowsComputerBackend
    backend = WindowsComputerBackend("테스트")
    backend.window = SimpleNamespace(handle=100, process_id=200)
    backend.automation = Mock()
    root = Mock()
    root.capture_as_image.return_value = Image.new("RGB", (10, 10))
    root.rectangle.return_value = [0, 0, 10, 10]
    root.window_text.return_value = "테스트"
    backend._root = lambda: root
    backend.automation._rectangle_value.return_value = [0, 0, 10, 10]
    focus = {"A": True, "B": False}
    wrappers = {key: SimpleNamespace(has_keyboard_focus=lambda key=key: focus[key],
        element_info=SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=False))) for key in focus}
    metadata = {key: {"name": key, "control_type": "Edit", "control_identity": {
        "stable": True, "token": key}} for key in focus}
    backend.automation._accessibility_candidates.return_value = [(0, wrappers[key], metadata[key]) for key in focus]
    backend.automation._read_wrapper_value.return_value = "initial"
    def focused_control(_handle, *, expected_identity, **kwargs):
        assert kwargs["require_keyboard_focus"] and kwargs["require_foreground"]
        key = expected_identity["token"]
        if not focus[key]:
            raise RuntimeError("focus changed")
        return wrappers[key], metadata[key]
    backend.automation._accessibility_text_control.side_effect = focused_control
    observation = backend.observe()
    assert observation.host_state["focused_identity"]["token"] == "A"
    decision = {"action": "type_text", "text": "approved text"}
    backend.validate_action(observation, decision)
    with patch("core.desktop_messaging.DesktopMessagingRuntime._type_unicode") as send, patch("core.computer_use_backends._wait"):
        backend.execute(decision, lambda: None)
        send.assert_called_once_with("approved text")
        send.reset_mock()
        focus.update(A=False, B=True)
        with pytest.raises(StaleObservation, match="포커스"):
            backend.validate_action(observation, decision)
        with pytest.raises(StaleObservation, match="포커스"):
            backend.execute(decision, lambda: None)
        send.assert_not_called()


def test_browser_scope_blocks_other_origins_even_when_public():
    from core.computer_use_backends import BrowserComputerBackend
    with patch("plugins.browser.BrowserPlugin._validate_url", side_effect=lambda u: u):
        backend = BrowserComputerBackend("https://example.org/start", profile_path=Path("unused"))
    with pytest.raises(ValueError):
        backend._check_navigation("https://other.example.org/checkout")
    route = Mock()
    route.request.url = "https://other.example.org/checkout"
    route.request.is_navigation_request.return_value = True
    backend._route(route)
    route.abort.assert_called_once()
    route.continue_.assert_not_called()


def test_one_time_gui_response_is_bound_to_its_request_and_text_is_plain():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QLabel
    from main_qt import JarvisApp
    from ui.main_window import PermissionRequestDialog
    app = QApplication.instance() or QApplication([])
    dialog = PermissionRequestDialog("Computer Use", "<b>입력 내용</b>", persist_decision=False)
    labels = [item for item in dialog.findChildren(QLabel) if "입력 내용" in item.text()]
    assert labels and labels[0].textFormat() == Qt.TextFormat.PlainText
    assert "저장되지 않습니다" in labels[0].text()
    dialog.close()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = SimpleNamespace(request_permission=Mock(return_value=True))
    old = {"name": "old", "description": "old", "event": threading.Event(),
           "cancelled": threading.Event(), "result": False}
    old["cancelled"].set()
    jarvis._on_action_permission_request(old)
    assert old["event"].is_set() and not old["result"]
    jarvis.window.request_permission.assert_not_called()
    current = {"name": "new", "description": "new", "event": threading.Event(),
               "cancelled": threading.Event(), "result": False}
    jarvis._on_action_permission_request(current)
    assert current["result"] and current["event"].is_set() and not old["result"]
    assert jarvis.window.request_permission.call_args.kwargs["persist_decision"] is False
    jarvis.window.request_permission.side_effect = RuntimeError("window closed")
    current["event"].clear()
    jarvis._on_action_permission_request(current)
    assert current["event"].is_set() and not current["result"]
    app.processEvents()


@pytest.mark.integration
@pytest.mark.parametrize("use_live_model", [False, True])
def test_real_chromium_loop_and_stale_dom(use_live_model):
    """Real Chromium on a synthetic page; scripted or opt-in local model decisions."""
    if use_live_model and os.getenv("ANIS_COMPUTER_USE_LIVE_MODEL") != "1":
        pytest.skip("Set ANIS_COMPUTER_USE_LIVE_MODEL=1 to exercise the configured Ollama vision model")
    from playwright.sync_api import sync_playwright
    from core.computer_use_backends import BrowserComputerBackend
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 800, "height": 600})
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            page.set_content('<label>검색<input id="q"></label><button onclick="document.querySelector(\'output\').textContent=\'검색 결과: \'+document.querySelector(\'input\').value">검색하기</button><output></output>')
            # Inject an already-owned offline page; production still validates all public URLs.
            with patch("plugins.browser.BrowserPlugin._validate_url", side_effect=lambda u: u):
                backend = BrowserComputerBackend("https://example.org", profile_path=Path(directory))
            backend.page, backend.context = page, context
            backend.tabs = {"tab1": page}
            backend._check_navigation = lambda url: None
            model = ScriptedModel({"action": "fill", "target": "e1", "text": "아니스"},
                                  {"action": "click", "target": "e2"}, DONE)
            if use_live_model:
                model = ComputerUseModel()
            decisions = []
            decide = model.decide
            def traced_decide(*args):
                raw = decide(*args)
                decisions.append(raw)
                return raw
            model.decide = traced_decide
            result = ComputerUseRuntime(backend, model, lambda *a: True).run(
                "검색 입력창에 '아니스'를 입력하고 '검색하기' 버튼을 클릭하세요. "
                "화면에 '검색 결과: 아니스'가 나오면 완료입니다.", max_steps=6, timeout_seconds=180)
            if not result.succeeded:
                print(json.dumps({"result": result.to_dict(), "decisions": decisions}, ensure_ascii=False, indent=2))
            assert result.succeeded, {"result": result.to_dict(), "decisions": decisions}
            assert page.locator("output").inner_text() == "검색 결과: 아니스"
            snapshot = backend.observe()
            page.locator("button").evaluate("el => el.outerHTML = '<button>다른 버튼</button>'")
            with pytest.raises(StaleObservation):
                backend.validate_action(snapshot, {"action": "click", "target": "e2"})
            page.set_content('<label>언어<select><option>English</option><option>한국어</option></select></label>')
            snapshot = backend.observe()
            decision = {"action": "select", "target": "e1", "text": "한국어"}
            backend.validate_action(snapshot, decision)
            backend.execute(decision, lambda: None)
            assert page.locator("select").input_value() == "한국어"
            snapshot = backend.observe()
            with pytest.raises(ValueError, match="선택지"):
                backend.validate_action(snapshot, {**decision, "text": "관찰하지 않은 선택지"})
            backend._dispose_elements()
        finally:
            browser.close()


@pytest.mark.integration
def test_real_windows_owned_fixture():
    """Opt-in native input, confined to a uniquely named test process we own."""
    if os.name != "nt" or os.getenv("ANIS_COMPUTER_USE_NATIVE_TEST") != "1":
        pytest.skip("Set ANIS_COMPUTER_USE_NATIVE_TEST=1 for an isolated Windows test window")
    from core.computer_use_backends import WindowsComputerBackend
    from core.windows_automation import WindowsAutomationRuntime
    title = "Anis Computer Use Test " + uuid.uuid4().hex
    environment = {**os.environ, "QT_QPA_PLATFORM": "windows"}
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), title], env=environment,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 15
        while not any(w.title == title and w.foreground for w in WindowsAutomationRuntime().list_windows()):
            if time.monotonic() >= deadline or child.poll() is not None:
                pytest.fail("Windows did not activate the owned test window")
            time.sleep(0.1)
        with WindowsComputerBackend(title) as backend:
            observation = backend.observe()
            edits = [key for key, value in observation.targets.items() if value["control_type"] == "Edit"]
            assert len(edits) == 1, observation.payload()
            decision = {"action": "fill", "target": edits[0], "text": "아니스 테스트"}
            backend.validate_action(observation, decision)
            backend.execute(decision, lambda: None)
            observation = backend.observe()
            assert "아니스 테스트" in observation.text
            buttons = [key for key, value in observation.targets.items() if value["name"] == "확인"]
            assert len(buttons) == 1, observation.payload()
            decision = {"action": "click", "target": buttons[0]}
            backend.validate_action(observation, decision)
            backend.execute(decision, lambda: None)
            assert "완료: 아니스 테스트" in backend.observe().text
    finally:
        child.terminate()
        child.wait(timeout=5)


if __name__ == "__main__":
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget
    app = QApplication([])
    widget = QWidget()
    widget.setWindowTitle(sys.argv[1])
    layout = QVBoxLayout(widget)
    entry, button, output = QLineEdit(), QPushButton("확인"), QLabel("대기")
    entry.setAccessibleName("내용")
    button.clicked.connect(lambda: output.setText("완료: " + entry.text()))
    for control in (entry, button, output):
        layout.addWidget(control)
    widget.resize(400, 180)
    widget.show()
    widget.raise_()
    widget.activateWindow()
    QTimer.singleShot(30000, app.quit)
    app.exec()
