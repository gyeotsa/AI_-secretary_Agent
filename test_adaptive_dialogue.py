"""Local model dialogue recovery/presentation never confers tool authority."""
import json

import pytest

from core.executor import ExecutionOutcome
from core.llm import OllamaClient
from core.plugin import ToolCancelledError
from core.response_realizer import ResponseRealizer
from core.semantic_request import SemanticDecision, SemanticRequestInterpreter
from test_semantic_clarification import Model
from test_semantic_executor_flow import make_executor, _model_output, _pending_send, SEND_TOOL, SESSION


SEND_DISCOVERY = {"request_kind": "action", "tool_names": [SEND_TOOL], "confidence": .99}


class LocalModel(Model, OllamaClient):
    base_url = "http://localhost:11434"

    def chat_prose(self, messages, **kwargs):
        self.prompts.append(messages)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_rejected_action_recovers_question_then_collects_reply_before_approval(make_executor):
    question = "받을 분과 전할 내용을 알려주실래요?"
    bad = _model_output(slots={"provider": "kakaotalk", "recipient": "없는사람", "message": "가짜본문"})
    model = LocalModel(SEND_DISCOVERY, bad, bad, {"relation": "new", "needs_clarification": True, "response": question})
    executor = make_executor(model=model)
    executor.semantic_interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    first = executor.execute_turn("카톡 보내줘", SESSION)
    assert first.status == "awaiting_user" and first.response == question
    assert first.response_generated and executor.plan_calls == []
    assert "없는사람" not in json.dumps(model.prompts[-1], ensure_ascii=False)
    assert executor.dialogue_state.get_intent_state(first.task_id) is None

    restarted = make_executor(model=LocalModel(SEND_DISCOVERY, _model_output(
        relation="continue", slots={"provider": "kakaotalk", "recipient": "민수", "message": "내일  봐"})))
    restarted.semantic_interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    final = restarted.execute_turn('민수에게 "내일  봐"라고 보내줘', SESSION)
    assert final.task_id == first.task_id and final.status == "awaiting_approval"
    assert restarted.plan_calls[0].steps[0].tool_input["message"] == "내일  봐"
    assert restarted.test_surface.calls == []


def test_recovery_keeps_confirmed_pending_state_and_only_asks_remaining_info(make_executor):
    bad = _model_output(relation="continue", slots={"recipient": "없는사람", "message": "가짜"})
    model = LocalModel(SEND_DISCOVERY, bad, bad, {"relation": "continue", "needs_clarification": True,
                                "response": "김하이님께 전할 내용은 무엇인가요?"})
    executor = make_executor(model=model)
    executor.semantic_interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    pending = _pending_send(executor)
    before = executor.dialogue_state.get_intent_state(pending.task_id)
    outcome = executor.execute_turn("내용은 아직 생각 중이야", SESSION)
    assert outcome.task_id == pending.task_id and outcome.status == "awaiting_user"
    assert executor.dialogue_state.get_intent_state(pending.task_id) == before
    assert model.prompts[-1]["pending_request"]["slots"]["recipient"] == "김하이"
    assert executor.plan_calls == []


@pytest.mark.parametrize("reply", ["카톡을 보냈습니다.", "지금 전송 중입니다."])
def test_recovery_rejects_false_completion_and_activity(make_executor, reply):
    bad = _model_output(slots={"recipient": "없는사람"})
    model = LocalModel(SEND_DISCOVERY, bad, bad, {
        "relation": "new", "needs_clarification": False, "response": reply})
    executor = make_executor(model=model)
    executor.semantic_interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    outcome = executor.execute_turn("카톡 보내줘", SESSION)
    assert outcome.status == "failed" and outcome.response != reply
    assert executor.plan_calls == []
    assert model.outputs == [] and model.prompts[-1]["executed_tools"] == []


def test_full_information_recovery_does_not_require_an_invented_missing_field(make_executor):
    reply = "입력하신 대상과 내용은 확인했지만 실행 명세 검증에 문제가 있어 전송하지 않았어요."
    bad = _model_output(slots={"recipient": "없는사람"})
    executor = make_executor(model=LocalModel(SEND_DISCOVERY, bad, bad, {
        "relation": "new", "needs_clarification": False, "response": reply}))
    executor.semantic_interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    outcome = executor.execute_turn('민수에게 "안녕"이라고 카톡 보내줘', SESSION)
    assert outcome.response == reply and outcome.status == "failed"
    assert not executor.dialogue_state.list(SESSION) and not executor.plan_calls


