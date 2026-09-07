import json

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

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
        self.search_value = ""
        self.search_identity = {
            "window_handle": 10,
            "runtime_id": [30, 7],
            "native_handle": 607,
            "process_id": 99,
            "automation_id": "contact-search",
            "control_type": "Edit",
            "class_name": "Edit",
            "name": "검색",
            "rectangle": [100, 100, 500, 140],
            "token": "search-30-7",
        }
        self.expose_new_bubble = expose_new_bubble
        self.swap_composer = swap_composer
        self.steal_focus_on_read = steal_focus_on_read
        self.rerender_old_bubble = rerender_old_bubble
        self.enter_count = 0
        self.contact_search_count = 0
        self.text_set_calls = []
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

    def activate_accessibility_control(self, handle, **kwargs):
        names = kwargs.get("names") or []
        if names == ["형택"]:
            self.contact_search_count += 1
            return {"control_type": "ListItem", "activation": "invoke"}
        if "친구" in names:
            return {"control_type": "TabItem", "activation": "invoke"}
        if int(handle) == self.main.handle:
            return {
                "name": "검색", "automation_id": "contact-search",
                "control_type": "Edit", "activation": "focus",
                "keyboard_focus_verified": True,
                "control_identity": dict(self.search_identity),
            }
        return {"control_type": "Edit", "activation": "focus"}

    def set_accessibility_text(self, handle, value, **_kwargs):
        self.text_set_calls.append((int(handle), dict(_kwargs)))
        if int(handle) == self.main.handle:
            self.search_value = str(value)
            identity = self.search_identity
        else:
            self.composer_value = str(value)
            identity = self.composer_identity
        return {
            "name": "메시지 입력", "automation_id": "chat-input",
            "control_type": "Edit", "value": str(value), "value_verified": True,
            "control_identity": dict(identity),
        }

    def read_accessibility_text(self, handle, **kwargs):
        if self.steal_focus_on_read and kwargs.get("require_keyboard_focus"):
            self.foreground_handle = self.main.handle
        if int(handle) == self.main.handle:
            return {
                "name": "검색", "automation_id": "contact-search",
                "control_type": "Edit", "value": self.search_value,
                "keyboard_focus_verified": bool(kwargs.get("require_keyboard_focus")),
                "control_identity": dict(self.search_identity),
            }
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

    def verify_accessibility_control(self, _handle, **kwargs):
        return {
            "name": str((kwargs.get("names") or [""])[0]),
            "control_type": "ListItem", "keyboard_focus_verified": True,
            "control_identity": dict(kwargs["expected_identity"]),
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
    def press(key):
        if key == "ENTER":
            automation.enter()
        elif key == "BACKSPACE":
            automation.composer_value = ""

    monkeypatch.setattr(runtime, "_press", press)
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
    assert automation.contact_search_count == 0


def test_message_input_selector_supports_current_kakao_document_richedit():
    selector = DesktopMessagingRuntime._message_input_selector({})

    assert "Document" in selector["control_types"]
    assert "RichEdit Control" in selector["names"]
    assert "1006" in selector["automation_id_keywords"]


def test_native_richedit_trailing_carriage_return_is_not_visible_message_content():
    state = {"value": "한글 테스트\r", "class_name": "RICHEDIT50W"}

    assert DesktopMessagingRuntime._canonical_composer_value(state) == "한글 테스트"
    assert DesktopMessagingRuntime._canonical_composer_value({
        "value": "\r", "class_name": "RICHEDIT50W",
    }) == ""


def test_native_richedit_preserves_intentional_trailing_newline_and_spaces():
    assert DesktopMessagingRuntime._canonical_composer_value({
        "value": "한글 😀  \r\r", "class_name": "RICHEDIT50W",
    }) == "한글 😀  \n"
    assert WindowsAutomationRuntime._canonical_edit_value("한글 😀  \n") == "한글 😀  \n"


@pytest.mark.parametrize("native", [False, True])
def test_message_composer_never_overwrites_an_unrelated_user_draft(monkeypatch, native):
    automation = VerifiedMessagingAutomation()
    draft = "아직 보내지 않은 사용자 초안 😀  "
    automation.composer_value = draft
    if native:
        automation.activate_accessibility_control = lambda *_args, **_kwargs: {
            "class_name": "RICHEDIT50W", "control_type": "Document",
            "control_identity": dict(automation.composer_identity),
        }
    runtime = _runtime(monkeypatch, automation)
    monkeypatch.setattr(runtime, "_select_native_edit_all", lambda *_args: pytest.fail("must not select"))
    monkeypatch.setattr(runtime, "_paste_unicode_preserving_clipboard", lambda *_args, **_kw: pytest.fail("must not paste"))

    with pytest.raises(RuntimeError, match="다른 초안"):
        runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert automation.composer_value == draft
    assert automation.enter_count == 0


def test_native_edit_uses_bounded_targeted_message_instead_of_global_backspace(monkeypatch):
    calls = []

    class NativeApi:
        def IsWindow(self, handle):
            return handle == 701

        def GetWindowThreadProcessId(self, handle, process):
            ctypes.cast(process, ctypes.POINTER(wintypes.DWORD)).contents.value = 99
            return 1

        def IsChild(self, parent, handle):
            return parent == 11 and handle == 701

        def GetClassNameW(self, handle, buffer, length):
            buffer.value = "RICHEDIT50W"
            return len(buffer.value)

        def SendMessageTimeoutW(self, handle, message, wparam, lparam, flags, timeout, result):
            calls.append((handle, message, wparam, lparam, flags, timeout))
            return 1

    monkeypatch.setattr("core.desktop_messaging._USER32", NativeApi())
    identity = {"native_handle": 701, "process_id": 99, "window_handle": 11, "class_name": "RICHEDIT50W"}
    DesktopMessagingRuntime._select_native_edit_all(identity)
    DesktopMessagingRuntime._native_edit_message(identity, 0x0302)
    assert calls == [(701, 0xB1, 0, -1, 3, 2000), (701, 0x302, 0, 0, 3, 2000)]


def test_native_edit_timeout_is_not_reported_as_input_success(monkeypatch):
    class NativeApi:
        def IsWindow(self, handle):
            return True

        def GetWindowThreadProcessId(self, handle, process):
            ctypes.cast(process, ctypes.POINTER(wintypes.DWORD)).contents.value = 99
            return 1

        def IsChild(self, parent, handle):
            return True

        def GetClassNameW(self, handle, buffer, length):
            buffer.value = "RICHEDIT50W"
            return len(buffer.value)

        def SendMessageTimeoutW(self, *_args):
            return 0

    monkeypatch.setattr("core.desktop_messaging._USER32", NativeApi())
    with pytest.raises(RuntimeError, match="시간 내"):
        DesktopMessagingRuntime._native_edit_message({
            "native_handle": 701, "process_id": 99, "window_handle": 11, "class_name": "RICHEDIT50W",
        }, 0x0302)


@pytest.mark.parametrize("field,replacement", [("native_handle", 999), ("process_id", 88),
                                              ("window_handle", 22), ("class_name", "OtherEdit")])
def test_same_runtime_id_cannot_hide_changed_native_identity(monkeypatch, field, replacement):
    automation = VerifiedMessagingAutomation()
    runtime = _runtime(monkeypatch, automation)
    expected = dict(automation.composer_identity)
    automation.composer_identity[field] = replacement
    with pytest.raises(RuntimeError, match=field):
        runtime._read_same_message_input(automation.chat, {}, expected)


def test_native_clipboard_prepare_failure_preserves_matching_unsent_draft(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.composer_value = "테스트"
    automation.activate_accessibility_control = lambda *_args, **_kwargs: {
        "class_name": "RICHEDIT50W", "control_type": "Document",
        "control_identity": dict(automation.composer_identity),
    }
    runtime = _runtime(monkeypatch, automation)

    def failed_paste(*_args, **_kwargs):
        raise RuntimeError("clipboard snapshot failed")

    monkeypatch.setattr(runtime, "_paste_unicode_preserving_clipboard", failed_paste)
    monkeypatch.setattr(runtime, "_select_native_edit_all", lambda *_args: pytest.fail("must not select before preparation"))
    with pytest.raises(RuntimeError, match="snapshot failed"):
        runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)
    assert automation.composer_value == "테스트"
    assert automation.enter_count == 0


def test_native_new_user_typing_before_paste_is_preserved(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.activate_accessibility_control = lambda *_args, **_kwargs: {
        "class_name": "RICHEDIT50W", "control_type": "Document",
        "control_identity": dict(automation.composer_identity),
    }
    runtime = _runtime(monkeypatch, automation)

    def delayed_paste(_value, *, identity, verify_target):
        automation.composer_value = "사용자가 방금 타이핑함"
        verify_target()
        pytest.fail("must not paste after draft mutation")

    monkeypatch.setattr(runtime, "_paste_unicode_preserving_clipboard", delayed_paste)
    with pytest.raises(RuntimeError, match="초안이 변경"):
        runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)
    assert automation.composer_value == "사용자가 방금 타이핑함"
    assert automation.enter_count == 0


def test_native_richedit_uses_verified_ole_paste_before_enter(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.composer_identity.update({
        "automation_id": "1006",
        "control_type": "Document",
        "class_name": "RICHEDIT50W",
        "name": "RichEdit Control",
    })

    def activate_composer(_handle, **_kwargs):
        return {
            "name": "RichEdit Control", "automation_id": "1006",
            "control_type": "Document", "class_name": "RICHEDIT50W",
            "activation": "focus",
            "control_identity": dict(automation.composer_identity),
        }

    def read_composer(_handle, **kwargs):
        return {
            "name": "RichEdit Control", "automation_id": "1006",
            "control_type": "Document", "class_name": "RICHEDIT50W",
            "value": automation.composer_value,
            "keyboard_focus_verified": bool(kwargs.get("require_keyboard_focus")),
            "control_identity": dict(automation.composer_identity),
        }

    automation.activate_accessibility_control = activate_composer
    automation.read_accessibility_text = read_composer
    runtime = _runtime(monkeypatch, automation)
    pasted_inputs = []
    native_selections = []

    def paste_unicode(value, *, identity, verify_target):
        verify_target()
        assert identity["native_handle"] == 701
        pasted_inputs.append(value)
        automation.composer_value = str(value) + "\r"

    monkeypatch.setattr(runtime, "_paste_unicode_preserving_clipboard", paste_unicode)
    monkeypatch.setattr(
        runtime, "_select_native_edit_all",
        lambda identity: native_selections.append(identity["native_handle"]),
    )

    result = runtime.send("카카오톡", "형택", "테스트", send_verify_timeout=0.2)

    assert pasted_inputs == ["테스트"]
    assert native_selections == []  # An empty composer needs no selection or deletion.
    assert automation.enter_count == 1
    assert result["send_accepted_verified"] is True
    assert result["message_input_focus"]["text_method"] == "verified_ole_paste"


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


def test_contact_search_uses_bound_uia_edit_without_global_typing(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.foreground_handle = automation.main.handle
    runtime = _runtime(monkeypatch, automation)
    search = automation.activate_accessibility_control(
        automation.main.handle, control_types=["Edit"],
    )
    monkeypatch.setattr(runtime, "_hotkey", lambda _keys: pytest.fail("global hotkey forbidden"))
    monkeypatch.setattr(runtime, "_type_unicode", lambda _text: pytest.fail("global typing forbidden"))
    monkeypatch.setattr(runtime, "_press", lambda _key: pytest.fail("global key forbidden"))

    result = runtime._activate_recipient_result(
        automation.main, "형택", {}, search,
    )

    assert automation.search_value == "형택"
    search_calls = [item for item in automation.text_set_calls if item[0] == automation.main.handle]
    assert len(search_calls) == 1
    assert search_calls[0][1]["require_keyboard_focus"] is True
    assert search_calls[0][1]["require_foreground"] is True
    assert search_calls[0][1]["expected_identity"]["token"] == "search-30-7"
    assert result["activation"] == "invoke"
    assert result["matched_recipient"] == "형택"


def test_contact_search_focus_theft_aborts_before_result_or_global_input(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.foreground_handle = automation.main.handle
    runtime = _runtime(monkeypatch, automation)
    search = automation.activate_accessibility_control(
        automation.main.handle, control_types=["Edit"],
    )
    original_read = automation.read_accessibility_text

    def steal_after_search_read(handle, **kwargs):
        state = original_read(handle, **kwargs)
        if int(handle) == automation.main.handle:
            automation.foreground_handle = 999
        return state

    automation.read_accessibility_text = steal_after_search_read
    monkeypatch.setattr(
        runtime, "_press", lambda _key: pytest.fail("must not dispatch after focus theft"),
    )
    monkeypatch.setattr(
        runtime, "_hotkey", lambda _keys: pytest.fail("must not dispatch after focus theft"),
    )
    monkeypatch.setattr(
        runtime, "_type_unicode", lambda _text: pytest.fail("must not dispatch after focus theft"),
    )

    with pytest.raises(RuntimeError, match="포커스 검증"):
        runtime._activate_recipient_result(
            automation.main, "형택", {}, search,
        )

    assert automation.contact_search_count == 0


def test_missing_exact_recipient_never_presses_enter_on_first_result(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.foreground_handle = automation.main.handle
    runtime = _runtime(monkeypatch, automation)
    search = automation.activate_accessibility_control(
        automation.main.handle, control_types=["Edit"],
    )
    original_activate = automation.activate_accessibility_control

    def no_exact_result(handle, **kwargs):
        if kwargs.get("names") == ["형택"]:
            raise LookupError("no exact result")
        return original_activate(handle, **kwargs)

    automation.activate_accessibility_control = no_exact_result
    monkeypatch.setattr(runtime, "_press", lambda _key: pytest.fail("blind Enter forbidden"))

    with pytest.raises(RuntimeError, match="전송을 중단"):
        runtime._activate_recipient_result(
            automation.main, "형택", {}, search,
        )


def test_noninvoke_recipient_result_requires_same_focused_uia_identity(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.foreground_handle = automation.main.handle
    runtime = _runtime(monkeypatch, automation)
    search = automation.activate_accessibility_control(
        automation.main.handle, control_types=["Edit"],
    )
    result_identity = {
        "window_handle": automation.main.handle,
        "runtime_id": [55, 1], "process_id": 99,
        "control_type": "ListItem", "token": "recipient-result-55-1",
    }
    original_activate = automation.activate_accessibility_control

    def select_only(handle, **kwargs):
        if kwargs.get("names") == ["형택"]:
            return {
                "name": "형택", "control_type": "ListItem",
                "activation": "select", "control_identity": result_identity,
            }
        return original_activate(handle, **kwargs)

    def stolen_before_verify(_handle, **_kwargs):
        automation.foreground_handle = 999
        raise RuntimeError("검색 결과가 포커스를 잃었습니다")

    automation.activate_accessibility_control = select_only
    automation.verify_accessibility_control = stolen_before_verify
    monkeypatch.setattr(runtime, "_press", lambda _key: pytest.fail("Enter forbidden"))

    with pytest.raises(RuntimeError, match="포커스"):
        runtime._activate_recipient_result(
            automation.main, "형택", {}, search,
        )


def test_verified_global_key_helper_never_dispatches_after_failed_proof(monkeypatch):
    runtime = DesktopMessagingRuntime()
    dispatched = []
    monkeypatch.setattr(runtime, "_press", dispatched.append)

    def reject():
        raise RuntimeError("foreground stolen")

    with pytest.raises(RuntimeError, match="stolen"):
        runtime._press_after_verification("ENTER", reject)

    assert dispatched == []


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


def test_windows_uia_writer_rechecks_foreground_after_focus_before_write(monkeypatch):
    runtime = WindowsAutomationRuntime()
    writes = []
    checks = []

    class Wrapper:
        def set_focus(self):
            pass

        def has_keyboard_focus(self):
            return True

        def set_edit_text(self, value):
            writes.append(value)

        def get_value(self):
            return ""

    identity = {
        "window_handle": 11, "runtime_id": [1, 9],
        "process_id": 99, "token": "search-1-9",
    }
    metadata = {
        "name": "검색", "automation_id": "search", "control_type": "Edit",
        "control_identity": identity,
    }
    monkeypatch.setattr(
        runtime, "_accessibility_candidates",
        lambda *_args, **_kwargs: [(100, Wrapper(), metadata)],
    )

    def foreground_check(_handle):
        checks.append(True)
        if len(checks) == 3:
            raise RuntimeError("foreground stolen before write")
        return WindowInfo(11, "카카오톡", 99, True, True, True)

    monkeypatch.setattr(runtime, "verify_foreground", foreground_check)

    with pytest.raises(RuntimeError, match="stolen before write"):
        runtime.set_accessibility_text(
            11, "형택", expected_identity=identity,
            require_keyboard_focus=True, require_foreground=True,
        )

    assert writes == []


def test_windows_uia_activation_requeries_same_identity_after_focus(monkeypatch):
    runtime = WindowsAutomationRuntime()

    class Wrapper:
        def set_focus(self):
            pass

        def has_keyboard_focus(self):
            return True

    first = {
        "window_handle": 11, "runtime_id": [1], "process_id": 99,
        "token": "search-a",
    }
    replacement = {
        "window_handle": 11, "runtime_id": [2], "process_id": 99,
        "token": "search-b",
    }
    calls = []

    def candidates(*_args, **_kwargs):
        identity = first if not calls else replacement
        calls.append(identity["token"])
        return [(100, Wrapper(), {
            "name": "검색", "automation_id": "search", "control_type": "Edit",
            "control_identity": identity,
        })]

    monkeypatch.setattr(runtime, "_accessibility_candidates", candidates)
    monkeypatch.setattr(runtime, "verify_foreground", lambda _handle: None)
    monkeypatch.setattr("core.windows_automation.time.sleep", lambda _seconds: None)

    with pytest.raises(LookupError, match="동일한 UIA 요소"):
        runtime.activate_accessibility_control(
            11, names=["검색"], control_types=["Edit"], invoke=False,
            require_keyboard_focus=True, require_foreground=True,
        )

    assert calls == ["search-a", "search-b"]


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


@pytest.mark.parametrize("requested,observed,expected_count", [
    ("테스트", "테스트", 1),
    ("  테스트  \n", "테스트", 0),
    ("테스트", "  테스트  \n", 0),
    ("테스트\n", "테스트", 0),
    ("테스트", "테스트\n", 0),
    ("앞\n뒤 😀  ", "앞\r\n뒤 😀  ", 1),
    ("앞\n뒤 😀  ", "앞\r뒤 😀  ", 1),
    ("앞  뒤", "앞 뒤", 0),
    ("\n  ", "\n  ", 0),
])
def test_outgoing_uia_body_evidence_preserves_whitespace(
    monkeypatch, requested, observed, expected_count,
):
    runtime = WindowsAutomationRuntime()

    class Wrapper:
        def __init__(self, control_type, text):
            self.element_info = SimpleNamespace(
                control_type=control_type, name=text, automation_id="message",
                class_name="Bubble", process_id=99, rectangle=[0, 0, 400, 300],
                runtime_id=[1, 2], handle=0,
            )

        def descendants(self):
            return [bubble]

        def is_visible(self):
            return True

        def parent(self):
            return None

    root = Wrapper("Window", "형택")
    bubble = Wrapper("Text", observed)
    monkeypatch.setattr(runtime, "_accessibility_root", lambda _handle: root)
    monkeypatch.setattr(runtime, "_wrapper_text_values", lambda _wrapper, metadata: [metadata["name"]])

    result = runtime.accessibility_text_occurrences(11, requested)
    assert len(result) == expected_count
    if result:
        assert result[0]["value"] == requested
