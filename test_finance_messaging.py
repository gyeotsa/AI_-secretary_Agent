import ctypes
import json
import types

import pytest

import core.desktop_messaging as desktop_messaging_module
from core.desktop_messaging import DesktopMessagingRuntime
from core.dialogue_state import DialogueStateStore
from core.executor import ExecutionOutcome, Executor
from core.intent_router import IntentRouter
from core.plan_runtime import (
    PlanCoordinator, PlanDAG, PlanExecutionStore, PlanRunResult, PlanStep, StepStatus,
)
from core.planner import _parse_json_object
from core.plugin import PluginRegistry
from core.rag import VectorRAGManager
from core.tool_result import Evidence, ToolRunResult, ToolRunStatus
from core.task_contracts import SupervisorRuntime, TaskContractStore
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


def test_kakaotalk_request_extracts_recipient_and_message_without_stock_context():
    resolution = _router().resolve("카카오 톡으로 형택이에게 테스트 라고 보내줄래?")
    assert resolution.ready
    assert resolution.intent_name == "messaging.send"
    assert resolution.request_type == "external_send"
    assert resolution.slots == {
        "provider": "kakaotalk", "recipient": "형택이", "message": "테스트",
    }


def test_kakaotalk_parser_supports_natural_recipient_first_word_order():
    resolution = _router().resolve("형택이에게 테스트123 이라고 카톡 보내줘")
    assert resolution.ready
    assert resolution.slots == {
        "provider": "kakaotalk", "recipient": "형택이", "message": "테스트123",
    }


def test_kakaotalk_parser_supports_standalone_talk_alias_and_counter():
    resolution = _router().resolve("형택이한테 테스트라고 톡 하나 보내줘")
    assert resolution.ready
    assert resolution.intent_name == "messaging.send"
    assert resolution.tool_name == "desktop_send_message"
    assert resolution.slots == {
        "provider": "kakaotalk", "recipient": "형택이", "message": "테스트",
    }


def test_kakaotalk_parser_supports_polite_delivery_capability_phrase():
    resolution = _router().resolve(
        "형택이에게 테스트라고 카톡으로 전달해줄 수 있어?"
    )
    assert resolution.ready
    assert resolution.intent_name == "messaging.send"
    assert resolution.tool_name == "desktop_send_message"
    assert resolution.execution_requested
    assert resolution.slots == {
        "provider": "kakaotalk", "recipient": "형택이", "message": "테스트",
    }


def test_kakaotalk_pending_slots_accept_short_recipient_and_message_answers():
    router = _router()
    first = router.resolve("카톡 보내줘")
    assert first.question == "누구에게 보낼까요, 보스?"
    recipient = router.resolve("형택", first.intent_name, first.slots)
    assert recipient.question == "어떤 내용을 보낼까요, 보스?"
    message = router.resolve("테스트123", recipient.intent_name, recipient.slots)
    assert message.ready
    assert message.slots == {
        "provider": "kakaotalk", "recipient": "형택", "message": "테스트123",
    }


def test_kakaotalk_speech_particle_typo_still_extracts_recipient():
    resolution = _router().resolve("형택이게 카톡 보내줘")
    assert resolution.question == "어떤 내용을 보낼까요, 보스?"
    assert resolution.slots["recipient"] == "형택"


def test_persistent_user_rule_is_not_a_pending_slot_answer():
    assert Executor._is_independent_declaration(
        "앞으로 카톡이라고 말하면 카카오톡을 말하는 거야."
    )
    assert not Executor._is_independent_declaration("형택")


