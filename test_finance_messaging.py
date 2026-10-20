import ctypes
import json
import core.desktop_messaging as desktop_messaging_module
from core.desktop_messaging import DesktopMessagingRuntime
from core.executor import Executor
from core.intent_router import IntentRouter
from core.planner import _parse_json_object
from core.plugin import PluginRegistry
from core.rag import VectorRAGManager
from core.windows_automation import WindowInfo, WindowsAutomationRuntime
from plugins.desktop_messaging import DesktopMessagingPlugin
from plugins.finance import FinancePlugin


def _router():
    registry = PluginRegistry()
    registry.register_plugin(FinancePlugin())
    registry.register_plugin(DesktopMessagingPlugin())
    return IntentRouter(registry)


def test_finance_request_routes_to_analysis_not_browser():
    resolution = _router().resolve("삼성전자 주식 분석해줄래?")
    assert resolution.ready
    assert resolution.intent_name == "finance.equity_analysis"
    assert resolution.tool_name == "finance_analyze_equity"
    assert resolution.slots["query"] == "삼성전자"


def test_market_overview_and_equity_analysis_are_distinct():
    router = _router()
    market = router.resolve("한국 주식시장 분석해줘")
    equity = router.resolve("삼성전자 종목 분석해줘")
    assert market.intent_name == "finance.market_overview"
    assert market.slots["market"] == "한국"
    assert equity.intent_name == "finance.equity_analysis"


def test_finance_presenter_contains_calculation_scope_and_risk():
    plugin = FinancePlugin()
    payload = {
        "name": "Samsung Electronics Co., Ltd.", "symbol": "005930.KS",
        "latest_close": 100.0, "currency": "KRW", "data_end": "2026-08-21",
        "change_1d_percent": 1.0, "return_5d_percent": 2.0,
        "return_1m_percent": 3.0, "return_3m_percent": 4.0,
        "trend_observation": "단기 강세", "ma20": 95.0, "ma60": 90.0,
        "annualized_volatility_percent": 20.0, "max_drawdown_percent": -15.0,
        "period_low": 70.0, "period_high": 110.0,
        "period_range_position_percent": 75.0, "volume_ratio_20d": 1.2,
        "market_cap": 500_000_000_000_000, "forward_pe": 10.0,
        "trailing_pe": None, "price_to_book": 1.5, "sessions": 250,
        "retrieved_at": "2026-08-21T12:00:00+09:00",
    }
    result = plugin.present_result("finance_analyze_equity", json.dumps(payload))
    assert "전일 대비" in result
    assert "최대 낙폭" in result
    assert "선행 PER" in result
    assert "정량 요약" in result
    assert "매수·매도 추천은 아닙니다" in result


def test_persistent_user_rule_is_not_a_pending_slot_answer():
    assert Executor._is_independent_declaration(
        "앞으로 카톡이라고 말하면 카카오톡을 말하는 거야."
    )
    assert not Executor._is_independent_declaration("형택")


def test_desktop_message_runtime_never_uses_provider_search_hotkey(monkeypatch):
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)

    class Automation:
        def list_windows(self):
            return [main]

        def activate_accessibility_control(self, _handle, **kwargs):
            names = kwargs.get("names") or []
            if "친구" in names:
                return {"control_type": "TabItem", "activation": "invoke"}
            return {"control_type": "Edit", "activation": "focus"}

    runtime.automation = Automation()
    hotkeys = []
    monkeypatch.setattr(runtime, "_hotkey", hotkeys.append)
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)

    control = runtime._focus_contact_search(main, {
        "contact_search_hotkey": ["CTRL", "F"],
        "forbidden_dialog_titles": ["친구 추가"],
    })

    assert control["control_type"] == "Edit"
    assert hotkeys == []


def test_desktop_message_runtime_never_guesses_contact_name_from_korean_suffix():
    assert DesktopMessagingRuntime._recipient_name_candidates("형택이") == ["형택이"]
    assert DesktopMessagingRuntime._recipient_name_candidates("민희") == ["민희"]


def test_win32_input_structure_matches_native_abi():
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(desktop_messaging_module._Input) == expected


