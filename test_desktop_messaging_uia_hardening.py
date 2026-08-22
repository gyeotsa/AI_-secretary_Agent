import json

import pytest

from core.desktop_messaging import DesktopMessagingRuntime
from core.tool_result import ToolRunStatus
from core.windows_automation import WindowInfo, WindowsAutomationRuntime
from plugins.desktop_messaging import DesktopMessagingPlugin


class VerifiedMessagingAutomation:
    def __init__(self, *, expose_new_bubble=True, swap_composer=False,
                 steal_focus_on_read=False, rerender_old_bubble=False):
        self.main = WindowInfo(10, "카카오톡", 99, True, True, True)
        self.chat = WindowInfo(11, "형택 - 카카오톡", 99, True, True, False)
        self.foreground_handle = self.main.handle
        self.composer_value = ""
        self.composer_identity = {
            "window_handle": 11,
            "runtime_id": [42, 7],
            "native_handle": 701,
            "process_id": 99,
            "automation_id": "chat-input",
            "control_type": "Edit",
            "class_name": "RichEdit",
            "name": "메시지 입력",
            "rectangle": [200, 700, 900, 760],
            "token": "composer-42-7",
        }
        self.expose_new_bubble = expose_new_bubble
        self.swap_composer = swap_composer
        self.steal_focus_on_read = steal_focus_on_read
        self.rerender_old_bubble = rerender_old_bubble
        self.enter_count = 0
        self.bubbles = [{
            "identity_token": "old-message",
            "control_identity": {"token": "old-message"},
            "value": "테스트",
            "direction": "outgoing",
            "outgoing": True,
            "x_ratio": 0.80,
        }]

    def list_windows(self):
        return [
            WindowInfo(item.handle, item.title, item.process_id, item.visible, item.enabled,
                       item.handle == self.foreground_handle)
            for item in (self.main, self.chat)
        ]

    def focus(self, handle):
        self.foreground_handle = int(handle)
        source = self.chat if int(handle) == self.chat.handle else self.main
        return WindowInfo(source.handle, source.title, source.process_id,
                          source.visible, source.enabled, True)

    def close_window(self, _handle):
        raise AssertionError("금지된 창은 테스트에 존재하지 않습니다")

    def activate_accessibility_control(self, _handle, **kwargs):
        names = kwargs.get("names") or []
        if names == ["형택"]:
            return {"control_type": "ListItem", "activation": "invoke"}
        if "친구" in names:
            return {"control_type": "TabItem", "activation": "invoke"}
        return {"control_type": "Edit", "activation": "focus"}

    def set_accessibility_text(self, _handle, value, **_kwargs):
        self.composer_value = str(value)
        return {
            "name": "메시지 입력", "automation_id": "chat-input",
            "control_type": "Edit", "value_verified": True,
            "control_identity": dict(self.composer_identity),
        }

    def read_accessibility_text(self, _handle, **kwargs):
        if self.steal_focus_on_read and kwargs.get("require_keyboard_focus"):
            self.foreground_handle = self.main.handle
        identity = dict(self.composer_identity)
        if self.swap_composer:
            identity["runtime_id"] = [42, 8]
            identity["token"] = "composer-42-8"
        return {
            "name": "메시지 입력", "automation_id": "chat-input",
            "control_type": "Edit", "value": self.composer_value,
            "keyboard_focus_verified": bool(kwargs.get("require_keyboard_focus")),
            "control_identity": identity,
        }

    def accessibility_text_occurrences(self, _handle, value, **_kwargs):
        return [item for item in self.bubbles if item["value"] == value]

    def enter(self):
        self.enter_count += 1
        self.composer_value = ""
        if self.rerender_old_bubble:
            self.bubbles[0]["identity_token"] = f"rerendered-{self.enter_count}"
            self.bubbles[0]["control_identity"] = {
                "token": f"rerendered-{self.enter_count}",
            }
        elif self.expose_new_bubble:
            self.bubbles.append({
                "identity_token": f"new-message-{self.enter_count}",
                "control_identity": {"token": f"new-message-{self.enter_count}"},
                "value": "테스트", "direction": "outgoing", "outgoing": True,
                "x_ratio": 0.82,
            })