def test_executor_routes_independent_message_before_context_rewrite(tmp_path):
    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor.intent_router = _router()
    executor._progress_callback = None
    executor.current_agent_task_id = ""
    executor._task_controls = {}
    executor.llm = object()
    executor.tool_executor = object()

    class NeverCalledResolver:
        def resolve(self, *_args):
            raise AssertionError("등록된 독립 Intent는 문맥 LLM으로 보내면 안 됩니다")

    executor.context_resolver = NeverCalledResolver()
    captured = {}

    def execute_resolved(self, resolution, goal, session_id, task_id="", progress_callback=None):
        captured.update(intent=resolution.intent_name, goal=goal, slots=resolution.slots)
        return ExecutionOutcome("captured", "completed", goal)

    executor._execute_resolved_intent = types.MethodType(execute_resolved, executor)
    outcome = executor._execute_turn_impl(
        "카카오 톡으로 형택이에게 테스트 라고 보내줄래?", "session", [
            {"role": "user", "content": "삼성전자 주식 분석해줄래?"},
            {"role": "assistant", "content": "삼성전자 분석 결과"},
        ]
    )

    assert outcome.response == "captured"
    assert captured == {
        "intent": "messaging.send",
        "goal": "카카오 톡으로 형택이에게 테스트 라고 보내줄래?",
        "slots": {"provider": "kakaotalk", "recipient": "형택이", "message": "테스트"},
    }


def _executor_with_approval_tasks(tmp_path, count=1):
    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor.intent_router = _router()
    executor.current_agent_task_id = ""
    calls = []

    for index in range(count):
        recipient = f"형택이{index + 1}" if count > 1 else "형택이"
        goal = f"카카오톡으로 {recipient}에게 테스트라고 보내줘"
        task = executor.dialogue_state.create_task("session", goal)
        slots = {"provider": "kakaotalk", "recipient": recipient, "message": "테스트"}
        plan = PlanDAG(goal=goal, steps=[PlanStep(
            id=f"intent-{task.task_id}", description="messaging.send",
            tool_name="desktop_send_message", tool_input=slots,
            requires_approval=True,
            approval_reason="외부 대상에게 데이터를 전송하는 작업입니다.",
            retry_budget=0, status=StepStatus.AWAITING_APPROVAL,
        )])
        assert executor.dialogue_state.transition_task(
            task.task_id, "running", intent_name="messaging.send", slots=slots,
            plan=plan.to_dict()["steps"], plan_id=plan.plan_id,
        )
        assert executor.dialogue_state.transition_task(
            task.task_id, "awaiting_approval",
            pending_question=executor._approval_request_message(plan.steps),
        )

    def execute_plan(self, plan, approval_callback=None, approved_step_ids=None,
                     replan_callback=None):
        assert approved_step_ids == [plan.steps[0].id]
        calls.append(dict(plan.steps[0].tool_input))
        plan.steps[0].status = StepStatus.COMPLETED
        raw = json.dumps({
            "provider": "kakaotalk",
            "recipient": plan.steps[0].tool_input["recipient"],
            "window_title": plan.steps[0].tool_input["recipient"],
            "window_handle": 11, "process_id": 99,
            "input_dispatched_at": "2026-08-21T12:00:00+09:00",
            "send_accepted_verified": True,
            "outgoing_message_verified": True,
            "send_verification": "new_exact_outgoing_message_bubble",
            "delivery_receipt_verified": False,
        }, ensure_ascii=False)
        result = ToolRunResult.successful(
            tool_name="desktop_send_message", raw_output=raw,
            evidence=[Evidence(
                "desktop_message_dispatch", "정확한 수신자 창에 키 입력을 전달했습니다.",
                {"recipient": plan.steps[0].tool_input["recipient"]},
            )],
        )
        return PlanRunResult(plan, "completed", {plan.steps[0].id: result})

    executor.execute_plan_dag = types.MethodType(execute_plan, executor)
    return executor, calls


def test_bare_approval_resumes_unique_message_with_evidence_based_result(tmp_path):
    executor, calls = _executor_with_approval_tasks(tmp_path)

    outcome = executor._execute_turn_impl("승인", "session")

    assert calls == [{
        "provider": "kakaotalk", "recipient": "형택이", "message": "테스트",
    }]
    assert outcome.status == "completed"
    assert "새 보낸 메시지 말풍선이 나타난 것까지 확인했습니다" in outcome.response
    assert "읽음 여부는 확인하지 못했습니다" in outcome.response
    assert "메시지가 보내졌어요" not in outcome.response
    stored = executor.dialogue_state.get_task("session", outcome.task_id, "")
    assert stored is not None
    assert stored.status == "completed"
    assert stored.evidence[0]["kind"] == "desktop_message_dispatch"


