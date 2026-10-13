import io
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PIL import Image, ImageDraw

from core.computer_use import StaleObservation
from core.computer_use_backends import WindowsComputerBackend
from core.plugin import ToolCancelledError
from core.windows_automation import WindowInfo, WindowsAutomationRuntime


@pytest.mark.parametrize("window_mode", ["normal", "maximized", "minimized"])
def test_focus_restores_only_minimized_windows_and_preserves_maximized_bounds(window_mode):
    native = Mock()
    native.IsWindow.return_value = True
    native.IsIconic.return_value = window_mode == "minimized"
    native.GetForegroundWindow.return_value = 100
    state = {"mode": window_mode}
    native.ShowWindow.side_effect = lambda *_args: state.update(mode="normal")
    runtime = WindowsAutomationRuntime()
    expected = WindowInfo(100, "테스트 문서", 200, True, True, True)
    runtime.find_window = Mock(return_value=expected)
    with patch("core.windows_automation.user32", native), patch("core.windows_automation.time.sleep"):
        assert runtime.focus(100) is expected
    native.SetForegroundWindow.assert_called_once_with(100)
    if window_mode == "minimized":
        native.ShowWindow.assert_called_once_with(100, 9)
    else:
        native.ShowWindow.assert_not_called()
    assert state["mode"] == ("normal" if window_mode == "minimized" else window_mode)


def backend_fixture():
    backend = WindowsComputerBackend("테스트 문서")
    backend.window = WindowInfo(100, "테스트 문서", 200, True, True, True)
    root = Mock()
    root.rectangle.return_value = [-100, 50, 100, 150]
    root.window_text.return_value = "테스트 문서"
    root.capture_as_image.return_value = Image.new("RGB", (400, 200), "white")
    backend._root = lambda: root
    backend.automation = Mock()
    backend.automation._rectangle_value.side_effect = lambda rectangle: list(rectangle)
    identity = {"stable": True, "token": "edit"}
    wrapper = SimpleNamespace(element_info=SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=False)),
                              is_enabled=lambda: True, has_keyboard_focus=lambda: True)
    metadata = {"name": "내용", "automation_id": "edit", "control_type": "Edit",
                "rectangle": [-90, 60, 90, 140], "control_identity": identity}
    backend.automation._accessibility_candidates.return_value = [(0, wrapper, metadata)]
    backend.automation._accessibility_text_control.return_value = (wrapper, metadata)
    backend.automation._read_wrapper_value.return_value = "원본"
    backend.automation.verify_accessibility_control.return_value = metadata
    return backend, root


def test_capture_masks_password_even_if_disabled_and_after_target_limit():
    backend, root = backend_fixture()
    candidates = backend.automation._accessibility_candidates.return_value * 65
    password = SimpleNamespace(element_info=SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=True)),
                               is_enabled=lambda: False, has_keyboard_focus=lambda: False)
    candidates.append((0, password, {"name": "비밀", "rectangle": [-80, 70, -60, 90]}))
    backend.automation._accessibility_candidates.return_value = candidates
    observation = backend.observe()
    image = Image.open(io.BytesIO(observation.screenshot))
    assert image.getpixel((60, 60)) == (0, 0, 0)
    assert image.getpixel((10, 10)) == (255, 255, 255)
    assert len(observation.targets) == 60 and "비밀" not in observation.text
    assert backend.automation._accessibility_candidates.call_args.kwargs["include_disabled"]


def test_image_coordinates_scale_to_negative_monitor_position():
    backend, root = backend_fixture()
    observation = backend.observe()
    decision = {"action": "move", "x": 200, "y": 100}
    with patch("pywinauto.mouse.move") as move, patch("core.computer_use_backends._wait"):
        backend.validate_action(observation, decision)
        backend.execute(decision, lambda: None)
    move.assert_called_once_with(coords=(0, 100))
    assert WindowsComputerBackend._screen_point([-1000, 0, 0, 500], (2000, 1000), 1999, 999) == (-1, 499)
    with pytest.raises(ValueError):
        WindowsComputerBackend._screen_point([-100, 0, 100, 100], (400, 200), 400, 0)


