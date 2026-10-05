import pytest

from config import APIConfig
from core import llm as llm_module
from core.model_registry import ModelRegistry, ModelRoleRouter


@pytest.mark.parametrize("configured", [None, "http://localhost:11434", "https://model.example:8443/base"])
def test_ollama_default_avoids_ipv6_fallback_without_rewriting_explicit_urls(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_BASE_URL", configured)
    assert APIConfig().OLLAMA_BASE_URL == (configured or "http://127.0.0.1:11434")


def test_model_registry_assigns_specialized_models():
    registry = ModelRegistry()

    assert registry.resolve("conversation").model == "qwen2.5:7b-instruct"
    assert registry.resolve("planning").model == "qwen2.5:7b-instruct"
    assert registry.resolve("code").model == "qwen2.5-coder:7b-instruct"
    assert registry.resolve("vision").model == "gemma3:4b"
    assert registry.resolve("vision").modalities == ("text", "image")


def test_llm_factory_uses_role_profile():
    llm_module._llm_clients.clear()
    try:
        assert llm_module.get_llm_client("conversation").model == "qwen2.5:7b-instruct"
        assert llm_module.get_llm_client("code").model == "qwen2.5-coder:7b-instruct"
        assert llm_module.get_llm_client("vision").model == "gemma3:4b"
    finally:
        llm_module._llm_clients.clear()


def test_role_router_uses_structured_tool_capabilities():
    router = ModelRoleRouter()

    assert router.route(conversational=True) == "conversation"
    assert router.route(modalities=["text", "image"]) == "vision"
    assert router.route(allowed_tools=["git_status", "run_command"]) == "code"
    assert router.route(allowed_tools=["excel_create_workbook"]) == "document"
    assert router.route(allowed_tools=["get_weather"]) == "tool_selection"
    assert router.route() == "reasoning"
