"""Partial human conversation is explicit; machine-facing output stays strict."""
from types import SimpleNamespace

import pytest
import requests

from core.agent_services import ConversationResponse, ConversationService, guard_conversation_response
from core.llm import (
    AnthropicClient, BaseLLMClient, HybridLLMClient, ModelCallError,
    OllamaClient, PROSE_TRUNCATION_NOTICE, ProseResponse,
)
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context


class Response:
    def __init__(self, content="재귀는 자기 자신을 호출하는 방식이야. 🙂", **flags):
        self.payload = {"message": {"content": content}, **flags}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.fixture(autouse=True)
def isolated_style_learning(monkeypatch):
    monkeypatch.setattr(
        "core.style_learning.get_style_learning_store",
        lambda: SimpleNamespace(effective_directive=lambda: ""),
    )


@pytest.mark.parametrize("flags,reason", [
    ({"done": True, "done_reason": "length"}, "length"),
    ({"done": False}, "incomplete"),
])
def test_prose_returns_partial_with_notice_and_bounded_single_request(monkeypatch, flags, reason):
    calls = []
    content = '재귀의 첫 예시야.\n```python\ndef factorial(n):\n    return n *'

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(content, **flags)

    monkeypatch.setattr("core.llm.post_json", post)
    client = OllamaClient()
    client.set_system_prompt("global")
    messages = [{"role": "system", "content": "conversation only"},
                {"role": "user", "content": "설명해 줘", "images": ["test-image"]}]
    result = client.chat_prose(messages)

    assert isinstance(result, str) and isinstance(result, ProseResponse)
    assert result.content == content
    assert result == PROSE_TRUNCATION_NOTICE + "\n\n" + content
    assert result.truncated and result.finish_reason == reason
    assert result.provider == "ollama" and result.model == client.model
    assert len(calls) == 1
    payload = calls[0][1]["json"]
    assert payload["messages"] == messages
    assert payload["options"]["num_predict"] == client.profile.max_tokens
    assert payload["stream"] is False
    assert "tools" not in payload and "format" not in payload


def test_complete_prose_does_not_gain_notice_or_lose_unicode(monkeypatch):
    content = '설명 🙂\n```python\nprint("안녕")\n```'
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: Response(
        content, done=True, done_reason="stop",
    ))
    result = OllamaClient().chat_prose([])
    assert result == result.content == content
    assert not result.truncated and result.finish_reason == "stop"


@pytest.mark.parametrize("method", ["chat", "chat_structured", "chat_with_tools"])
@pytest.mark.parametrize("flags", [{"done_reason": "length"}, {"done": False}])
def test_machine_facing_apis_reject_partial_even_if_json_is_parseable(monkeypatch, method, flags):
    response = Response('{"tool_names": ["send_message"]}', **flags)
    response.payload["message"]["tool_calls"] = [
        {"function": {"name": "send_message", "arguments": {"text": "not authorized"}}},
    ]
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: response)
    with pytest.raises(ModelCallError) as raised:
        getattr(OllamaClient(), method)([])
    assert raised.value.code == "truncated_output"
    assert not raised.value.retryable


@pytest.mark.parametrize("failure,code", [
    (requests.Timeout("too slow"), "timeout"),
    (requests.ConnectionError("offline"), "connection"),
    (ValueError("invalid provider JSON"), "protocol"),
])
def test_prose_does_not_turn_real_provider_failure_into_text(monkeypatch, failure, code):
    def post(*args, **kwargs):
        raise failure
    monkeypatch.setattr("core.llm.post_json", post)
    with pytest.raises(ModelCallError) as raised:
        OllamaClient().chat_prose([])
    assert raised.value.code == code


@pytest.mark.parametrize("when", ["before", "after_transport", "after_decode"])
def test_cancelled_prose_never_returns_partial(monkeypatch, when):
    context = TurnExecutionContext("cancel-prose", "session")
    calls = []

    class CancelResponse(Response):
        def json(self):
            if when == "after_decode":
                context.cancel()
            return super().json()

    def post(*args, **kwargs):
        calls.append(args)
        if when == "after_transport":
            context.cancel()
        return CancelResponse(done_reason="length")

    monkeypatch.setattr("core.llm.post_json", post)
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        if when == "before":
            context.cancel()
        OllamaClient().chat_prose([])
    assert len(calls) == (0 if when == "before" else 1)