def test_target_bounds_use_screenshot_coordinates_and_retain_native_rectangle():
    backend, root = backend_fixture()
    observation = backend.observe()
    assert observation.targets["e1"]["rectangle"] == [-90, 60, 90, 140]
    assert observation.targets["e1"]["bounds"] == [20, 20, 380, 180]
    root.rectangle.return_value = [-1000, -500, 0, 0]
    root.capture_as_image.return_value = Image.new("RGB", (1500, 750), "white")
    metadata = backend.automation._accessibility_candidates.return_value[0][2]
    metadata["rectangle"] = [-800, -400, -200, -100]
    observation = backend.observe()
    assert observation.targets["e1"]["bounds"] == [300, 150, 1200, 600]
    assert observation.targets["e1"]["rectangle"] == [-800, -400, -200, -100]
    assert observation.payload()["targets"]["e1"]["bounds"] == [300, 150, 1200, 600]


def test_pointer_accepts_caret_blink_but_rejects_changed_target_or_occlusion():
    backend, root = backend_fixture()
    observation = backend.observe()
    decision = {"action": "coordinate_click", "x": 200, "y": 100}
    image = root.capture_as_image.return_value.copy()
    ImageDraw.Draw(image).line((201, 91, 201, 110), fill="black")
    root.capture_as_image.return_value = image
    backend.validate_action(observation, decision)
    ImageDraw.Draw(image).rectangle((190, 90, 210, 110), fill="black")
    with pytest.raises(StaleObservation, match="영역"):
        backend.validate_action(observation, decision)
    root.capture_as_image.return_value = Image.new("RGB", (400, 200), "white")
    backend.automation.verify_pointer_target.side_effect = RuntimeError("가려짐")
    with pytest.raises(RuntimeError, match="가려짐"):
        backend.validate_action(observation, decision)


def test_pointer_refuses_masked_password_rectangle():
    backend, root = backend_fixture()
    password = SimpleNamespace(element_info=SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=True)))
    backend.automation._accessibility_candidates.return_value.append(
        (0, password, {"rectangle": [-80, 70, -60, 90]}))
    observation = backend.observe()
    with pytest.raises(ValueError, match="비밀"):
        backend.validate_action(observation, {"action": "coordinate_click", "x": 60, "y": 60})


@pytest.mark.parametrize("action, function", [("double_click", "double_click"), ("right_click", "click")])
def test_target_mouse_actions_revalidate_native_identity(action, function):
    backend, root = backend_fixture()
    observation = backend.observe()
    decision = {"action": action, "target": "e1"}
    with patch("pywinauto.mouse." + function) as click, patch("core.computer_use_backends._wait"):
        backend.validate_action(observation, decision)
        backend.execute(decision, lambda: None)
    assert click.call_args.kwargs["coords"] == (0, 100)
    if action == "right_click":
        assert click.call_args.kwargs["button"] == "right"
    assert backend.automation.verify_accessibility_control.call_args.kwargs["require_foreground"]


@pytest.mark.parametrize("action, function", [("double_click", "double_click"), ("right_click", "click")])
def test_target_mouse_actions_block_same_app_popup_covering_observed_control(action, function):
    backend, root = backend_fixture()
    observation = backend.observe()
    image = root.capture_as_image.return_value.copy()
    ImageDraw.Draw(image).rectangle((175, 75, 225, 125), fill="black")
    root.capture_as_image.return_value = image
    decision = {"action": action, "target": "e1"}
    with patch("pywinauto.mouse." + function) as click:
        with pytest.raises(StaleObservation, match="영역"):
            backend.validate_action(observation, decision)
            backend.execute(decision, lambda: None)
    backend.automation.verify_pointer_target.assert_called_once_with(100, 0, 100)
    click.assert_not_called()


def test_hotkeys_are_fixed_and_do_not_use_model_key_expressions():
    backend, root = backend_fixture()
    observation = backend.observe()
    with patch("pywinauto.keyboard.send_keys") as send, patch("core.computer_use_backends._wait"):
        backend.validate_action(observation, {"action": "hotkey", "key": "Ctrl+S"})
        backend.execute({"action": "hotkey", "key": "Ctrl+S"}, lambda: None)
        send.assert_called_once_with("^s", pause=0.01)
        with pytest.raises(ValueError, match="단축키"):
            backend.validate_action(observation, {"action": "hotkey", "key": "%{F4};malicious"})


