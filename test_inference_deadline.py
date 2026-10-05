"""One elapsed-time budget across the model queue and semantic stages."""
import threading
import time

import pytest

from core import local_inference as inference
from core.llm import HybridLLMClient
from core.plugin import ToolCancelledError
from core.semantic_request import SemanticDecision, SemanticRequestInterpreter
from core.turn_context import TurnExecutionContext, bind_turn_context
from test_hybrid_llm import FakeClient
from test_semantic_request import _Model, _data, registry


@pytest.fixture
def clock(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(inference.time, "monotonic", lambda: now[0])
    return now


def test_nested_deadline_cannot_extend_parent_and_is_reset(clock):
    assert inference.current_inference_deadline() is None
    with inference.inference_deadline(2):
        assert inference.current_inference_deadline() == 12
        with inference.inference_deadline(50):
            assert inference.current_inference_deadline() == 12
        with pytest.raises(inference.InferenceDeadlineError):
            with inference.inference_deadline(.5):
                clock[0] = 10.5
        assert inference.current_inference_deadline() == 12
    assert inference.current_inference_deadline() is None


def test_expired_queue_does_not_enter_body_and_next_request_succeeds():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def hold():
        with inference.local_inference():
            entered.set()
            assert release.wait(3)

    @inference.serialized_inference
    def model(*, request_timeout=None):
        calls.append("called")
        return "result"

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        with pytest.raises(inference.InferenceDeadlineError):
            model(request_timeout=.02)
        assert time.monotonic() - started < .3
        assert calls == []
        assert inference.current_inference_deadline() is None
    finally:
        release.set()
        holder.join(3)
        assert not holder.is_alive()
    assert model(request_timeout=1) == "result" and calls == ["called"]


def test_late_lock_acquisition_releases_without_entering_body(clock, monkeypatch):
    released = []
    class LateLock:
        def acquire(self, *, timeout):
            assert timeout == pytest.approx(.005)
            clock[0] += .03
            return True
        def release(self):
            released.append(True)
    monkeypatch.setattr(inference, "_lock", LateLock())
    with pytest.raises(TimeoutError):
        with inference.local_inference(timeout=.005):
            pytest.fail("expired acquisition entered model body")
    assert released == [True]


@pytest.mark.parametrize("generator", [False, True])
def test_late_model_result_is_rejected_and_scope_restored(clock, generator):
    def result(*, request_timeout=None):
        clock[0] += 2
        return "late"
    def stream(*, request_timeout=None):
        clock[0] += 2
        yield "late"
    model = inference.serialized_inference(stream if generator else result)
    with pytest.raises(inference.InferenceDeadlineError):
        value = model(request_timeout=1)
        if generator:
            list(value)
    assert inference.current_inference_deadline() is None


def test_cancel_precedes_expiry_and_does_not_corrupt_next_scope(clock):
    context = TurnExecutionContext("deadline-cancel", "test")
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        with inference.inference_deadline(1):
            clock[0] += 2
            context.cancel()
            inference.check_inference_deadline()
    assert inference.current_inference_deadline() is None
    with inference.inference_deadline(1):
        inference.check_inference_deadline()


@pytest.mark.parametrize("method", ["chat", "chat_with_tools"])
@pytest.mark.parametrize("raises", [False, True])
def test_expired_primary_never_starts_provider_fallback(clock, method, raises):
    class Primary(FakeClient):
        def chat(self, messages):
            clock[0] += 2
            if raises:
                raise RuntimeError("late provider failure")
            return "late provider success"
        def chat_with_tools(self, messages, allowed=None):
            return self.chat(messages), []
    fallback = FakeClient(text="must not run")
    client = HybridLLMClient(primary=Primary(), fallback=fallback)
    with pytest.raises(inference.InferenceDeadlineError), inference.inference_deadline(1):
        getattr(client, method)([])
    assert fallback.calls == 0 and client.routing_stats["ollama_fallback"] == 0


@pytest.mark.parametrize("stage", ["discovery", "contract", "style", "recovery"])
def test_semantic_expiry_has_no_answer_or_execution_authority(clock, registry, monkeypatch, stage):
    model = _Model(_data())
    interpreter = SemanticRequestInterpreter(model, registry, classify_response_mode=stage == "style")
    interpreter.INTERPRETATION_TIMEOUT_SECONDS = 1

    def expire(*args, **kwargs):
        clock[0] += 2
        inference.check_inference_deadline()
    if stage == "discovery":
        interpreter.SINGLE_PASS_CATALOG_CHARS = 0
        monkeypatch.setattr(interpreter, "_discover_tools", expire)
    elif stage == "contract":
        monkeypatch.setattr(model, "chat", expire)
    else:
        decision = SemanticDecision("", relation="conversation", operation="conversation",
                                    grounded=stage == "style", source="model")
        monkeypatch.setattr(interpreter, "_interpret", lambda *a: decision)
        monkeypatch.setattr(interpreter, "_classify_response_mode" if stage == "style" else "_recover_dialogue", expire)
    result = interpreter.interpret("자료를 확인해줘")
    assert result.reason == "semantic_interpretation_failed:inference_deadline_exceeded"
    assert not result.grounded and not result.is_grounded_conversation
    assert not result.dialogue_response and not result.to_resolution(registry).ready
    assert inference.current_inference_deadline() is None


def test_interpretation_retries_share_one_budget(clock, registry):
    class SlowInvalid(_Model):
        def chat(self, messages):
            self.calls.append(messages)
            clock[0] += .6
            return "{}"
    model = SlowInvalid({})
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.INTERPRETATION_TIMEOUT_SECONDS = 1
    result = interpreter.interpret("자료를 확인해줘")
    assert len(model.calls) == 2
    assert result.reason.endswith(":inference_deadline_exceeded") and not result.grounded