class ProseClient:
    def __init__(self, response, *, repair=None):
        self.response = response
        self.repair = repair
        self.prose_calls = []
        self.strict_calls = []

    def set_system_prompt(self, prompt):
        pass

    def chat_prose(self, messages):
        self.prose_calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def chat(self, messages):
        self.strict_calls.append(messages)
        if isinstance(self.repair, Exception):
            raise self.repair
        return self.repair


def test_conversation_retains_partial_and_metadata_without_optional_model_repair():
    content = '반갑습니다. 재귀를 설명할게.\n```python\n# 원문 🙂\ndef recurse(n):\n    return'
    llm = ProseClient(ProseResponse(content, truncated=True, finish_reason="length",
                                    provider="ollama", model="test-model"),
                      repair="잘못된 완성 답변")
    result = ConversationService(llm).respond("재귀 예제 10개를 자세히 설명해 줘", [], style="반말")
    assert isinstance(result, ConversationResponse)
    assert result.truncated and result.finish_reason == "length"
    assert result.provider == "ollama" and result.model == "test-model"
    assert result.count(PROSE_TRUNCATION_NOTICE) == 1
    assert result.startswith(PROSE_TRUNCATION_NOTICE)
    assert '```python\n# 원문 🙂\ndef recurse(n):\n    return' in result
    assert len(llm.prose_calls) == 1 and not llm.strict_calls


def test_notice_is_reapplied_after_local_polish(monkeypatch):
    llm = ProseClient(ProseResponse("초안", truncated=True, finish_reason="length"))
    monkeypatch.setattr("core.agent_services.light_polish_korean", lambda value: "다듬은 내용")
    result = ConversationService(llm).respond("설명해 줘", [])
    assert result == PROSE_TRUNCATION_NOTICE + "\n\n다듬은 내용"
    assert result.truncated


def test_empty_partial_is_not_replaced_with_a_complete_greeting():
    result = ConversationService(ProseClient(ProseResponse(
        "", truncated=True, finish_reason="length",
    ))).respond("길게 설명해 줘", [])
    assert result == PROSE_TRUNCATION_NOTICE
    assert result.truncated
    assert "듣고 있어" not in result


@pytest.mark.parametrize("repair", [
    ModelCallError("ollama", "test", "truncated_output", "partial"),
    ProseResponse("잘린 수정본", truncated=True, finish_reason="length"),
])
def test_truncated_optional_repair_keeps_complete_original_draft(repair):
    llm = ProseClient(ProseResponse("반갑습니다."), repair=repair)
    result = ConversationService(llm).respond("같이 얘기할까?", [], style="반말")
    assert result == "반갑습니다."
    assert not result.truncated
    assert len(llm.prose_calls) == len(llm.strict_calls) == 1


def test_completion_guard_preserves_truncation_and_still_blocks_false_claims():
    reply = ProseResponse("파일을 저장했어.", truncated=True, finish_reason="length")
    result = guard_conversation_response(reply, "파일을 만들어 줘")
    assert result.unverified_completion and result.truncated
    assert "도구 실행 증거가 없으므로" in result
    assert result.count(PROSE_TRUNCATION_NOTICE) == 1
    assert guard_conversation_response(result, "파일") is result


def test_partial_metadata_is_owned_by_each_response(monkeypatch):
    responses = iter([Response("첫 답변", done_reason="length"),
                      Response("둘째 답변", done=True, done_reason="stop")])
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: next(responses))
    service = ConversationService(OllamaClient())
    partial = service.respond("첫 질문", [])
    complete = service.respond("둘째 질문", [])
    assert partial.truncated and not complete.truncated
    assert partial.finish_reason == "length" and complete.finish_reason == "stop"
    assert PROSE_TRUNCATION_NOTICE not in complete