def test_bare_approval_never_falls_through_to_conversation_without_pending_task(tmp_path):
    executor, calls = _executor_with_approval_tasks(tmp_path, count=0)

    outcome = executor._execute_turn_impl("승인해줘", "session")

    assert calls == []
    assert outcome.status == "completed"
    assert "승인 대기 중인 작업이 없습니다" in outcome.response


def test_bare_approval_requires_task_id_when_multiple_are_waiting(tmp_path):
    executor, calls = _executor_with_approval_tasks(tmp_path, count=2)

    outcome = executor._execute_turn_impl("허용", "session")

    assert calls == []
    assert outcome.status == "awaiting_approval"
    assert "여러 개라 임의로 실행하지 않았습니다" in outcome.response
    assert outcome.response.count("- 작업 ") == 2


def test_bare_task_id_selects_and_approves_waiting_task(tmp_path):
    executor, calls = _executor_with_approval_tasks(tmp_path)
    task = executor.dialogue_state.list_tasks(
        "session", include_finished=False, workspace_path="",
    )[0]

    outcome = executor._execute_turn_impl(task.task_id, "session")

    assert outcome.status == "completed"
    assert calls == [{
        "provider": "kakaotalk", "recipient": "형택이", "message": "테스트",
    }]
    assert "성공적으로 보내졌어요" not in outcome.response


def test_stale_planner_approval_is_rebuilt_from_registry_contract(tmp_path):
    executor, calls = _executor_with_approval_tasks(tmp_path)
    task = executor.dialogue_state.list_tasks(
        "session", include_finished=False, workspace_path="",
    )[0]
    stale = PlanDAG(goal=task.goal, steps=[
        PlanStep(
            id="target-check", description="대상자 확인",
            requires_approval=True, approval_reason="대상자 확인",
            status=StepStatus.AWAITING_APPROVAL,
        ),
        PlanStep(
            id="send", description="잘못된 Planner 전송",
            tool_name="desktop_send_message",
            tool_input={"service": "kakaotalk", "target": "형택이", "content": "테스트"},
            dependencies=["target-check"],
        ),
    ])
    executor.dialogue_state.update_task(
        task.task_id, intent_name="", slots={},
        plan=stale.to_dict()["steps"], plan_id=stale.plan_id,
    )

    outcome = executor._execute_turn_impl(task.task_id, "session")

    assert outcome.status == "completed"
    assert calls == [{
        "provider": "kakaotalk", "recipient": "형택이", "message": "테스트",
    }]


def test_equivalent_pending_external_send_is_cancelled_before_new_approval(tmp_path):
    executor, _calls = _executor_with_approval_tasks(tmp_path)
    old = executor.dialogue_state.list_tasks(
        "session", include_finished=False, workspace_path="",
    )[0]
    resolution = executor.intent_router.resolve(
        "형택이한테 테스트라고 톡 하나 보내줘"
    )

    executor._cancel_equivalent_approval_tasks("session", "", resolution)

    stored = executor.dialogue_state.get_task("session", old.task_id, "")
    assert stored is not None
    assert stored.status == "cancelled"
    assert stored.result == "동일한 새 요청으로 대체됨"


def test_approval_prompt_hides_internal_intent_and_task_identifier():
    message = Executor._approval_request_message([PlanStep(
        id="intent-a2b9f555", description="messaging.send",
        tool_name="desktop_send_message",
        tool_input={"provider": "kakaotalk", "recipient": "형택이", "message": "테스트"},
        requires_approval=True,
        approval_reason="외부 대상에게 데이터를 전송하는 작업입니다.",
    )])

    assert "messaging.send" not in message
    assert "a2b9f555" not in message
    assert "대상: 형택이" in message
    assert "내용: 테스트" in message
    assert "‘승인’" in message