@pytest.mark.parametrize("asks_again", [False, True])
def test_unsupported_recovery_cannot_invent_missing_information(make_executor, asks_again):
    model = LocalModel({"request_kind": "unsupported", "tool_names": [], "confidence": .7}, {
        "relation": "new", "needs_clarification": asks_again,
        "response": "현재 등록된 도구로는 해당 작업을 수행할 수 없어요."})
    executor = make_executor(model=model)
    executor.semantic_interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    decision = executor.semantic_interpreter.interpret("지원하지 않는 외부 장치를 직접 조종해줘")
    assert decision.reason == "no_supported_tool" and decision.confidence == .7
    assert not decision.needs_clarification and not decision.clarification_question
    assert not decision.grounded and not decision.to_resolution(executor.semantic_interpreter.registry).ready
    assert bool(decision.dialogue_response) is not asks_again
    assert not executor.plan_calls and model.outputs == []


@pytest.mark.parametrize("status,canonical,generated", [
    ("failed", "검증 오류. 실행된 도구 없음.", "실행 명세가 검증되지 않아 전송하지 않았어요."),
    ("awaiting_user", "필수 입력값 확인 필요", "기존 내용을 수정할까요, 새 문서를 만들까요?"),
    ("cancelled", "사용자 취소", "요청하신 대로 남은 작업을 중단했어요."),
    ("completed", "등록된 작업이 없습니다.", "지금 등록된 작업은 없어요."),
])
def test_common_renderer_covers_runtime_outcomes_and_persists_questions(make_executor, status, canonical, generated):
    executor = make_executor()
    model = LocalModel(generated)
    executor.response_realizer = ResponseRealizer(model)
    task = executor.dialogue_state.create_task(SESSION, "원래 요청", workspace_path=executor._workspace_scope())
    if status == "awaiting_user":
        executor.dialogue_state.create(SESSION, "원래 요청", canonical, [], task.task_id, executor._workspace_scope())
    outcome = ExecutionOutcome(canonical, status, "원래 요청", task_id=task.task_id)
    rendered = executor.render_outcome(outcome, "현재 요청", [{"role": "user", "content": "앞선 답변"}], SESSION)
    assert rendered.status == status and rendered.response == generated
    assert executor.render_outcome(rendered, "현재 요청", session_id=SESSION) is rendered
    assert len(model.prompts) == 1
    assert executor.dialogue_state.get_task(SESSION, task.task_id).result == generated
    if status == "awaiting_user":
        assert executor.dialogue_state.get(SESSION, task.task_id).question == generated


def test_renderer_checks_approval_body_exactly_and_never_claims_sending():
    kwargs = dict(tool_name="send", user_request="카톡 보내줘", assistant_name="비서", address="",
                  status="awaiting_approval", required_facts=["민수", "내일  봐"])
    canonical = '민수에게 "내일  봐" 전송 승인 필요'
    for wrong in ['민수에게 "내일 봐" 전송 승인할까요?', '민수에게 "내일  봐" 카톡을 보냈습니다. 승인됐어요.']:
        assert ResponseRealizer(LocalModel(wrong)).realize(canonical, **kwargs).startswith("[시스템 상태:")
    reply = '민수에게 "내일  봐"라고 보낼까요? 전송하려면 승인이라고 답해 주세요.'
    assert ResponseRealizer(LocalModel(reply)).realize(canonical, **kwargs) == reply


def test_renderer_reconsiders_missing_facts_without_using_a_template():
    model = LocalModel("승인해 주세요.", '민수에게 "내일  봐"라고 보낼까요? 승인이라고 답해 주세요.')
    result = ResponseRealizer(model).realize(
        '대상: 민수\n내용: 내일  봐\n계속하려면 ‘승인’이라고 말씀해 주세요.',
        tool_name="", user_request="카톡 보내줘", assistant_name="비서", address="",
        status="awaiting_approval", required_facts=["민수", "내일  봐"],
    )
    assert result == '민수에게 "내일  봐"라고 보낼까요? 승인이라고 답해 주세요.'
    assert len(model.prompts) == 2


def test_local_only_and_cancellation_contract():
    kwargs = dict(tool_name="", user_request="상태", assistant_name="비서", address="", status="failed")
    remote = LocalModel("should not run")
    remote.base_url = "https://example.com"
    assert ResponseRealizer(remote).realize("진단 정보", **kwargs).startswith("[시스템 상태:")
    assert remote.prompts == []
    with pytest.raises(ToolCancelledError):
        ResponseRealizer(LocalModel(ToolCancelledError("cancel"))).realize("진단 정보", **kwargs)


