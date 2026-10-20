from types import SimpleNamespace

import pytest

from core.desktop_messaging import DesktopMessagingRuntime
from core.windows_automation import WindowInfo, WindowsAutomationRuntime


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
            return {
                "name": "형택", "control_type": "ListItem", "activation": "invoke",
                "control_identity": {"runtime_id": [55, 1], "token": "recipient-55-1"},
            }
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
    monkeypatch.setattr(runtime, "_verify_provider_process", lambda _pid, _config: None)
    monkeypatch.setattr(runtime, "_capture_chat_observation", lambda _chat: None)
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


def test_recipient_title_preserves_internal_spaces_and_only_removes_exact_provider_suffix():
    normal = DesktopMessagingRuntime._normal_title
    assert normal("김 철수 - KakaoTalk") != normal("김철수 - KakaoTalk")
    assert normal("형택 - 카카오톡 추가 내용") != normal("형택")
    assert normal("형택 - 카카오톡") == "형택"


def test_provider_title_requires_real_kakao_executable(monkeypatch):
    config = {"executable_names": ["KakaoTalk.exe"]}
    monkeypatch.setattr("psutil.Process", lambda _pid: SimpleNamespace(exe=lambda: r"C:\Apps\Notepad.exe"))
    with pytest.raises(RuntimeError, match="프로세스가 일치하지"):
        DesktopMessagingRuntime._verify_provider_process(99, config)
    monkeypatch.setattr("psutil.Process", lambda _pid: SimpleNamespace(exe=lambda: r"C:\Apps\KakaoTalk.exe"))
    DesktopMessagingRuntime._verify_provider_process(99, config)


def test_duplicate_exact_recipient_results_are_never_invoked(monkeypatch):
    automation = VerifiedMessagingAutomation()
    runtime = _runtime(monkeypatch, automation)
    original_activate = automation.activate_accessibility_control
    search = original_activate(automation.main.handle, control_types=["Edit"])

    def ambiguous(handle, **kwargs):
        if kwargs.get("names") == ["형택"]:
            assert kwargs["require_unique"] is True
            assert kwargs["invoke"] is False
            raise LookupError("same-name contacts")
        return original_activate(handle, **kwargs)

    automation.activate_accessibility_control = ambiguous
    with pytest.raises(RuntimeError, match="정확히 일치"):
        runtime._activate_recipient_result(automation.main, "형택", {}, search)
    assert automation.enter_count == 0


def test_space_different_uia_contact_is_never_invoked(monkeypatch):
    automation = VerifiedMessagingAutomation()
    runtime = _runtime(monkeypatch, automation)
    original_activate = automation.activate_accessibility_control
    search = original_activate(automation.main.handle, control_types=["Edit"])

    def different_display_name(handle, **kwargs):
        result = original_activate(handle, **kwargs)
        if kwargs.get("names") == ["형택"]:
            assert kwargs["invoke"] is False
            result["name"] = "형 택"
        return result

    automation.activate_accessibility_control = different_display_name
    with pytest.raises(RuntimeError, match="표시 이름"):
        runtime._activate_recipient_result(automation.main, "형택", {}, search)
    assert automation.enter_count == 0


def test_read_returns_only_visible_semantic_message_items_without_sending(monkeypatch):
    automation = VerifiedMessagingAutomation()
    runtime = _runtime(monkeypatch, automation)

    def item(name, token, automation_id, top, **extra):
        return {
            "name": name, "control_type": "Text", "automation_id": automation_id,
            "rectangle": [100, top, 300, top + 30], "class_name": "Text",
            "control_identity": {"runtime_id": [token], "token": str(token)}, **extra,
        }

    automation.accessibility_controls = lambda _handle: [
        item("알겠어", 2, "OutgoingMessageBody", 300),
        item("오늘 7시에 보자", 1, "IncomingMessageBody", 100, x_ratio=0.2),
        item("오후 6:30", 3, "outgoing-timestamp", 310),
        item("보내지 않은 초안", 4, "outgoing-composer", 500, class_name="RichEdit50W"),
        item("검색", 5, "search", 20),
    ]
    result = runtime.read("카톡", "형택", limit=2)
    assert [(message["text"], message["direction"]) for message in result["messages"]] == [
        ("오늘 7시에 보자", "incoming"), ("알겠어", "outgoing"),
    ]
    assert result["read_verified"] is True
    assert result["visible_only"] is True
    assert result["history_complete"] is False
    assert automation.enter_count == 0
    assert automation.text_set_calls == []


def test_read_empty_accessibility_tree_does_not_claim_no_received_messages(monkeypatch):
    automation = VerifiedMessagingAutomation()
    runtime = _runtime(monkeypatch, automation)
    automation.accessibility_controls = lambda _handle: []
    result = runtime.read("카톡", "형택")
    assert result["read_verified"] is False
    assert result["messages"] == []
    assert "메시지가 없다는 뜻은 아닙니다" in result["unverified_reason"]
    assert automation.enter_count == 0


def test_outgoing_marker_does_not_match_arbitrary_message_text_or_present_class():
    config = {"outgoing_message_direction_keywords": ["sent", "mine"]}
    assert DesktopMessagingRuntime._is_outgoing_occurrence({
        "class_name": "PresentText", "ancestor_names": ["mine sent this"],
    }, config) is False