def test_external_send_executes_only_after_bare_approval_through_real_plan_runtime(tmp_path):
    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor.intent_router = _router()
    executor.current_agent_task_id = ""
    executor.model_role_router = None
    executor.plan_coordinator = PlanCoordinator(
        PlanExecutionStore(str(tmp_path / "plans.db")), max_parallel=1,
    )
    executor.supervisor = SupervisorRuntime(
        TaskContractStore(str(tmp_path / "contracts.db"))
    )
    calls = []

    class ToolExecutor:
        plugin_registry = executor.intent_router.registry

        def execute_tool(self, tool_name, tool_input):
            calls.append((tool_name, dict(tool_input)))
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=json.dumps({
                    "provider": "kakaotalk", "recipient": tool_input["recipient"],
                    "window_title": tool_input["recipient"], "window_handle": 11,
                    "process_id": 99,
                    "input_dispatched_at": "2026-08-21T12:00:00+09:00",
                    "send_accepted_verified": True,
                    "outgoing_message_verified": True,
                    "send_verification": "new_exact_outgoing_message_bubble",
                    "delivery_receipt_verified": False,
                }, ensure_ascii=False),
                evidence=[Evidence(
                    "desktop_message_dispatch", "정확한 수신자 창에 키 입력을 전달했습니다.",
                    {"recipient": tool_input["recipient"]},
                )],
            )

    executor.tool_executor = ToolExecutor()
    resolution = executor.intent_router.resolve(
        "카카오톡으로 형택이에게 테스트라고 보내줘"
    )

    waiting = executor._execute_resolved_intent(
        resolution, "카카오톡으로 형택이에게 테스트라고 보내줘", "session"
    )
    assert waiting.status == "awaiting_approval"
    assert calls == []
    assert "messaging.send" not in waiting.response
    assert "작업 " not in waiting.response

    completed = executor._execute_turn_impl("승인", "session")
    assert completed.status == "completed"
    assert calls == [(
        "desktop_send_message",
        {"provider": "kakaotalk", "recipient": "형택이", "message": "테스트"},
    )]
    assert "새 보낸 메시지 말풍선이 나타난 것까지 확인했습니다" in completed.response
    assert "읽음 여부는 확인하지 못했습니다" in completed.response


def test_unverified_external_send_is_never_completed_or_replayed_after_approval(tmp_path):
    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor.intent_router = _router()
    executor.current_agent_task_id = ""
    executor.model_role_router = None
    executor.plan_coordinator = PlanCoordinator(
        PlanExecutionStore(str(tmp_path / "plans.db")), max_parallel=1,
    )
    executor.supervisor = SupervisorRuntime(
        TaskContractStore(str(tmp_path / "contracts.db"))
    )
    calls = []

    class ToolExecutor:
        plugin_registry = executor.intent_router.registry

        def execute_tool(self, tool_name, tool_input):
            calls.append((tool_name, dict(tool_input)))
            return ToolRunResult.unverified(
                tool_name=tool_name,
                raw_output=(
                    "입력창에서 전송 키는 처리했지만 새 보낸 메시지 말풍선을 "
                    "확인하지 못해 전송 완료로 확정하지 않았습니다."
                ),
            )

    executor.tool_executor = ToolExecutor()
    resolution = executor.intent_router.resolve(
        "형택이에게 테스트라고 카톡으로 전달해줄 수 있어?"
    )

    waiting = executor._execute_resolved_intent(
        resolution, "형택이에게 테스트라고 카톡으로 전달해줄 수 있어?", "session"
    )
    assert waiting.status == "awaiting_approval"
    assert calls == []

    outcome = executor._execute_turn_impl("승인", "session")
    assert outcome.status == "failed"
    assert len(calls) == 1
    assert outcome.tool_result is not None
    assert outcome.tool_result.status == ToolRunStatus.UNVERIFIED
    assert "완료하지 못했습니다" in outcome.response
    assert "말풍선" in outcome.response
    assert "전송했습니다" not in outcome.response

    stored = executor.dialogue_state.get_task("session", outcome.task_id, "")
    assert stored is not None
    assert stored.status == "failed"
    assert stored.verification_status == "failed"


