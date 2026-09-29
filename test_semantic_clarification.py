"""Model-led information gathering; real validators/state, no external sends."""
from copy import deepcopy
import json

import pytest

from core.plugin import ToolCancelledError
from core.semantic_request import SemanticRequestInterpreter
from core.turn_context import TurnExecutionContext, bind_turn_context
from test_semantic_request import registry, _data
from test_semantic_executor_flow import make_executor, _model_output, SEND_TOOL, SESSION


class Model:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def chat_structured(self, messages, schema, **kwargs):
        self.prompts.append(deepcopy({**json.loads(messages[1]["content"]),
                                     "recent_dialogue": messages[2:-1],
                                     "current_user_input": messages[-1]["content"]}))
        assert self.outputs, "model reconsideration must be bounded"
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return json.dumps(output, ensure_ascii=False)


def send(**changes):
    return _data(operation="external_send", intent_name="messaging.send",
                 tool_names=["desktop_send_message"], **changes)


def test_rejected_recipient_is_reconsidered_without_becoming_user_evidence(registry):
    question = "카카오톡으로 전할 상대와 메시지 내용을 알려주실래요?"
    model = Model(
        send(slots={"provider": "kakaotalk", "recipient": "홍길동", "message": "안녕"}),
        send(slots={"provider": "kakaotalk"}, needs_clarification=True,
             clarification_question=question),
    )
    decision = SemanticRequestInterpreter(model, registry).interpret("카톡 보내줘")
    assert decision.grounded and decision.needs_clarification
    assert decision.clarification_question == question
    assert decision.slots == {"provider": "kakaotalk"}
    assert not decision.to_resolution(registry).ready
    feedback = model.prompts[1]["validation_feedback"]
    assert feedback["reason"] == "ungrounded_literal:recipient"
    assert model.prompts[1]["current_user_input"] == "카톡 보내줘"
    assert model.prompts[1]["recent_dialogue"] == []
    assert model.prompts[1]["pending_request"] == {}


@pytest.mark.parametrize("proposal,raw,fields,question", [
    (send(slots={"provider": "kakaotalk"}), "카톡 보내줘", ["message", "recipient"],
     "받으실 분과 전할 내용을 함께 말씀해 주세요."),
    (_data(slots={}), "파일을 읽어줘", ["filename"], "읽고 싶은 파일의 이름이나 경로가 무엇인가요?"),
    (_data(intent_name="", tool_names=["weather_lookup"], slots={}), "날씨 알아봐줘", ["location"],
     "어느 지역의 날씨를 알아볼까요?"),
])
def test_missing_fields_trigger_model_question_for_any_registered_tool(registry, proposal, raw, fields, question):
    model = Model(proposal, {"clarification_question": question})
    decision = SemanticRequestInterpreter(model, registry).interpret(raw)
    assert model.prompts[1]["missing_fields"] == fields
    assert model.prompts[1]["confirmed_slots"] == proposal["slots"]
    assert decision.clarification_question == question
    assert decision.needs_clarification and not decision.to_resolution(registry).ready


def test_repeated_bad_proposal_cannot_become_evidence_or_execute(registry):
    proposal = send(slots={"provider": "kakaotalk", "recipient": "홍길동", "message": "안녕"})
    model = Model(proposal, proposal)
    decision = SemanticRequestInterpreter(model, registry).interpret("카톡 보내줘")
    assert decision.reason == "ungrounded_literal:recipient"
    assert not decision.grounded and not decision.to_resolution(registry).ready
    assert len(model.prompts) == 2


def test_model_question_failure_does_not_fall_back_to_static_slot_question(registry):
    proposal = send(slots={"provider": "kakaotalk"})
    model = Model(proposal, proposal, proposal)
    decision = SemanticRequestInterpreter(model, registry).interpret("카톡 보내줘")
    assert decision.reason == "semantic_clarification_invalid"
    assert not decision.to_resolution(registry).ready
    assert not decision.clarification_question


def test_null_intake_fields_are_missing_not_executable_values(registry):
    question = "받으실 분과 전할 내용을 말씀해 주시겠어요?"
    proposal = send(slots={"provider": "kakaotalk", "recipient": None, "message": None},
                    clarification_question=question)
    model = Model(proposal)
    decision = SemanticRequestInterpreter(model, registry).interpret("카톡 보내줘")
    assert decision.grounded and decision.needs_clarification
    assert decision.slots == {"provider": "kakaotalk"}
    assert decision.clarification_question == question
    assert len(model.prompts) == 1 and not decision.to_resolution(registry).ready


def test_generated_question_cannot_change_confirmed_fields(registry):
    invalid_question = {"clarification_question": "어떤 내용인가요?", "slots": {"recipient": "홍길동"}}
    model = Model(send(slots={"provider": "kakaotalk", "recipient": "민수"}),
                  invalid_question, invalid_question)
    decision = SemanticRequestInterpreter(model, registry).interpret("민수에게 카톡 보내줘")
    assert decision.reason == "semantic_clarification_invalid"
    assert not decision.to_resolution(registry).ready