def test_hybrid_preserves_primary_partial_without_starting_fallback():
    partial = ProseResponse("부분 답변", truncated=True, finish_reason="length")
    primary = ProseClient(partial)
    fallback = ProseClient("fallback")
    hybrid = HybridLLMClient(primary=primary, fallback=fallback)
    assert hybrid.chat_prose([]) is partial
    assert len(primary.prose_calls) == 1
    assert not fallback.prose_calls and not primary.strict_calls
    assert hybrid.routing_stats["anthropic_success"] == 0
    assert hybrid.routing_stats["anthropic_partial"] == 1


def test_hybrid_uses_prose_api_on_fallback():
    partial = ProseResponse("부분 답변", truncated=True, finish_reason="length")
    primary = ProseClient(ModelCallError("anthropic", "test", "connection", "offline"))
    fallback = ProseClient(partial)
    hybrid = HybridLLMClient(primary=primary, fallback=fallback)
    assert hybrid.chat_prose([]) is partial
    assert len(primary.prose_calls) == len(fallback.prose_calls) == 1
    assert not primary.strict_calls and not fallback.strict_calls


def test_hybrid_cancellation_does_not_start_prose_fallback():
    context = TurnExecutionContext("hybrid-prose-cancel", "s")

    class Primary(ProseClient):
        def chat_prose(self, messages):
            context.cancel()
            raise RuntimeError("transport ended after cancellation")

    fallback = ProseClient("must not run")
    hybrid = HybridLLMClient(primary=Primary(""), fallback=fallback)
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        hybrid.chat_prose([])
    assert not fallback.prose_calls and not fallback.strict_calls


def anthropic_client(stop_reason):
    client = object.__new__(AnthropicClient)
    client.system_prompt = ""
    client.tools = []
    response = SimpleNamespace(stop_reason=stop_reason, content=[
        SimpleNamespace(type="text", text="생성된 답변"),
    ])
    client.client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))
    return client


def test_anthropic_prose_exposes_max_tokens_as_partial():
    result = anthropic_client("max_tokens").chat_prose([])
    assert result.truncated and result.finish_reason == "max_tokens"
    assert result.content == "생성된 답변" and PROSE_TRUNCATION_NOTICE in result


@pytest.mark.parametrize("blocks,expected", [
    ([], ""),
    ([SimpleNamespace(type="thinking", thinking="not prose"),
      SimpleNamespace(type="text", text="첫 부분"),
      SimpleNamespace(type="text", text="다음 부분")], "첫 부분\n다음 부분"),
])
def test_anthropic_partial_preserves_all_prose_blocks_and_handles_empty(blocks, expected):
    client = anthropic_client("max_tokens")
    client.client.messages.create = lambda **kwargs: SimpleNamespace(
        stop_reason="max_tokens", content=blocks,
    )
    result = client.chat_prose([])
    assert result.truncated and result.content == expected
    assert result.startswith(PROSE_TRUNCATION_NOTICE)


def test_partial_model_metric_is_not_recorded_as_complete(monkeypatch):
    increments = []
    monkeypatch.setattr("core.llm.post_json", lambda *a, **k: Response(done_reason="length"))
    monkeypatch.setattr("core.productization.METRICS.increment", increments.append)
    monkeypatch.setattr("core.productization.METRICS.observe", lambda *args: None)
    client = OllamaClient()
    client.chat_prose([])
    assert increments == [f"model.{client.model}.partial"]


@pytest.mark.parametrize("method", ["chat", "chat_with_tools"])
def test_anthropic_machine_output_rejects_truncation(method):
    with pytest.raises(ModelCallError) as raised:
        getattr(anthropic_client("max_tokens"), method)([])
    assert raised.value.code == "truncated_output"


def test_legacy_base_client_remains_compatible_with_prose_service():
    class LegacyClient(BaseLLMClient):
        def chat(self, messages):
            return "완전한 답변"
    result = ConversationService(LegacyClient()).respond("설명해 줘", [])
    assert result == "완전한 답변" and not result.truncated