def _runtime(monkeypatch, automation):
    runtime = DesktopMessagingRuntime()
    runtime.automation = automation
    monkeypatch.setattr("core.desktop_messaging.os.name", "nt")
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime, "_hotkey", lambda _keys: None)
    monkeypatch.setattr(runtime, "_type_unicode", lambda _text: None)
    monkeypatch.setattr(runtime, "_press", lambda key: automation.enter() if key == "ENTER" else None)
    return runtime


def test_main_window_matching_does_not_confuse_recipient_chat():
    main = WindowInfo(10, "카카오톡", 99, True, True, False)
    chat = WindowInfo(11, "형택 - 카카오톡", 99, True, True, True)

    assert DesktopMessagingRuntime._matching_windows([main, chat], ["카카오톡"]) == [main]


def test_send_requires_same_focused_composer_and_new_exact_outgoing_bubble(monkeypatch):
    automation = VerifiedMessagingAutomation()
    runtime = _runtime(monkeypatch, automation)

    result = runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.enter_count == 1
    assert result["send_accepted_verified"] is True
    assert result["outgoing_message_verified"] is True
    assert result["send_verification"] == "new_exact_outgoing_message_bubble"
    assert result["message_input_focus"]["control_identity"]["token"] == "composer-42-7"


def test_send_fails_closed_if_composer_identity_changes_before_enter(monkeypatch):
    automation = VerifiedMessagingAutomation(swap_composer=True)
    runtime = _runtime(monkeypatch, automation)

    with pytest.raises(RuntimeError, match="다른 UIA 요소"):
        runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.enter_count == 0


def test_send_fails_closed_if_foreground_changes_during_composer_read(monkeypatch):
    automation = VerifiedMessagingAutomation(steal_focus_on_read=True)
    runtime = _runtime(monkeypatch, automation)

    with pytest.raises(RuntimeError, match="포커스 검증"):
        runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.enter_count == 0


def test_send_fails_closed_without_stable_composer_identity(monkeypatch):
    automation = VerifiedMessagingAutomation()

    def set_without_identity(_handle, value, **_kwargs):
        automation.composer_value = str(value)
        return {
            "name": "메시지 입력", "automation_id": "chat-input",
            "control_type": "Edit", "value_verified": True,
        }

    automation.set_accessibility_text = set_without_identity
    runtime = _runtime(monkeypatch, automation)

    with pytest.raises(RuntimeError, match="안정적인 UIA"):
        runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.enter_count == 0


def test_send_returns_unverified_when_new_outgoing_bubble_is_not_observable(monkeypatch):
    automation = VerifiedMessagingAutomation(expose_new_bubble=False)
    runtime = _runtime(monkeypatch, automation)

    result = runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.enter_count == 1
    assert result["send_accepted_verified"] is False
    assert result["outgoing_message_verified"] is False
    assert result["verification_status"] == "unverified"
    assert "말풍선" in result["unverified_reason"]


def test_right_aligned_text_without_outgoing_semantics_is_not_delivery_evidence(monkeypatch):
    automation = VerifiedMessagingAutomation(expose_new_bubble=False)
    automation.bubbles.append({
        "identity_token": "right-side-non-message",
        "control_identity": {"token": "right-side-non-message"},
        "value": "테스트",
        "x_ratio": 0.92,
        "automation_id": "timestamp",
        "class_name": "Text",
    })
    runtime = _runtime(monkeypatch, automation)

    result = runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.enter_count == 1
    assert result["outgoing_message_verified"] is False
    assert result["verification_status"] == "unverified"