def test_nullable_optional_value_is_not_removed_from_tool_input(registry):
    contract = registry.get_capability("desktop_send_message")
    contract.input_schema["properties"]["note"] = {"type": ["string", "null"]}
    model = Model(send(slots={"provider": "kakaotalk", "recipient": "민수", "message": "안녕", "note": None}))
    result = SemanticRequestInterpreter(model, registry).interpret("민수에게 안녕이라고 카톡 보내줘")
    assert result.grounded and "note" in result.slots and result.slots["note"] is None
    assert result.to_resolution(registry).ready


def test_question_hidden_by_presenter_is_regenerated_by_model_without_changing_facts(registry):
    question = "민수님께 어떤 내용을 보내드릴까요?"
    model = Model(send(slots={"provider": "kakaotalk", "recipient": "민수"},
                       needs_clarification=True, clarification_question="请告诉我您想发送的内容。"),
                  {"clarification_question": "请告诉我您想发送的内容。"},
                  {"clarification_question": question})
    result = SemanticRequestInterpreter(model, registry).interpret("민수에게 카톡 보내줘")
    assert result.grounded and result.clarification_question == question
    assert result.slots == {"provider": "kakaotalk", "recipient": "민수"}
    assert model.prompts[1]["missing_fields"] == ["message"]
    assert not result.to_resolution(registry).ready


def test_explicit_unknown_correction_clears_old_value_without_losing_other_answers(registry):
    question = "받을 분을 정하시면 알려주시겠어요?"
    model = Model(send(relation="correct", slots={"recipient": None}, clarification_question=question))
    result = SemanticRequestInterpreter(model, registry).interpret("받는 사람은 아직 못 정했어", pending={
        "intent_name": "messaging.send", "original_request": "민수에게 안녕이라고 카톡 보내줘",
        "slots": {"provider": "kakaotalk", "recipient": "민수", "message": "안녕"},
    })
    assert result.grounded and result.slots == {"provider": "kakaotalk", "message": "안녕"}
    assert result.clarification_question == question and not result.to_resolution(registry).ready


def test_partial_answer_keeps_known_fields_and_asks_only_remaining_information(registry):
    question = "민수님께 전할 메시지 내용을 알려주세요."
    proposal = send(relation="continue", slots={"recipient": "민수", "message": None})
    model = Model(proposal, {"clarification_question": question})
    decision = SemanticRequestInterpreter(model, registry, classify_response_mode=True).interpret("민수", pending={
        "intent_name": "messaging.send", "slots": {"provider": "kakaotalk"},
        "original_request": "카톡 보내줘", "question": "받을 분과 내용을 알려주세요.",
    })
    assert decision.slots == {"provider": "kakaotalk", "recipient": "민수"}
    assert model.prompts[1]["missing_fields"] == ["message"]
    assert decision.clarification_question == question and decision.needs_clarification


@pytest.mark.parametrize("raw,proposal", [
    ("왜 계속 같은 질문을 해?", _data(relation="conversation", operation="conversation",
                                  tool_names=[], intent_name="", slots={})),
    ("부산 날씨 알아봐줘", _data(intent_name="", tool_names=["weather_lookup"], slots={"location": "부산"})),
])
def test_pending_question_does_not_force_chat_or_new_requests_into_slot_answers(registry, raw, proposal):
    model = Model(proposal)
    pending = {"intent_name": "messaging.send", "original_request": "카톡 보내줘",
               "slots": {"provider": "kakaotalk", "recipient": "민수"}, "question": "어떤 내용을 보낼까요?"}
    result = SemanticRequestInterpreter(model, registry, classify_response_mode=True).interpret(raw, pending=pending)
    assert result.grounded and result.relation == proposal["relation"]
    assert result.slots == proposal["slots"]
    assert pending["slots"] == {"provider": "kakaotalk", "recipient": "민수"}
    assert "available_tools" in model.prompts[0] and len(model.prompts) == 1


@pytest.mark.parametrize("slots", [{"recipient": [1]}, {"invented_field": None}])
def test_invalid_intake_schema_never_executes(registry, slots):
    proposal = send(slots=slots)
    model = Model(proposal, proposal)
    decision = SemanticRequestInterpreter(model, registry).interpret("카톡 보내줘")
    assert not decision.grounded and not decision.to_resolution(registry).ready


def test_cancellation_after_invalid_proposal_prevents_reconsideration(registry):
    context = TurnExecutionContext("clarification-turn", "session")
    class CancelModel(Model):
        def chat_structured(self, *args, **kwargs):
            response = super().chat_structured(*args, **kwargs)
            context.cancel()
            return response
    model = CancelModel(send(slots={"recipient": "홍길동"}))
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        SemanticRequestInterpreter(model, registry).interpret("카톡 보내줘")
    assert len(model.prompts) == 1