def test_unicode_is_chunked_and_stops_before_next_batch_on_cancel():
    backend, root = backend_fixture()
    observation = backend.observe()
    decision = {"action": "type_text", "text": "한🙂" * 80}
    backend.validate_action(observation, decision)
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise ToolCancelledError("취소")

    with patch("core.desktop_messaging.DesktopMessagingRuntime._type_unicode") as send:
        with pytest.raises(ToolCancelledError):
            backend.execute(decision, check)
    assert [call.args[0] for call in send.call_args_list] == [decision["text"][:64], decision["text"][64:128]]


def test_drag_always_releases_button_when_cancelled():
    backend, root = backend_fixture()
    observation = backend.observe()
    decision = {"action": "drag", "x": 100, "y": 50, "to_x": 300, "to_y": 150}
    backend.validate_action(observation, decision)
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise ToolCancelledError("취소")

    with patch("pywinauto.mouse.press") as press, patch("pywinauto.mouse.move") as move, patch("pywinauto.mouse.release") as release:
        with pytest.raises(ToolCancelledError):
            backend.execute(decision, check)
    press.assert_called_once_with(coords=(-50, 75))
    move.assert_called_once_with(coords=(-40, 80))
    release.assert_called_once_with(coords=(-40, 80))


def test_only_same_process_owned_dialog_is_followed_and_observed_window_can_switch():
    backend, root = backend_fixture()
    backend._scope_root = 100
    main = WindowInfo(100, "테스트 문서", 200, True, False, False)
    dialog = WindowInfo(101, "다른 이름으로 저장", 200, True, True, True)
    foreign = WindowInfo(102, "다른 앱", 999, True, True, False)
    backend.automation.list_windows.return_value = [main, dialog, foreign]
    backend.automation.is_owned_window.side_effect = lambda handle, owner: (handle, owner) == (101, 100)
    observation = backend.observe()
    assert backend.window == dialog and set(observation.context["windows"]) == {"w100", "w101"}
    decision = {"action": "switch_window", "target": "w100"}
    backend.validate_action(observation, decision)
    backend.automation.list_windows.return_value = [dialog, foreign]
    with pytest.raises(StaleObservation):
        backend.validate_action(observation, decision)
    with pytest.raises(StaleObservation):
        backend.validate_action(observation, {"action": "switch_window", "target": "w102"})


def test_app_selection_rejects_missing_target_and_non_file_launch_arguments():
    backend, root = backend_fixture()
    backend.window_title = ""
    with pytest.raises(ValueError, match="대상"):
        backend._choose_window([])
    backend.app, backend.app_arguments = "메모장", ["--execute=anything"]
    with pytest.raises(ValueError, match="절대 파일"):
        backend._choose_window([])


def document_fixture():
    backend, root = backend_fixture()
    value = {"text": "\r", "selected": False}
    wrapper = backend.automation._accessibility_candidates.return_value[0][1]
    metadata = backend.automation._accessibility_candidates.return_value[0][2]
    metadata.update(control_type="Document", class_name="RichEditD2DPT")
    document_range = Mock()
    document_range.GetText.side_effect = lambda _maximum: value["text"]
    wrapper.iface_text = SimpleNamespace(DocumentRange=document_range)
    backend.automation._read_wrapper_value.side_effect = WindowsAutomationRuntime._read_wrapper_value

    def select_all(_keys):
        value["selected"] = True

    def type_text(text):
        previous = "" if value["selected"] else value["text"][:-1]
        value["text"] = previous + text + "\r"
        value["selected"] = False

    return backend, value, document_range, select_all, type_text


def test_textpattern_only_document_fill_preserves_exact_unicode_and_final_newline():
    backend, value, document_range, select_all, type_text = document_fixture()
    observation = backend.observe()
    assert observation.targets["e1"]["value"] == ""
    assert observation.targets["e1"]["value_length"] == 0
    text = "한글🙂" * 30 + "\n끝 공백  \n"
    decision = {"action": "fill", "target": "e1", "text": text}
    with patch("core.desktop_messaging.DesktopMessagingRuntime._hotkey", side_effect=select_all) as hotkey, \
            patch("core.desktop_messaging.DesktopMessagingRuntime._type_unicode", side_effect=type_text) as send, \
            patch("core.computer_use_backends._wait"):
        backend.validate_action(observation, decision)
        backend.execute(decision, lambda: None)
    assert value["text"] == text + "\r" and send.call_count == 2
    hotkey.assert_called_once_with(("CTRL", "A"))
    assert all(call.args == (-1,) for call in document_range.GetText.call_args_list)
    backend.automation.set_accessibility_text.assert_not_called()


