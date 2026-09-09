import pytest
import requests

from core.llm import ModelCallError, OllamaClient, has_configured_anthropic_key


class _Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"role": "assistant", "content": "ok"}}


@pytest.mark.parametrize(
    "value",
    ["", "your_anthropic_api_key_here", "replace_me", "changeme", "example-key"],
)
def test_placeholder_anthropic_keys_are_never_treated_as_configured(monkeypatch, value):
    monkeypatch.setattr("core.llm.Config.API_CONFIG.ANTHROPIC_API_KEY", value)
    assert has_configured_anthropic_key() is False


def test_ollama_chat_preserves_per_call_system_messages(monkeypatch):
    captured = {}

    def post(_url, json, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr("core.llm.requests.post", post)
    client = OllamaClient()
    client.model = "test-model"
    client.set_system_prompt("global instruction")

    assert client.chat([
        {"role": "system", "content": "planner instruction"},
        {"role": "user", "content": "make a plan"},
    ]) == "ok"
    assert captured["messages"] == [
        {"role": "system", "content": "planner instruction"},
        {"role": "user", "content": "make a plan"},
    ]


def test_ollama_structured_chat_sends_json_schema_format(monkeypatch):
    captured = {}

    def post(_url, json, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr("core.llm.requests.post", post)
    client = OllamaClient(); client.model = "test-model"
    schema = {"type": "object", "required": ["answer"],
              "properties": {"answer": {"type": "string"}}}
    assert client.chat_structured([{"role": "user", "content": "json"}], schema) == "ok"
    assert captured["format"] == schema


def test_structured_context_and_payload_unicode_are_preserved(monkeypatch):
    captured = {}
    literal = '{"message":"x = [1, 2] 🙂"}'
    class Response(_Response):
        def json(self): return {"message": {"content": literal}, "done": True}
    def post(_url, json, timeout):
        captured.update(json)
        return Response()
    monkeypatch.setattr("core.llm.requests.post", post)
    client = OllamaClient()
    assert client.chat_structured([{"role": "user", "content": literal}],
                                  {"type": "object"}, context_window=8192) == literal
    assert captured["options"]["num_ctx"] == 8192
    assert captured["messages"][-1]["content"] == literal


@pytest.mark.parametrize("context", [True, 1024, 65536, "8192"])
def test_invalid_context_does_not_call_model(monkeypatch, context):
    monkeypatch.setattr("core.llm.requests.post", lambda *a, **k: pytest.fail("must not call provider"))
    with pytest.raises(ModelCallError):
        OllamaClient().chat_structured([], context_window=context)


@pytest.mark.parametrize("flags", [{"done_reason": "length"}, {"done": False}])
def test_structured_truncation_is_not_accepted_as_model_text(monkeypatch, flags):
    class Response(_Response):
        def json(self): return {"message": {"content": '{"tool_names":[]}'}, **flags}
    monkeypatch.setattr("core.llm.requests.post", lambda *a, **k: Response())
    with pytest.raises(ModelCallError) as raised:
        OllamaClient().chat_structured([], {"type": "object"}, context_window=8192)
    assert raised.value.code == "truncated_output"


def test_saturated_context_does_not_authorize_structured_action(monkeypatch):
    class Response(_Response):
        def json(self): return {"message": {"content": '{"tool_names":["send"]}'}, "prompt_eval_count": 8192}
    monkeypatch.setattr("core.llm.requests.post", lambda *a, **k: Response())
    with pytest.raises(ModelCallError) as raised:
        OllamaClient().chat_structured([], {"type": "object"}, context_window=8192)
    assert raised.value.code == "context_saturated"


def test_ollama_tool_chat_preserves_per_call_system_message(monkeypatch):
    captured = {}

    def post(_url, json, timeout):
        captured.update(json)
        return _Response()

    monkeypatch.setattr("core.llm.requests.post", post)
    client = OllamaClient(); client.model = "test-model"
    client.set_system_prompt("global instruction")

    text, tools = client.chat_with_tools([
        {"role": "system", "content": "tool selector instruction"},
        {"role": "user", "content": "run it"},
    ], allowed_tool_names=[])

    assert text == "ok"
    assert tools == []
    assert captured["messages"] == [
        {"role": "system", "content": "tool selector instruction"},
        {"role": "user", "content": "run it"},
    ]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (requests.exceptions.ConnectionError("offline"), "connection"),
        (requests.exceptions.ReadTimeout("slow"), "timeout"),
    ],
)
def test_ollama_transport_failures_are_typed_not_model_text(
    monkeypatch, failure, expected_code,
):
    def post(_url, json, timeout):
        raise failure

    monkeypatch.setattr("core.llm.requests.post", post)
    client = OllamaClient(); client.model = "test-model"

    with pytest.raises(ModelCallError) as raised:
        client.chat([{"role": "user", "content": "hello"}])

    assert raised.value.code == expected_code
    assert raised.value.retryable is True
