"""Cancelled model calls cannot dispatch, succeed, or start provider fallbacks."""
from types import SimpleNamespace

import pytest
import requests

from core.llm import AnthropicClient, HybridLLMClient, OllamaClient
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
