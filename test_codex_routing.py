"""Provider switching is exercised without a model server or authentication."""
from dataclasses import replace
import sys
from types import SimpleNamespace

import pytest

from core import auxiliary_models, llm
from core.model_registry import ModelProfile


@pytest.fixture
def routing(monkeypatch):
    values = {}
    settings = SimpleNamespace(get=lambda key: values.get(key, ""), set=values.__setitem__)
    monkeypatch.setattr(auxiliary_models, "get_assistant_settings", lambda: settings)
    monkeypatch.setattr(auxiliary_models, "_shutdown", False)
    monkeypatch.setattr(llm.Config.API_CONFIG, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(llm, "_llm_clients", {})
    monkeypatch.setattr(llm, "get_tools_schema", lambda: [])
    created, calls = [], []

    class Local(llm.OllamaClient):
        def __init__(self, role="default", cancellation_check=None):
            self.role, self.system_prompt, self.tools = role, "original", []
            self.profile = ModelProfile(role, "local-model", 0.1, 2048, "5m")
            self.model, self.base_url = "local-model", "http://127.0.0.1:11434"
            created.append(("local", role))

        def chat(self, messages, **kwargs):
            calls.append(("local", self.role, messages, kwargs))
            return "local"

        def chat_prose(self, messages, **kwargs):
            return self.chat(messages, **kwargs)

        def chat_structured(self, messages, json_schema=None, **kwargs):
            return self.chat(messages, json_schema=json_schema, **kwargs)

        def chat_with_tools(self, messages, allowed_tool_names=None):
            return self.chat(messages, allowed_tool_names=allowed_tool_names), []

    class Codex(llm.BaseLLMClient):
        chat_prose = Local.chat_prose
        chat_structured = Local.chat_structured
        chat_with_tools = Local.chat_with_tools

        def __init__(self, role="default", cancellation_check=None):
            self.role, self.system_prompt, self.tools = role, "GPT", []
            self.model = "gpt-test"
            self.profile = ModelProfile(role, self.model, 0.1, 2048, "0")
            self.failure = None
            self.cancellation_check = cancellation_check
            created.append(("codex", role))

        def chat(self, messages, **kwargs):
            calls.append(("codex", self.role, messages, kwargs))
            if self.failure:
                raise self.failure
            return "GPT"

    monkeypatch.setattr(llm, "OllamaClient", Local)
    runtime = SimpleNamespace(shutdown=lambda: calls.append(("shutdown",)))
    monkeypatch.setitem(sys.modules, "core.codex_client", SimpleNamespace(
        CodexClient=Codex, get_codex_runtime=lambda: runtime))
    return SimpleNamespace(created=created, calls=calls, Local=Local, Codex=Codex, values=values)


@pytest.mark.parametrize("role", ["default", "conversation", "reasoning", "planning", "code", "document", "vision"])
def test_retained_role_client_switches_gpt_on_and_off(routing, role):
    client = llm.get_llm_client(role)
    assert client.chat([]) == "local"
    auxiliary_models.configure("gpt", True)
    assert client.chat([]) == "GPT"
    assert routing.calls[-1][:2] == ("codex", role)
    assert client.model == "gpt-test"
    assert isinstance(client, routing.Codex) and not isinstance(client, routing.Local)
    auxiliary_models.configure("gpt", False)
    assert client.chat([]) == "local"
    assert isinstance(client, routing.Local)


def test_gpt_startup_is_lazy_and_does_not_construct_local(routing):
    auxiliary_models.configure("gpt", True)
    client = llm.get_llm_client("reasoning")
    assert routing.created == []
    client.set_system_prompt("custom")
    client.tools = [{"name": "read_file"}]
    assert client.chat_with_tools([], ["read_file"]) == ("GPT", [])
    assert client._codex.system_prompt == "custom"
    assert client._codex.tools == [{"name": "read_file"}]
    assert routing.created == [("codex", "reasoning")]


def test_gpt_failure_never_calls_local_fallback(routing):
    client = llm.get_llm_client("conversation")
    auxiliary_models.configure("gpt", True)
    client._active_client().failure = llm.ModelCallError("codex", "gpt-test", "authentication", "로그인 필요")
    with pytest.raises(llm.ModelCallError, match="로그인 필요") as error:
        client.chat([])
    assert error.value.user_message() == "GPT · Codex: 로그인 필요"
    assert [call[0] for call in routing.calls] == ["codex"]


def test_private_local_client_preserves_attributes_and_follows_switch(routing):
    first, second = llm.get_local_llm_client("document"), llm.get_local_llm_client("document")
    first.profile = replace(first.profile, keep_alive="0")
    assert first.profile.keep_alive == "0" and second.profile.keep_alive == "5m"
    auxiliary_models.configure("gpt", True)
    assert first.chat_structured([], json_schema={"type": "object"}, request_timeout=3) == "GPT"
    assert routing.calls[-1][3] == {"json_schema": {"type": "object"}, "request_timeout": 3}
    auxiliary_models.configure("default", True)
    assert first.chat([]) == "local" and first.profile.keep_alive == "0"


def test_gpt_code_and_answer_drafting_use_codex(routing):
    from core.executor import _local_answer_draft_client
    from core.answer_verification import _local_client
    auxiliary_models.configure("gpt", True)
    assert llm.get_coding_llm_client().chat([]) == "GPT"
    for requires_code in (True, False):
        assert _local_answer_draft_client(SimpleNamespace(requires_code=requires_code)).chat([]) == "GPT"
    assert _local_client("reasoning").chat([]) == "GPT"
    assert all(call[0] == "codex" for call in routing.calls)


def test_coding_cancellation_callback_reaches_codex(routing):
    auxiliary_models.configure("gpt", True)
    checkpoint = lambda: None
    client = llm.get_coding_llm_client(cancellation_check=checkpoint)
    client.chat([])
    assert client._codex.cancellation_check is checkpoint


def test_jev_preserves_gpt_and_shutdown_is_lazy(routing):
    auxiliary_models.configure("gpt", True)
    auxiliary_models.configure("jev", True)
    assert auxiliary_models.selection() == ("jev", True)
    assert llm.is_gpt_enabled()
    assert auxiliary_models.main_selection() == ("gpt", True)
    auxiliary_models.shutdown()
    assert routing.created == [] and routing.calls == [("shutdown",)]


def test_main_models_are_exclusive_and_default_returns_when_disabled(routing):
    assert auxiliary_models.main_selection() == ("default", True)
    assert auxiliary_models.is_enabled("default")
    auxiliary_models.configure("jev", True)
    for model in ("kimi_k3", "gpt", "default"):
        auxiliary_models.configure(model, True)
        assert auxiliary_models.main_selection() == (model, True)
        assert auxiliary_models.is_enabled("jev")
        assert sum(auxiliary_models.is_enabled(key) for key in ("default", "gpt", "kimi_k3")) == 1
    auxiliary_models.configure("gpt", True)
    auxiliary_models.configure("gpt", False)
    assert auxiliary_models.main_selection() == ("default", True)
    assert auxiliary_models.is_enabled("jev")


def test_legacy_last_selection_does_not_override_active_main_flag(routing):
    routing.values.update(auxiliary_model="jev", auxiliary_model_enabled_gpt="true",
                          auxiliary_model_enabled_jev="true")
    assert auxiliary_models.selection() == ("jev", True)
    assert auxiliary_models.main_selection() == ("gpt", True)
    assert llm.get_llm_client("conversation").chat([]) == "GPT"
    routing.values.update(auxiliary_model="kimi_k3", auxiliary_model_enabled_gpt="false",
                          auxiliary_model_enabled="true")
    assert auxiliary_models.main_selection() == ("kimi_k3", True)
    auxiliary_models.configure("default", True)
    assert auxiliary_models.main_selection() == ("default", True)
    assert routing.values["auxiliary_model_enabled"] == "false"


def test_jev_epoch_and_status_are_independent_from_main(routing):
    auxiliary_models.configure("gpt", True)
    main_generation, jev_generation = auxiliary_models.ticket(), auxiliary_models.ticket("jev")
    auxiliary_models.set_status("GPT 응답 중")
    auxiliary_models.configure("jev", True)
    auxiliary_models.set_status("Jev 분류 중", "jev")
    assert auxiliary_models.is_current(main_generation)
    assert not auxiliary_models.is_current(jev_generation, "jev")
    assert auxiliary_models.status() == "GPT 응답 중"
    assert auxiliary_models.status("jev") == "Jev 분류 중"
    jev_generation = auxiliary_models.ticket("jev")
    auxiliary_models.configure("default", True)
    assert auxiliary_models.is_current(jev_generation, "jev")
    assert not auxiliary_models.is_current(main_generation)