def test_document_snapshot_reports_full_length_when_value_preview_is_truncated():
    backend, value, *_ = document_fixture()
    value["text"] = "한" * 1200 + "\r"
    observation = backend.observe()
    assert len(observation.targets["e1"]["value"]) == 1000
    assert observation.payload()["targets"]["e1"]["value_length"] == 1200


def test_document_fill_does_not_retry_failed_valuepattern_write_with_global_input():
    backend, value, document_range, select_all, type_text = document_fixture()
    wrapper = backend.automation._accessibility_candidates.return_value[0][1]
    wrapper.iface_value = SimpleNamespace(CurrentValue="\r")
    observation = backend.observe()
    backend.automation.set_accessibility_text.side_effect = RuntimeError("쓰기 후 오류")
    with patch("core.desktop_messaging.DesktopMessagingRuntime._type_unicode") as send:
        with pytest.raises(RuntimeError, match="쓰기 후"):
            backend.execute({"action": "fill", "target": "e1", "text": "문서"}, lambda: None)
    send.assert_not_called()


def test_document_fill_cancel_stops_before_next_unicode_chunk():
    backend, value, document_range, select_all, type_text = document_fixture()
    observation = backend.observe()
    decision = {"action": "fill", "target": "e1", "text": "한" * 150}
    backend.validate_action(observation, decision)
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise ToolCancelledError("취소")

    with patch("core.desktop_messaging.DesktopMessagingRuntime._hotkey", side_effect=select_all), \
            patch("core.desktop_messaging.DesktopMessagingRuntime._type_unicode", side_effect=type_text) as send:
        with pytest.raises(ToolCancelledError):
            backend.execute(decision, check)
    send.assert_called_once_with("한" * 64)
    assert value["text"] == "한" * 64 + "\r"


def test_unsupported_uia_invoke_method_is_not_advertised_or_dispatched():
    class Wrapper:
        element_info = SimpleNamespace(element=SimpleNamespace(CurrentIsPassword=False))
        set_focus, invoke = Mock(), Mock()

        @property
        def iface_invoke(self):
            raise RuntimeError("")

        @property
        def iface_selection_item(self):
            raise RuntimeError("")

        def is_enabled(self):
            return True

        def has_keyboard_focus(self):
            return False

    backend, root = backend_fixture()
    wrapper = Wrapper()
    metadata = dict(backend.automation._accessibility_candidates.return_value[0][2], control_type="Pane")
    backend.automation._accessibility_candidates.return_value = [(0, wrapper, metadata)]
    observation = backend.observe()
    assert "click" not in observation.targets["e1"]["actions"]
    with pytest.raises(ValueError, match="지원하지"):
        backend.validate_action(observation, {"action": "click", "target": "e1"})
    runtime = WindowsAutomationRuntime()
    runtime._accessibility_candidates = Mock(return_value=[(0, wrapper, metadata)])
    with pytest.raises(RuntimeError, match="InvokePattern"):
        runtime.activate_accessibility_control(100, allow_type_only=True)
    wrapper.set_focus.assert_not_called()
    wrapper.invoke.assert_not_called()


def test_selection_pattern_is_used_without_requiring_keyboard_focus():
    selection = Mock()
    wrapper = SimpleNamespace(iface_invoke=None, iface_selection_item=selection,
                              set_focus=Mock(side_effect=RuntimeError("키보드 포커스 미지원")))
    runtime = WindowsAutomationRuntime()
    runtime._accessibility_candidates = Mock(return_value=[(0, wrapper, {
        "name": "새 탭", "control_identity": {"stable": True, "token": "tab"}})])
    with patch("core.windows_automation.time.sleep"):
        result = runtime.activate_accessibility_control(100, allow_type_only=True)
    assert result["activation"] == "select"
    selection.Select.assert_called_once_with()
    wrapper.set_focus.assert_not_called()