def test_cancelled_wording_cannot_erase_verified_tool_result(make_executor):
    from core.tool_result import ToolRunResult, Evidence
    executor = make_executor()
    executor.response_realizer = ResponseRealizer(LocalModel(ToolCancelledError("cancel")))
    result = ToolRunResult.successful(tool_name="create", raw_output="파일 생성 완료",
                                     evidence=[Evidence("test", "verified")])
    outcome = ExecutionOutcome("파일 생성 완료", tool_result=result)
    assert executor.render_outcome(outcome, "파일 생성", session_id=SESSION) is outcome
    assert outcome.status == "completed" and outcome.tool_result is result


@pytest.mark.integration
def test_live_executor_intake_with_production_shortlist(make_executor, tmp_path, monkeypatch):
    """Real Ollama through the GUI's Executor boundary; every send is forbidden."""
    import os
    from types import SimpleNamespace
    from core.intent_router import IntentRouter
    from core.plugin import PluginRegistry
    from core.tool_loadout import ToolLoadoutSelector

    if os.getenv("JARVIS_RUN_LIVE_CLARIFICATION") != "1":
        pytest.skip("Opt in to local Ollama; no external actions")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("core.jev_client.classify_response_mode", lambda *_a, **_k: None)
    registry = PluginRegistry()
    registry.load_plugins_from_directory()
    assert not registry._load_failures
    registry.get_plugin("filesystem").workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))
    def forbidden(*_a, **_k):
        raise AssertionError("Real tools must never execute in this test")
    monkeypatch.setattr(registry, "execute_tool", forbidden)
    for plugin in registry.plugins.values():
        monkeypatch.setattr(plugin, "execute_tool", forbidden)
    client = OllamaClient.__new__(OllamaClient)
    client.base_url, client.model = "http://localhost:11434", "qwen2.5:7b-instruct"
    client.profile = SimpleNamespace(keep_alive="5m", temperature=.2, max_tokens=2048)
    client.system_prompt, client.role = "", "reasoning"
    original_structured = client.chat_structured
    def capture_structured(*args, **kwargs):
        result = original_structured(*args, **kwargs)
        print(json.dumps({"model_decision": str(result)}, ensure_ascii=False), flush=True)
        return result
    client.chat_structured = capture_structured
    original_prose = client.chat_prose
    def capture_prose(*args, **kwargs):
        result = original_prose(*args, **kwargs)
        print(json.dumps({"generated_prose": str(result)}, ensure_ascii=False), flush=True)
        return result
    client.chat_prose = capture_prose
    executor = make_executor()
    executor.intent_router = IntentRouter(registry)
    executor.semantic_interpreter = SemanticRequestInterpreter(client, registry, classify_response_mode=True)
    executor.tool_loadout = ToolLoadoutSelector(registry)
    executor.response_realizer = ResponseRealizer(client)
    # Repeated error replies polluted the user's real session. Include that
    # context, not only a pristine single-request interpreter demonstration.
    history = [
        {"role": "user", "content": "카톡 보내줘"},
        {"role": "assistant", "content": "요청을 해석하는 과정에서 모델의 분류 결과를 검증하지 못했어. 설명이 부족하다는 뜻은 아니며, 어떤 작업도 실행하지 않았습니다."},
    ]
    for raw in ["카톡 보내줘", "민수", "내일 6시에 보자"]:
        outcome = executor.execute_turn(raw, SESSION, history)
        print(json.dumps({"input": raw, "status": outcome.status, "response": outcome.response}, ensure_ascii=False), flush=True)
        assert outcome.status == ("awaiting_approval" if raw == "내일 6시에 보자" else "awaiting_user")
        assert outcome.response and not outcome.response.startswith("[시스템 상태:")
        history.extend([{"role": "user", "content": raw}, {"role": "assistant", "content": outcome.response}])
    assert len(executor.plan_calls) == 1
    assert executor.plan_calls[0].steps[0].tool_input == {
        "provider": "kakaotalk", "recipient": "민수", "message": "내일 6시에 보자"}
    recovered = executor.semantic_interpreter._recover_dialogue(
        "카톡 보내줘", history[:2], {}, ["desktop_send_message"],
        SemanticDecision("카톡 보내줘", reason="ungrounded_literal:recipient"),
    )
    print(json.dumps({"recovered_question": recovered.clarification_question}, ensure_ascii=False), flush=True)
    assert recovered.source == "dialogue_recovery" and recovered.needs_clarification
    assert not recovered.grounded and not recovered.tool_names and not recovered.slots
    assert recovered.clarification_question and "검증하지 못했" not in recovered.clarification_question