def _vision_observation(color):
    import io
    from PIL import Image
    from core.computer_use import Observation
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), color).save(buffer, format="PNG")
    return Observation(color, buffer.getvalue(), "", {}, {"bounds": [0, 0, 100, 100]})


def test_read_uses_existing_vision_model_when_kakao_history_is_not_accessible(monkeypatch):
    automation = VerifiedMessagingAutomation()
    runtime = _runtime(monkeypatch, automation)
    automation.accessibility_controls = lambda _handle: []
    observation = _vision_observation("white")
    monkeypatch.setattr(runtime, "_capture_chat_observation", lambda _chat: observation)
    monkeypatch.setattr("core.kakao_vision.read_messages", lambda *_args: [{
        "text": "오늘 7시에 보자", "direction": "incoming", "sender": "",
    }])
    result = runtime.read("카톡", "형택")
    assert result["read_verified"] is True
    assert result["read_method"] == "vision_visible_chat_bubbles"
    assert result["screenshot_sha256"] == observation.image_hash
    assert automation.enter_count == 0


@pytest.mark.parametrize("result_change, expected_open", [
    ({}, True), ({"all_results_visible": False}, False),
    ({"count_label": "친구 2", "total_results": 2}, False),
    ({"friends_results": False}, False),
    ({"candidates": [{"name": "형 택", "x": 30, "y": 60}]}, False),
    ({"count_label": "친구 2", "total_results": 2, "candidates": [
        {"name": "형택", "x": 30, "y": 60}, {"name": "형택", "x": 30, "y": 80},
    ]}, False),
    ({"count_label": "친구 2", "total_results": 2, "candidates": [
        {"name": "형택", "x": 30, "y": 60}, {"name": "형택의 어머니", "x": 30, "y": 80},
    ]}, True),
])
@pytest.mark.parametrize("search_prepared", [False, True])
def test_visual_contact_navigation_requires_complete_unique_friend_results(monkeypatch, result_change, expected_open, search_prepared):
    import sys
    import core.kakao_vision as vision
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    chat = WindowInfo(11, "형택 - 카카오톡", 99, True, True, True)
    identity = {"stable": True, "token": "search-1", "runtime_id": [1]}
    actions, double_clicks = [], []

    class Backend:
        def __init__(self, **_kwargs):
            self.window = main
            self.query = "형택" if search_prepared else ""
            self.automation = SimpleNamespace(
                read_accessibility_text=lambda *_args, **_kwargs: {"value": self.query},
                list_windows=lambda: [main, chat] if double_clicks else [main],
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def observe(self):
            observation = _vision_observation("white")
            observation.context.update({
                "handle": 10, "width": 100, "height": 100,
                "actions": ["coordinate_click", "fill", "type_text"],
            })
            observation.host_state["focused_identity"] = identity
            observation.targets = {"e1": {
                "control_type": "Edit", "bounds": [5, 5, 50, 25],
                "control_identity": identity, "actions": ["fill"], "value": self.query,
            }}
            return observation

        def validate_action(self, _observation, decision):
            assert decision["action"] in {"coordinate_click", "fill", "type_text"}

        def execute(self, decision, _check):
            actions.append(decision)
            if decision["action"] == "type_text":
                self.query = decision["text"]

        def _root(self):
            return None

        _screen_point = staticmethod(lambda _bounds, _size, x, y: (x, y))

    answers = iter(([] if search_prepared else [{"kind": "search_field", "x": 15, "y": 15}]) + [
        {"friends_results": True, "all_results_visible": True, "count_label": "친구 1",
         "total_results": 1, "candidates": [{"name": "형택", "x": 30, "y": 60}], **result_change},
    ])
    monkeypatch.setattr(vision, "WindowsComputerBackend", Backend)
    monkeypatch.setattr(vision.time, "sleep", lambda _seconds: None)
    monkeypatch.setitem(sys.modules, "pywinauto.mouse", SimpleNamespace(
        double_click=lambda **kwargs: double_clicks.append(kwargs),
    ))
    model = SimpleNamespace(_call=lambda *_args: next(answers))
    if expected_open:
        result = vision.open_recipient_chat(main, "형택", model=model, search_prepared=search_prepared)
        assert result["window_handle"] == chat.handle
        assert double_clicks == [{"coords": (30, 60)}]
    else:
        with pytest.raises(RuntimeError, match="수신자를 하나로"):
            vision.open_recipient_chat(main, "형택", model=model, search_prepared=search_prepared)
        assert double_clicks == []
    assert [action["action"] for action in actions] == ([] if search_prepared else ["coordinate_click", "fill", "type_text"])
    if not search_prepared:
        assert actions[-1]["text"] == "형택"


def test_contact_search_uses_bound_uia_edit_without_global_typing(monkeypatch):
    automation = VerifiedMessagingAutomation()
    automation.foreground_handle = automation.main.handle
    runtime = _runtime(monkeypatch, automation)
    search = automation.activate_accessibility_control(
        automation.main.handle, control_types=["Edit"],
    )
    monkeypatch.setattr(runtime, "_hotkey", lambda _keys: pytest.fail("global hotkey forbidden"))
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

    with pytest.raises(RuntimeError, match="대화방 열기를 중단"):
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