def test_rerendered_old_bubble_with_new_runtime_id_is_not_new_send_evidence(monkeypatch):
    automation = VerifiedMessagingAutomation(
        expose_new_bubble=False, rerender_old_bubble=True,
    )
    runtime = _runtime(monkeypatch, automation)

    result = runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.enter_count == 1
    assert len(automation.bubbles) == 1
    assert result["outgoing_message_verified"] is False
    assert result["verification_status"] == "unverified"


def test_semantic_outgoing_marker_must_also_be_on_expected_side():
    config = {
        "outgoing_message_direction_keywords": ["outgoing"],
        "outgoing_message_min_x_ratio": 0.55,
    }

    assert DesktopMessagingRuntime._is_outgoing_occurrence({
        "direction": "outgoing", "x_ratio": 0.8,
    }, config) is True
    assert DesktopMessagingRuntime._is_outgoing_occurrence({
        "direction": "outgoing", "x_ratio": 0.2,
    }, config) is False


def test_plugin_never_claims_completion_for_unverified_dispatch(monkeypatch):
    plugin = DesktopMessagingPlugin()
    raw = {
        "provider": "kakaotalk", "recipient": "형택", "window_title": "형택 - 카카오톡",
        "window_handle": 11, "process_id": 99,
        "send_accepted_verified": False, "outgoing_message_verified": False,
        "send_verification": "unverified_no_new_exact_outgoing_message_bubble",
        "unverified_reason": "새 말풍선을 찾지 못했습니다.",
        "delivery_receipt_verified": False,
    }
    monkeypatch.setattr(plugin.runtime, "send", lambda *_args, **_kwargs: dict(raw))

    result = plugin.execute_tool("desktop_send_message", {
        "provider": "kakaotalk", "recipient": "형택", "message": "테스트",
    })

    assert result.status == ToolRunStatus.UNVERIFIED
    assert "완료로 처리하지 않았습니다" in result.evidence[0].summary
    presentation = plugin.present_result("desktop_send_message", result.raw_output)
    assert "완료로 처리하지 않았습니다" in presentation
    assert "전송했습니다" not in presentation


def test_windows_uia_reader_rejects_different_runtime_identity(monkeypatch):
    runtime = WindowsAutomationRuntime()

    class Wrapper:
        value = "테스트"

        def get_value(self):
            return self.value

        def has_keyboard_focus(self):
            return True

    wrapper = Wrapper()
    first = {"token": "composer-a", "runtime_id": [1], "window_handle": 11}
    second = {"token": "composer-b", "runtime_id": [2], "window_handle": 11}
    monkeypatch.setattr(runtime, "_accessibility_candidates", lambda *_args, **_kwargs: [(
        100, wrapper, {
            "name": "메시지 입력", "automation_id": "chat-input", "control_type": "Edit",
            "control_identity": second,
        },
    )])

    with pytest.raises(LookupError, match="동일한 UIA 요소"):
        runtime.read_accessibility_text(11, expected_identity=first)


def test_windows_uia_runtime_identity_token_ignores_mutable_bounds():
    runtime = WindowsAutomationRuntime()

    class ElementInfo:
        runtime_id = [9, 8, 7]
        handle = 0
        process_id = 99
        class_name = "RichEdit"
        rectangle = [0, 0, 100, 40]

    class Wrapper:
        element_info = ElementInfo()

    first = runtime._control_identity(11, Wrapper(), {
        "name": "메시지 입력", "automation_id": "chat-input",
        "control_type": "Edit", "rectangle": [0, 0, 100, 40],
    })
    second = runtime._control_identity(11, Wrapper(), {
        "name": "메시지 입력", "automation_id": "chat-input",
        "control_type": "Edit", "rectangle": [50, 50, 550, 90],
    })

    assert first["stable"] is True
    assert first["identity_basis"] == "runtime_id"
    assert first["token"] == second["token"]