def test_model_questions_collect_partial_answers_across_restart_before_approval(make_executor):
    first_question = "카카오톡을 받을 분과 전하고 싶은 내용을 알려주세요."
    next_question = "김하이님께 어떤 메시지를 전하면 될까요?"
    first_model = Model(
        _model_output(slots={"provider": "kakaotalk", "recipient": "지어낸사람"}),
        _model_output(slots={"provider": "kakaotalk"}, needs_clarification=True,
                      clarification_question=first_question),
    )
    first_executor = make_executor(model=first_model)
    first = first_executor.execute_turn("카톡 보내줘", SESSION)
    assert first.status == "awaiting_user" and first.response == first_question
    assert first_executor.plan_calls == []
    assert first_executor.dialogue_state.get_intent_state(first.task_id)["slots"] == {"provider": "kakaotalk"}

    next_model = Model(_model_output(relation="continue", slots={"recipient": "김하이"},
                                    needs_clarification=True, clarification_question=next_question))
    next_executor = make_executor(model=next_model)
    second = next_executor.execute_turn("김하이", SESSION)
    assert second.status == "awaiting_user" and second.response == next_question
    assert second.task_id == first.task_id
    assert next_model.prompts[0]["pending_request"]["question"] == first_question
    state = next_executor.dialogue_state.get_intent_state(second.task_id)
    assert state["slots"] == {"provider": "kakaotalk", "recipient": "김하이"}
    assert next_executor.plan_calls == []

    body = "내일  학교에서 만나"
    final_model = Model(_model_output(relation="continue", slots={"message": body}))
    final_executor = make_executor(model=final_model)
    third = final_executor.execute_turn(body, SESSION)
    assert third.status == "awaiting_approval" and third.task_id == first.task_id
    step = final_executor.plan_calls[0].steps[0]
    assert step.tool_name == SEND_TOOL
    assert step.tool_input == {"provider": "kakaotalk", "recipient": "김하이", "message": body}
    assert final_executor.test_surface.calls == []
    assert len(final_executor.dialogue_state.list_tasks(SESSION)) == 1


@pytest.mark.integration
def test_live_model_led_information_collection(tmp_path, monkeypatch):
    """Opt-in actual Ollama + production catalog; tool execution is forbidden."""
    import os
    from types import SimpleNamespace
    from core.llm import OllamaClient
    from core.plugin import PluginRegistry
    from core.response_presenter import present_channels

    if os.getenv("JARVIS_RUN_LIVE_CLARIFICATION") != "1":
        pytest.skip("Set JARVIS_RUN_LIVE_CLARIFICATION=1 for local Ollama validation")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr("core.jev_client.classify_response_mode", lambda *_a, **_k: None)
    registry = PluginRegistry()
    registry.load_plugins_from_directory()
    assert not registry._load_failures
    registry.get_plugin("filesystem").workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No external action is permitted in an interpretation test")

    monkeypatch.setattr(registry, "execute_tool", forbidden)
    for plugin in registry.plugins.values():
        monkeypatch.setattr(plugin, "execute_tool", forbidden)
    client = OllamaClient.__new__(OllamaClient)
    client.base_url, client.model = "http://localhost:11434", "qwen2.5:7b-instruct"
    client.profile = SimpleNamespace(keep_alive="5m", temperature=.2, max_tokens=2048)
    client.system_prompt, client.role = "", "reasoning"
    original_call = client.chat_structured
    def capture(*args, **kwargs):
        result = original_call(*args, **kwargs)
        print(json.dumps({"model_output": result}, ensure_ascii=False), flush=True)
        return result
    client.chat_structured = capture
    interpreter = SemanticRequestInterpreter(client, registry, classify_response_mode=True)
    body = "내일 6시에 보자"
    provider = {"provider": "kakaotalk"}
    recipient = {**provider, "recipient": "민수"}
    complete = {**recipient, "message": body}
    for turns in [
        [("카톡 보내줘", provider), ("민수", recipient), (body, complete)],
        [("민수에게 카톡 보내줘", recipient), (body, complete)],
        [(f"카톡으로 '{body}'라고 보내줘", {**provider, "message": body}), ("민수", complete)],
    ]:
        history, pending = [], {}
        for index, (raw, expected) in enumerate(turns):
            decision = interpreter.interpret(raw, history=history, pending=pending)
            print(json.dumps({"input": raw, "decision": decision.__dict__}, ensure_ascii=False))
            assert decision.grounded, decision.reason
            assert decision.slots == expected
            finished = index == len(turns) - 1
            assert decision.to_resolution(registry).ready == finished
            if not finished:
                assert decision.needs_clarification and decision.clarification_question.strip()
                assert present_channels(decision.clarification_question, raw).screen_text == decision.clarification_question
                pending = {"task_id": "synthetic-only", "intent_name": decision.intent_name,
                           "original_request": turns[0][0], "slots": decision.slots,
                           "question": decision.clarification_question}
                history.extend([{"role": "user", "content": raw},
                                {"role": "assistant", "content": decision.clarification_question}])