def test_desktop_message_runtime_verifies_exact_recipient_before_dispatch(monkeypatch):
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    chat = WindowInfo(11, "형택이", 99, True, True, True)
    class Automation:
        def __init__(self):
            self.composer_value = ""
            self.bubbles = []
            self.composer_identity = {
                "window_handle": chat.handle,
                "runtime_id": [42, 1],
                "control_type": "Edit",
                "token": "composer-runtime-id-42-1",
            }

        def list_windows(self):
            return [main, chat]

        def focus(self, handle):
            return chat if handle == chat.handle else main

        def close_window(self, _handle):
            raise AssertionError("금지된 창이 없으므로 닫기 호출이 없어야 합니다")

        def activate_accessibility_control(self, _handle, **kwargs):
            names = kwargs.get("names") or []
            if names == ["형택이"]:
                return {"control_type": "ListItem", "activation": "invoke"}
            if "친구" in names:
                return {"control_type": "TabItem", "activation": "invoke"}
            return {"control_type": "Edit", "activation": "focus"}

        def set_accessibility_text(self, _handle, value, **_kwargs):
            self.composer_value = value
            return {
                "control_type": "Edit", "value_verified": True,
                "control_identity": self.composer_identity,
            }

        def read_accessibility_text(self, _handle, **kwargs):
            return {
                "control_type": "Edit", "value": self.composer_value,
                "control_identity": self.composer_identity,
                "keyboard_focus_verified": bool(kwargs.get("require_keyboard_focus")),
            }

        def accessibility_text_occurrences(self, _handle, value, **_kwargs):
            return [item for item in self.bubbles if item["text"] == value]

    automation = Automation()
    runtime.automation = automation
    typed = []
    pressed = []
    monkeypatch.setattr(runtime, "_hotkey", lambda keys: None)
    monkeypatch.setattr(runtime, "_type_unicode", typed.append)
    def press(key):
        pressed.append(key)
        if key == "ENTER" and automation.composer_value:
            automation.bubbles.append({
                "text": automation.composer_value,
                "identity_token": "outgoing-bubble-1",
                "outgoing": True,
            })
            automation.composer_value = ""
    monkeypatch.setattr(runtime, "_press", press)
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.desktop_messaging.os.name", "nt")

    result = runtime.send("카카오톡", "형택이", "테스트")

    # 이미 정확히 일치하는 대화창이 열려 있으면 불필요한 메인 검색/재입력을 하지 않는다.
    assert typed == []
    assert pressed == ["ENTER"]
    assert result["recipient"] == "형택이"
    assert result["send_accepted_verified"] is True
    assert result["outgoing_message_verified"] is True
    assert result["delivery_receipt_verified"] is False