def test_failed_real_invoke_never_retries_an_alternate_pattern():
    invoke, selection = Mock(), Mock()
    invoke.Invoke.side_effect = RuntimeError("실행 후 오류")
    wrapper = SimpleNamespace(iface_invoke=invoke, iface_selection_item=selection, set_focus=Mock())
    runtime = WindowsAutomationRuntime()
    runtime._accessibility_candidates = Mock(return_value=[(0, wrapper, {
        "name": "버튼", "control_identity": {"stable": True, "token": "button"}})])
    with pytest.raises(RuntimeError, match="실행 후 오류"):
        runtime.activate_accessibility_control(100, allow_type_only=True)
    invoke.Invoke.assert_called_once_with()
    selection.Select.assert_not_called()


def test_editor_actions_are_only_advertised_after_readback_and_focus_support():
    backend, root = backend_fixture()
    wrapper = backend.automation._accessibility_candidates.return_value[0][1]
    wrapper.element_info.element.CurrentIsKeyboardFocusable = True
    observation = backend.observe()
    assert "fill" in observation.targets["e1"]["actions"]
    assert "press" in observation.targets["e1"]["actions"]
    assert "click" not in observation.targets["e1"]["actions"]
    backend.automation._read_wrapper_value.side_effect = RuntimeError("읽기 미지원")
    observation = backend.observe()
    assert "fill" not in observation.targets["e1"]["actions"]


def test_backspace_and_save_in_nonempty_editors_require_confirmation():
    from plugins.computer_use import ComputerUsePlugin
    identity = {"stable": True, "token": "editor"}
    observation = SimpleNamespace(context={"title": "메모장"}, host_state={"focused_identity": identity},
        targets={"editor": {"name": "내용", "control_type": "Document", "value": "기존 초안", "control_identity": identity}})
    assert ComputerUsePlugin._confirmation_reason(observation, {"action": "press", "target": "editor", "key": "Backspace"})
    assert ComputerUsePlugin._confirmation_reason(observation, {"action": "hotkey", "key": "Ctrl+S"})
    observation.targets["editor"]["value"] = ""
    assert not ComputerUsePlugin._confirmation_reason(observation, {"action": "press", "target": "editor", "key": "Backspace"})


def test_ctrl_n_can_follow_a_new_same_process_window_but_not_a_preexisting_user_window():
    backend, root = backend_fixture()
    backend._scope_root = 100
    other = WindowInfo(102, "다른 문서", 200, True, True, False)
    backend.automation.list_windows.return_value = [backend.window, other]
    backend.automation.is_owned_window.return_value = False
    observation = backend.observe()
    created = WindowInfo(103, "새 문서", 200, True, True, True)

    def create_window(*_args, **_kwargs):
        backend.automation.list_windows.return_value = [
            WindowInfo(100, "테스트 문서", 200, True, True, False), other, created]

    with patch("pywinauto.keyboard.send_keys", side_effect=create_window), patch("core.computer_use_backends._wait"):
        backend.execute({"action": "hotkey", "key": "Ctrl+N"}, lambda: None)
    backend.observe()
    assert backend.window == created and backend._scope_root == 103
    assert backend._pending_new_window is None

    backend.automation.list_windows.return_value = [
        WindowInfo(103, "새 문서", 200, True, True, False),
        WindowInfo(102, "다른 문서", 200, True, True, True)]
    backend._follow_owned_dialog()
    assert backend.window == created


def test_ctrl_n_follow_scope_expires_and_never_enters_another_process():
    backend, root = backend_fixture()
    backend._scope_root = 100
    original = backend.window
    backend.automation.is_owned_window.return_value = False
    backend._pending_new_window = ({100}, 0)
    backend.automation.list_windows.return_value = [WindowInfo(101, "새 문서", 200, True, True, True)]
    backend._follow_owned_dialog()
    assert backend.window == original and backend._pending_new_window is None
    backend._pending_new_window = ({100}, float("inf"))
    backend.automation.list_windows.return_value = [WindowInfo(101, "다른 앱", 999, True, True, True)]
    backend._follow_owned_dialog()
    assert backend.window == original