def test_checked_enter_uses_native_sendinput_keydown_and_keyup(monkeypatch):
    captured = {}

    def dispatch(array):
        captured["keys"] = [int(item.ki.wVk) for item in array]
        captured["flags"] = [int(item.ki.dwFlags) for item in array]
        return len(array)

    monkeypatch.setattr(desktop_messaging_module, "_dispatch_inputs", dispatch)
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)

    DesktopMessagingRuntime._press("ENTER")

    assert captured["keys"] == [DesktopMessagingRuntime.VK["ENTER"]] * 2
    assert captured["flags"] == [
        0, DesktopMessagingRuntime.KEYEVENTF_KEYUP,
    ]


def test_accessibility_text_setter_reads_back_exact_unicode(monkeypatch):
    runtime = WindowsAutomationRuntime()

    class Wrapper:
        value = ""

        def set_focus(self):
            pass

        def set_edit_text(self, value):
            self.value = value

        def get_value(self):
            return self.value

    wrapper = Wrapper()
    monkeypatch.setattr(
        runtime, "_accessibility_candidates",
        lambda *_args, **_kwargs: [(100, wrapper, {
            "name": "메시지 입력", "automation_id": "chat-input",
            "control_type": "Edit",
        })],
    )
    monkeypatch.setattr("core.windows_automation.time.sleep", lambda _seconds: None)

    result = runtime.set_accessibility_text(11, "한글 테스트")
    state = runtime.read_accessibility_text(11)

    assert result["value_verified"] is True
    assert result["text_method"] == "set_edit_text"
    assert state["value"] == "한글 테스트"


def test_accessibility_text_setter_accepts_native_richedit_terminator(monkeypatch):
    runtime = WindowsAutomationRuntime()

    class Wrapper:
        value = "\r"

        def set_focus(self):
            pass

        def set_edit_text(self, value):
            self.value = str(value) + "\r"

        def get_value(self):
            return self.value

    wrapper = Wrapper()
    monkeypatch.setattr(runtime, "_accessibility_candidates", lambda *_args, **_kwargs: [(
        100, wrapper, {
            "name": "RichEdit Control", "automation_id": "1006",
            "control_type": "Document", "class_name": "RICHEDIT50W",
        },
    )])
    monkeypatch.setattr("core.windows_automation.time.sleep", lambda _seconds: None)

    result = runtime.set_accessibility_text(11, "한글 테스트", control_types=["Document"])
    cleared = runtime.set_accessibility_text(11, "", control_types=["Document"])

    assert result["value_verified"] is True
    assert result["value"] == "한글 테스트\r"
    assert cleared["value_verified"] is True
    assert cleared["value"] == "\r"


def test_desktop_message_runtime_closes_friend_add_before_typing():
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    friend_add = WindowInfo(12, "친구 추가", 99, True, True, True)

    class Automation:
        def __init__(self):
            self.closed = []

        def list_windows(self):
            return [main, friend_add]

        def close_window(self, handle):
            self.closed.append(handle)

    runtime.automation = Automation()
    try:
        runtime._close_forbidden_provider_windows(99, {
            "forbidden_dialog_titles": ["친구 추가"],
        })
    except RuntimeError as exc:
        assert "입력 전에 중단" in str(exc)
    else:
        raise AssertionError("친구 추가 창이 열렸으면 전송을 중단해야 합니다")
    assert runtime.automation.closed == [12]


def test_single_global_namespace_uses_direct_chroma_filter():
    manager = VectorRAGManager.__new__(VectorRAGManager)
    manager.namespace = "global"
    manager.reranker = None
    manager._all_lexical_candidates = lambda _query: []
    manager._filter_and_rerank = lambda _query, items, *_args: items
    manager._attach_confidence = lambda items, _query: items

    class Collection:
        where = None

        def query(self, **kwargs):
            self.where = kwargs["where"]
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    manager.collection = Collection()
    manager._hybrid_search("테스트", 3)
    assert manager.collection.where == {"namespace": "global"}


def test_planner_extracts_json_after_explanatory_text():
    parsed = _parse_json_object('설명입니다.\n```json\n{"tasks": []}\n```')
    assert parsed == {"tasks": []}
