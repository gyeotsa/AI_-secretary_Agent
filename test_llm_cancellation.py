"""Cancelled model calls cannot dispatch, succeed, or start provider fallbacks."""
from types import SimpleNamespace

import pytest
import requests

from core.llm import AnthropicClient, HybridLLMClient, ModelCallError, OllamaClient
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context
from test_hybrid_llm import FakeClient


@pytest.mark.parametrize("method", ["chat", "chat_with_tools"])
def test_cancelled_before_dispatch_never_calls_ollama(monkeypatch, method):
    context = TurnExecutionContext("old", "session")
    client = OllamaClient()
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: pytest.fail("dispatch after cancel"))
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        context.cancel()
        getattr(client, method)([])


@pytest.mark.parametrize("method", ["chat", "chat_with_tools"])
@pytest.mark.parametrize("outcome", ["success", "timeout", "connection", "http400"])
def test_late_transport_result_remains_cancellation(monkeypatch, method, outcome):
    context = TurnExecutionContext("old", "session")
    client = OllamaClient()
    calls = []
    class Response:
        status_code = 400
        def raise_for_status(self):
            if outcome == "http400":
                raise requests.HTTPError(response=self)
        def json(self):
            return {"message": {"content": "stale"}, "error": "does not support tools"}
    def post(*args, **kwargs):
        calls.append(args)
        context.cancel()
        if outcome == "timeout":
            raise requests.Timeout("cancelled during transport")
        if outcome == "connection":
            raise requests.ConnectionError("cancelled during transport")
        return Response()
    monkeypatch.setattr("core.llm.post_json", post)
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        getattr(client, method)([])
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["chat", "chat_with_tools"])
@pytest.mark.parametrize("typed_error", [True, False])
def test_hybrid_cancellation_never_starts_fallback(method, typed_error):
    context = TurnExecutionContext("old", "session")
    class Primary(FakeClient):
        def chat(self, messages):
            context.cancel()
            if typed_error:
                raise ToolCancelledError("superseded")
            raise RuntimeError("provider failed after supersession")
        def chat_with_tools(self, messages, allowed=None):
            return self.chat(messages)
    fallback = FakeClient(text="must not run")
    client = HybridLLMClient(primary=Primary(), fallback=fallback)
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        getattr(client, method)([])
    assert fallback.calls == 0
    assert client.routing_stats["ollama_fallback"] == 0


@pytest.mark.parametrize("method", ["chat", "chat_with_tools"])
def test_anthropic_cancellation_is_not_wrapped(method):
    context = TurnExecutionContext("old", "session")
    def create(**kwargs):
        context.cancel()
        raise RuntimeError("provider ended after cancellation")
    client = object.__new__(AnthropicClient)
    client.system_prompt = ""
    client.tools = []
    client.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        getattr(client, method)([{"role": "user", "content": "hello"}])


def _offline_ollama(profile_limit=4096):
    from core.model_registry import ModelProfile
    client = object.__new__(OllamaClient)
    client.model = "test-only"
    client.base_url = "http://127.0.0.1:1"
    client.system_prompt = ""
    client.profile = ModelProfile("reasoning", "test-only", 0.1, profile_limit, "5m")
    return client


@pytest.mark.parametrize("profile_limit,expected", [(4096, 1536), (512, 512), (-1, 1536), (-2, 1536)])
def test_call_limits_tighten_profile_without_changing_defaults(monkeypatch, profile_limit, expected):
    calls = []
    client = _offline_ollama(profile_limit)
    profile = client.profile

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"message": {"content": "complete"}, "done": True}

    def post(url, *, json, timeout):
        calls.append((json, timeout))
        return Response()

    monkeypatch.setattr("core.llm.post_json", post)
    assert client.chat_structured([], {"type": "object"}, context_window=8192,
        request_timeout=4.5, max_output_tokens=1536) == "complete"
    assert calls[0][1] == 4.5
    assert calls[0][0]["options"]["num_predict"] == expected
    assert calls[0][0]["options"]["num_ctx"] == 8192
    assert calls[0][0]["format"] == {"type": "object"}
    assert client.chat([]) == "complete"
    assert client.chat_prose([]) == "complete"
    assert all(payload["options"]["num_predict"] == profile_limit and timeout == 120
               for payload, timeout in calls[1:])
    assert client.profile is profile and profile.max_tokens == profile_limit
    assert profile.keep_alive == "5m"


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "3", (1, 2), 10 ** 400])
def test_invalid_call_timeout_is_rejected_before_dispatch(monkeypatch, value):
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: pytest.fail("invalid timeout dispatched"))
    with pytest.raises(ValueError, match="positive finite number"):
        _offline_ollama().chat_structured([], request_timeout=value)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, float("nan"), float("inf"), "1536"])
def test_invalid_call_output_limit_is_rejected_before_dispatch(monkeypatch, value):
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: pytest.fail("invalid output limit dispatched"))
    with pytest.raises(ValueError, match="positive integer"):
        _offline_ollama().chat_structured([], max_output_tokens=value)


def test_cancelled_call_takes_precedence_over_invalid_limit(monkeypatch):
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: pytest.fail("cancelled call dispatched"))
    context = TurnExecutionContext("cancelled-limited-call", "test")
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        context.cancel()
        _offline_ollama().chat_structured([], request_timeout=0, max_output_tokens=0)


@pytest.mark.parametrize("cancel", [False, True])
def test_output_limit_truncation_never_becomes_completed_text(monkeypatch, cancel):
    context = TurnExecutionContext("limited-output", "test")

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"message": {"content": "partial"}, "done_reason": "length"}

    def post(*args, **kwargs):
        if cancel:
            context.cancel()
        return Response()

    monkeypatch.setattr("core.llm.post_json", post)
    with bind_turn_context(context), pytest.raises(ToolCancelledError if cancel else ModelCallError) as raised:
        _offline_ollama().chat_structured([], request_timeout=1, max_output_tokens=2)
    if not cancel:
        assert raised.value.code == "truncated_output"
