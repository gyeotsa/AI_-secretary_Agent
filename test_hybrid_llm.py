"""Role-based Claude/Ollama routing regression tests."""
from config import Config
from core import llm as llm_module
from core.llm import HybridLLMClient, OllamaClient


class FakeClient:
    def __init__(self, *, text="", tools=None, error=None):
        self.text = text
        self.tools = tools or []
        self.error = error
        self.system_prompt = ""
        self.calls = 0

    def set_system_prompt(self, prompt):
        self.system_prompt = prompt

    def chat(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text

    def chat_with_tools(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text, self.tools


def test_primary_tool_call_does_not_use_fallback():
    primary = FakeClient(tools=[{"name": "read_file", "input": {"path": "a"}}])
    fallback = FakeClient(text="local")
    client = HybridLLMClient(primary=primary, fallback=fallback)

    _, tools = client.chat_with_tools([{"role": "user", "content": "read"}])

    assert tools
    assert client.last_provider == "anthropic"
    assert primary.calls == 1
    assert fallback.calls == 0


def test_primary_error_response_falls_back_to_local():
    primary = FakeClient(text="오류가 발생했습니다: API unavailable")
    fallback = FakeClient(text="local response")
    client = HybridLLMClient(primary=primary, fallback=fallback)

    assert client.chat([]) == "local response"
    assert client.last_provider == "ollama"
    assert primary.calls == fallback.calls == 1
    assert client.get_routing_status()["anthropic_failure"] == 1
    assert client.get_routing_status()["ollama_fallback"] == 1


def test_empty_primary_response_falls_back_to_local():
    client = HybridLLMClient(primary=FakeClient(text=""), fallback=FakeClient(text="local"))
    assert client.chat([]) == "local"
    assert client.last_provider == "ollama"


def test_primary_exception_falls_back_and_preserves_prompt():
    primary = FakeClient(error=RuntimeError("timeout"))
    fallback = FakeClient(text="fallback")
    client = HybridLLMClient(primary=primary, fallback=fallback)
    client.set_system_prompt("reasoner prompt")

    assert client.chat([]) == "fallback"
    assert primary.system_prompt == fallback.system_prompt == "reasoner prompt"
    assert "timeout" in client.primary_unavailable_reason


def test_missing_api_key_uses_local_without_calling_anthropic():
    fallback = FakeClient(text="offline")
    client = HybridLLMClient(fallback=fallback, enable_configured_primary=False)

    assert client.chat([]) == "offline"
    assert client.primary is None
    assert client.last_provider == "ollama"


def test_factory_routes_only_configured_role_to_hybrid():
    original_provider = Config.API_CONFIG.LLM_PROVIDER
    original_roles = Config.API_CONFIG.HYBRID_CLAUDE_ROLES
    original_key = Config.API_CONFIG.ANTHROPIC_API_KEY
    try:
        Config.API_CONFIG.LLM_PROVIDER = "hybrid"
        Config.API_CONFIG.HYBRID_CLAUDE_ROLES = ["reasoning"]
        Config.API_CONFIG.ANTHROPIC_API_KEY = ""
        llm_module._llm_clients.clear()

        assert isinstance(llm_module.get_llm_client("reasoning"), HybridLLMClient)
        assert isinstance(llm_module.get_llm_client("default"), OllamaClient)
    finally:
        Config.API_CONFIG.LLM_PROVIDER = original_provider
        Config.API_CONFIG.HYBRID_CLAUDE_ROLES = original_roles
        Config.API_CONFIG.ANTHROPIC_API_KEY = original_key
        llm_module._llm_clients.clear()


def test_placeholder_api_key_is_treated_as_missing():
    original_key = Config.API_CONFIG.ANTHROPIC_API_KEY
    try:
        Config.API_CONFIG.ANTHROPIC_API_KEY = "your_anthropic_api_key_here"
        client = HybridLLMClient(fallback=FakeClient(text="local"))
        assert client.primary is None
        assert client.chat([]) == "local"
    finally:
        Config.API_CONFIG.ANTHROPIC_API_KEY = original_key