def test_desktop_message_runtime_blocks_wrong_recipient_window(monkeypatch):
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    wrong = WindowInfo(11, "다른 사람", 99, True, True, True)

    class Automation:
        search_value = ""
        search_identity = {
            "window_handle": main.handle, "runtime_id": [30, 1],
            "control_type": "Edit", "token": "contact-search-30-1",
        }

        def list_windows(self):
            return [main, wrong]

        def focus(self, _handle):
            return main

        def close_window(self, _handle):
            raise AssertionError("금지된 창이 없으므로 닫기 호출이 없어야 합니다")

        def activate_accessibility_control(self, _handle, **kwargs):
            names = kwargs.get("names") or []
            if names in (["형택이"], ["형택"]):
                raise LookupError("검색 결과에 정확한 이름이 없음")
            if "친구" in names:
                return {"control_type": "TabItem", "activation": "invoke"}
            return {
                "control_type": "Edit", "activation": "focus",
                "control_identity": self.search_identity,
            }

        def set_accessibility_text(self, _handle, value, **_kwargs):
            self.search_value = str(value)
            return {
                "control_type": "Edit", "value": self.search_value,
                "value_verified": True, "control_identity": self.search_identity,
            }

        def read_accessibility_text(self, _handle, **kwargs):
            return {
                "control_type": "Edit", "value": self.search_value,
                "keyboard_focus_verified": bool(kwargs.get("require_keyboard_focus")),
                "control_identity": self.search_identity,
            }

    runtime.automation = Automation()
    typed = []
    monkeypatch.setattr(runtime, "_hotkey", lambda keys: None)
    monkeypatch.setattr(runtime, "_type_unicode", typed.append)
    monkeypatch.setattr(runtime, "_press", lambda key: None)
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.desktop_messaging.time.monotonic", _Monotonic())
    monkeypatch.setattr("core.desktop_messaging.os.name", "nt")

    try:
        runtime.send("카카오톡", "형택이", "전송되면 안 됨", chat_timeout=0.1)
    except RuntimeError as exc:
        assert "전송을 중단" in str(exc)
    else:
        raise AssertionError("잘못된 수신자 창에서 전송이 차단되어야 합니다")
    assert typed == []


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


def test_unicode_input_builds_keydown_and_keyup_with_native_structure(monkeypatch):
    captured = {}

    def dispatch(array):
        captured["count"] = len(array)
        captured["size"] = ctypes.sizeof(type(array)._type_)
        captured["scans"] = [int(item.ki.wScan) for item in array]
        captured["flags"] = [int(item.ki.dwFlags) for item in array]
        return len(array)

    monkeypatch.setattr(desktop_messaging_module, "_dispatch_inputs", dispatch)

    DesktopMessagingRuntime._type_unicode("가")

    assert captured["count"] == 2
    assert captured["size"] == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert captured["scans"] == [ord("가"), ord("가")]
    assert captured["flags"] == [
        DesktopMessagingRuntime.KEYEVENTF_UNICODE,
        DesktopMessagingRuntime.KEYEVENTF_UNICODE | DesktopMessagingRuntime.KEYEVENTF_KEYUP,
    ]


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


def test_desktop_message_runtime_never_falls_back_to_different_korean_contact(monkeypatch):
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    chat = WindowInfo(11, "형택", 99, True, True, True)

    class Automation:
        def __init__(self):
            self.composer_value = ""
            self.search_value = ""
            self.bubbles = []
            self.search_identity = {
                "window_handle": main.handle,
                "runtime_id": [30, 2],
                "control_type": "Edit",
                "token": "contact-search-runtime-id-30-2",
            }
            self.composer_identity = {
                "window_handle": chat.handle,
                "runtime_id": [42, 2],
                "control_type": "Edit",
                "token": "composer-runtime-id-42-2",
            }

        def list_windows(self):
            return [main, chat]

        def focus(self, handle):
            return chat if handle == chat.handle else main

        def close_window(self, _handle):
            raise AssertionError("금지된 창이 없으므로 닫기 호출이 없어야 합니다")

        def activate_accessibility_control(self, _handle, **kwargs):
            names = kwargs.get("names") or []
            if names == ["형택이"]:
                raise LookupError("조사 결합 표현은 실제 친구 이름이 아님")
            if names == ["형택"]:
                return {"control_type": "ListItem", "activation": "invoke"}
            if "친구" in names:
                return {"control_type": "TabItem", "activation": "invoke"}
            return {
                "control_type": "Edit", "activation": "focus",
                "control_identity": self.search_identity,
            }

        def set_accessibility_text(self, handle, value, **_kwargs):
            if int(handle) == main.handle:
                self.search_value = str(value)
                identity = self.search_identity
            else:
                self.composer_value = value
                identity = self.composer_identity
            return {
                "control_type": "Edit", "value_verified": True,
                "value": str(value), "control_identity": identity,
            }

        def read_accessibility_text(self, handle, **kwargs):
            is_search = int(handle) == main.handle
            return {
                "control_type": "Edit",
                "value": self.search_value if is_search else self.composer_value,
                "control_identity": self.search_identity if is_search else self.composer_identity,
                "keyboard_focus_verified": bool(kwargs.get("require_keyboard_focus")),
            }

        def accessibility_text_occurrences(self, _handle, value, **_kwargs):
            return [item for item in self.bubbles if item["text"] == value]

    automation = Automation()
    runtime.automation = automation
    typed = []
    pressed = []
    monkeypatch.setattr(runtime, "_hotkey", lambda _keys: None)
    monkeypatch.setattr(runtime, "_type_unicode", typed.append)
    def press(key):
        pressed.append(key)
        if key == "ENTER" and automation.composer_value:
            automation.bubbles.append({
                "text": automation.composer_value,
                "identity_token": "outgoing-bubble-2",
                "outgoing": True,
            })
            automation.composer_value = ""
    monkeypatch.setattr(runtime, "_press", press)
    monkeypatch.setattr(runtime, "_wait_for_windows", lambda predicate, _timeout: [
        item for item in (main, chat) if predicate(item)
    ])
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.desktop_messaging.os.name", "nt")

    with pytest.raises(RuntimeError, match="수신자 이름과 정확히 일치"):
        runtime.send("카카오톡", "형택이", "테스트")

    assert typed == []
    assert pressed == []
    assert automation.composer_value == ""
    assert automation.bubbles == []


