import json
from types import SimpleNamespace

import pytest

from core.plugin import PluginRegistry
from core.tool_result import ToolRunStatus
from plugins.desktop_messaging import DesktopMessagingPlugin
from core.windows_automation import WindowInfo


def test_send_capability_is_removed_and_stale_calls_cannot_send():
    plugin = DesktopMessagingPlugin()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    assert registry.get_capability("desktop_send_message") is None
    assert [intent.name for intent in plugin.get_intents()] == ["messaging.read"]
    assert not hasattr(plugin.runtime, "send")
    plugin.runtime = SimpleNamespace()
    result = plugin.execute_tool("desktop_send_message", {
        "provider": "kakaotalk", "recipient": "테스트 상대", "message": "안녕",
    })
    assert result.status == ToolRunStatus.FAILED


def test_read_contract_is_read_only_and_requires_exact_chat():
    registry = PluginRegistry()
    registry.register_plugin(DesktopMessagingPlugin())
    contract = registry.get_capability("desktop_read_messages")
    assert contract.side_effect == "read"
    assert contract.input_schema["required"] == ["provider", "recipient"]
    assert contract.max_retries == 0


def test_read_reports_visible_messages_with_scope():
    plugin = DesktopMessagingPlugin()
    calls = []
    payload = {"recipient": "테스트 상대", "read_verified": True,
               "messages": [{"text": "오늘 만나요", "direction": "incoming"},
                            {"text": "네", "direction": "outgoing"}]}
    plugin.runtime = SimpleNamespace(read=lambda *args, **kwargs: (calls.append((args, kwargs)) or payload))
    result = plugin.execute_tool("desktop_read_messages", {"provider": "kakaotalk", "recipient": "테스트 상대", "limit": 2})
    assert result.status == ToolRunStatus.SUCCEEDED
    assert calls == [(("kakaotalk", "테스트 상대"), {"limit": 2})]
    presented = plugin.present_result("desktop_read_messages", result.raw_output)
    assert "상대: 오늘 만나요" in presented
    assert "나: 네" in presented
    assert "전체 기록" in presented


def test_inaccessible_history_is_unverified_not_no_messages():
    plugin = DesktopMessagingPlugin()
    payload = {"recipient": "테스트 상대", "read_verified": False,
               "messages": [], "unverified_reason": "메시지 본문을 확인할 수 없습니다."}
    plugin.runtime = SimpleNamespace(read=lambda *args, **kwargs: payload)
    result = plugin.execute_tool("desktop_read_messages", {"recipient": "테스트 상대"})
    assert result.status == ToolRunStatus.UNVERIFIED
    assert "읽지 못했습니다" in plugin.present_result("desktop_read_messages", json.dumps(payload))




def test_native_friend_search_waits_for_verified_view_and_edit(monkeypatch):
    runtime = DesktopMessagingPlugin().runtime
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    views = iter([[], [{"name": "ContactListView_123"}]])
    hotkeys, attempts = [], []

    def activate(_handle, **kwargs):
        assert kwargs["require_unique"] and kwargs["require_keyboard_focus"] and kwargs["require_foreground"]
        attempts.append(True)
        if len(attempts) == 1:
            raise LookupError("edit not ready")
        return {"control_identity": {"token": "search"}}

    runtime.automation = SimpleNamespace(accessibility_controls=lambda _h: next(views),
                                         activate_accessibility_control=activate)
    monkeypatch.setattr(runtime, "_verify_provider_foreground", lambda _m: main)
    monkeypatch.setattr(runtime, "_close_forbidden_provider_windows", lambda *_a: None)
    monkeypatch.setattr(runtime, "_hotkey", lambda keys: hotkeys.append(keys))
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _s: None)
    result = runtime._focus_contact_search(main, {"contact_view_accessibility_name_prefix": "ContactListView_"})
    assert result["control_identity"]["token"] == "search"
    assert hotkeys == [["CTRL", "SHIFT", "TAB"], ["CTRL", "F"]]
    assert len(attempts) == 2


def test_native_friend_search_never_types_when_view_is_unverified(monkeypatch):
    runtime = DesktopMessagingPlugin().runtime
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    hotkeys = []
    runtime.automation = SimpleNamespace(accessibility_controls=lambda _h: [])
    monkeypatch.setattr(runtime, "_verify_provider_foreground", lambda _m: main)
    monkeypatch.setattr(runtime, "_hotkey", lambda keys: hotkeys.append(keys))
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _s: None)
    with pytest.raises(LookupError, match="친구 목록"):
        runtime._focus_contact_search(main, {"contact_view_accessibility_name_prefix": "ContactListView_"})
    assert all(keys != ["CTRL", "F"] for keys in hotkeys)
