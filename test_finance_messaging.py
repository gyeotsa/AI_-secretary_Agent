import json
import types

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
from core.tool_result import Evidence, ToolRunResult
from core.task_contracts import SupervisorRuntime, TaskContractStore
from core.windows_automation import WindowInfo
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
    assert "메시지 전송 입력을 완료했습니다" in outcome.response
    assert "수신 확인 정보까지는 확인하지 못했습니다" in outcome.response
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
    assert "전송 입력을 완료했습니다" in completed.response
    assert "수신 확인 정보까지는 확인하지 못했습니다" in completed.response


def test_desktop_message_runtime_verifies_exact_recipient_before_dispatch(monkeypatch):
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    chat = WindowInfo(11, "형택이", 99, True, True, True)
    snapshots = [[main], [main, chat], [main, chat]]

    class Automation:
        def list_windows(self):
            return snapshots.pop(0) if snapshots else [main, chat]

        def focus(self, handle):
            return chat if handle == chat.handle else main

    runtime.automation = Automation()
    typed = []
    pressed = []
    monkeypatch.setattr(runtime, "_hotkey", lambda keys: None)
    monkeypatch.setattr(runtime, "_type_unicode", typed.append)
    monkeypatch.setattr(runtime, "_press", pressed.append)
    monkeypatch.setattr("core.desktop_messaging.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.desktop_messaging.os.name", "nt")

    result = runtime.send("카카오톡", "형택이", "테스트")

    assert typed == ["형택이", "테스트"]
    assert pressed == ["ENTER", "ENTER"]
    assert result["recipient"] == "형택이"
    assert result["delivery_receipt_verified"] is False


def test_desktop_message_runtime_blocks_wrong_recipient_window(monkeypatch):
    runtime = DesktopMessagingRuntime()
    main = WindowInfo(10, "카카오톡", 99, True, True, True)
    wrong = WindowInfo(11, "다른 사람", 99, True, True, True)

    class Automation:
        def list_windows(self):
            return [main, wrong]

        def focus(self, _handle):
            return main

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
    assert typed == ["형택이"]


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