def test_desktop_message_runtime_fails_when_composer_does_not_clear(monkeypatch):
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    chat = WindowInfo(11, "형택", 99, True, True, True)

    class Automation:
        composer_value = ""
        composer_identity = {
            "window_handle": chat.handle,
            "runtime_id": [42, 3],
            "control_type": "Edit",
            "token": "composer-runtime-id-42-3",
        }

        def list_windows(self):
            return [main, chat]

        def focus(self, handle):
            return chat if handle == chat.handle else main

        def close_window(self, _handle):
            raise AssertionError("금지된 창이 없어야 합니다")

        def activate_accessibility_control(self, _handle, **kwargs):
            names = kwargs.get("names") or []
            if names == ["형택"]:
                return {"control_type": "ListItem", "activation": "invoke"}
            if "친구" in names:
                return {"control_type": "TabItem", "activation": "invoke"}
            return {"control_type": "Edit", "activation": "focus"}

        def set_accessibility_text(self, _handle, value, **_kwargs):
            self.composer_value = value
            return {
                "control_type": "Edit", "value_verified": True,
                "control_identity": self.composer_identity,
            }

        def read_accessibility_text(self, _handle, **kwargs):
            return {
                "control_type": "Edit", "value": self.composer_value,
                "control_identity": self.composer_identity,
                "keyboard_focus_verified": bool(kwargs.get("require_keyboard_focus")),
            }

        def accessibility_text_occurrences(self, _handle, _value, **_kwargs):
            return []

    runtime.automation = Automation()
    monkeypatch.setattr(runtime, "_hotkey", lambda _keys: None)
    monkeypatch.setattr(runtime, "_type_unicode", lambda _text: None)
    monkeypatch.setattr(runtime, "_press", lambda _key: None)
    monkeypatch.setattr(runtime, "_wait_for_windows", lambda predicate, _timeout: [
        item for item in (main, chat) if predicate(item)
    ])
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.desktop_messaging.time.monotonic", _Monotonic())
    monkeypatch.setattr("core.desktop_messaging.os.name", "nt")

    try:
        runtime.send(
            "카카오톡", "형택", "전송 실패 검증", send_verify_timeout=0.1,
        )
    except RuntimeError as exc:
        assert "입력창이 비워지지 않아" in str(exc)
    else:
        raise AssertionError("입력창이 유지되면 성공으로 반환하면 안 됩니다")


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


class _Monotonic:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.2
        return self.value


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
