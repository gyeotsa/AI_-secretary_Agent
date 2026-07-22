from core.llm import OllamaClient


class _Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"response": "ok"}


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
    assert "global instruction" in captured["prompt"]
    assert "System: planner instruction" in captured["prompt"]
    assert "User: make a plan" in captured["prompt"]
