"""Browser mail discovery and routing without accounts, browsers or model calls."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.intent_router import IntentRouter
from core.plugin import BasePlugin, PluginRegistry, ToolSchema
from core.semantic_request import SemanticRequestInterpreter
from plugins.browser_mail import BrowserMailPlugin
from plugins.mail import MailPlugin


@pytest.fixture
def mail_registry():
    registry = PluginRegistry()
    registry.register_plugin(BrowserMailPlugin())
    registry.register_plugin(MailPlugin())
    yield registry
    registry.shutdown()


def test_dynamic_loader_discovers_browser_mail_beside_legacy_mail(monkeypatch):
    directory = Path(__file__).resolve().parent / "plugins"
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: (
        iter((directory / "browser_mail.py", directory / "mail.py"))
        if path == directory else original(path)))
    registry = PluginRegistry()
    try:
        registry.load_plugins_from_directory(str(directory))
        assert isinstance(registry.get_plugin("browser_mail"), BrowserMailPlugin)
        assert isinstance(registry.get_plugin("mail"), MailPlugin)
        assert not registry._load_failures
        assert not registry.validate_contracts()
        assert len(registry.get_all_tools()) == 8
        assert [intent.name for _, intent in registry.get_all_intents()] == ["mail.brief"]
    finally:
        registry.shutdown()


@pytest.mark.parametrize("text, slots", [
    ("네이버 메일 현황 알려줘", {"providers": ["naver"]}),
    ("새 메일 건수 알려줘", {}),
    ("네이버와 구글 메일 요약해줘", {"providers": ["naver", "gmail"]}),
])
def test_korean_mail_count_request_routes_to_shared_collector(mail_registry, text, slots):
    result = IntentRouter(mail_registry).resolve(text)
    assert result.ready and result.execution_requested
    assert result.intent_name == "mail.brief"
    assert result.tool_name == "browser_mail_collect_summary"
    assert result.slots == slots
    assert result.request_type == "query" and result.freshness == "live"
    assert result.requires_sources


def test_unchecked_browser_auth_allows_read_attempt_without_claiming_login(mail_registry):
    plugin = mail_registry.get_plugin("browser_mail")
    assert plugin.auth_required
    assert plugin.is_connected() is None and plugin.is_authenticated() is None
    assert plugin.probe_connection().state == "unchecked"
    assert plugin.probe_authentication().state == "unchecked"
    brief = Mock()
    brief.collect.return_value = {"providers": [{"provider": "naver", "list_count": 2}]}
    plugin._brief_service = brief
    assert "browser_mail_collect_summary" in {tool.name for tool in mail_registry.get_all_tools()}
    result = mail_registry.execute_tool("browser_mail_collect_summary", {"providers": ["naver"]})
    assert result.succeeded
    assert brief.collect.call_args.kwargs["providers"] == ["naver"]
    assert callable(brief.collect.call_args.kwargs["checkpoint"])
    assert plugin.is_authenticated() is None


def test_verified_counts_have_a_readable_presentation_without_exposing_internal_json(mail_registry):
    payload = {"providers": [{"provider": "naver", "new_count": 3, "reply_count": 2, "draft_count": 22}],
               "summary": "네이버: 신규 3건 · 회신 검토 2건(AI 추정) · 미발송 초안 22건."}
    output = json.dumps(payload, ensure_ascii=False)
    assert mail_registry.present_result("browser_mail_collect_summary", output) == payload["summary"]
    assert mail_registry.present_result("browser_mail_list_inbox", output) == output


@pytest.mark.parametrize("output", ['{"summary": null}', '[]', 'not-json'])
def test_invalid_brief_presentation_does_not_echo_machine_output(mail_registry, output):
    assert mail_registry.present_result("browser_mail_collect_summary", output) == "메일 현황의 표시 결과를 확인하지 못했습니다."


def test_semantic_catalog_contains_browser_summary_and_infers_its_intent(mail_registry):
    proposal = {
        "relation": "new", "operation": "read", "intent_name": "",
        "tool_names": ["browser_mail_collect_summary"], "slots": {"providers": ["naver"]},
        "confidence": .98, "needs_clarification": False,
        "clarification_question": "", "control_scope": "current",
    }

    class ModelFixture:
        def __init__(self):
            self.calls = []

        def chat(self, messages):
            self.calls.append(messages)
            return json.dumps(proposal)

    model = ModelFixture()
    decision = SemanticRequestInterpreter(model, mail_registry).interpret("네이버 메일 현황 알려줘")
    assert decision.grounded
    result = decision.to_resolution(mail_registry)
    assert result.ready and result.intent_name == "mail.brief"
    assert result.tool_name == "browser_mail_collect_summary"
    catalogues = [json.loads(messages[1]["content"]).get("available_tools", [])
                  for messages in model.calls]
    tools = {tool["tool"]: tool for catalogue in catalogues for tool in catalogue}
    assert tools["browser_mail_collect_summary"]["intent"] == "mail.brief"
    assert tools["browser_mail_collect_summary"]["operation"] == "read"
    assert tools["browser_mail_collect_summary"]["freshness"] == "live"
    assert tools["browser_mail_collect_summary"]["requires_sources"] is True
    assert "mail_list_inbox" in tools


def test_grouped_discovery_keeps_mail_purpose_in_each_budgeted_index_batch(mail_registry):
    class LargeGroup(BasePlugin):
        def __init__(self, number):
            super().__init__()
            self.name = f"inspection_{number}_{'x' * 400}"
            self.tool_prefix = f"inspection_{number}"
            self.description = "별도 장치의 상태와 기록 조회"

        def get_tools(self):
            return [ToolSchema(f"{self.tool_prefix}_{'x' * 90}_{number}", "별도 장치 상태 조회",
                {"type": "object", "properties": {}}, side_effect="read") for number in range(10)]

        def execute_tool(self, *args):
            raise AssertionError("Discovery must not execute a tool")

    for number in range(8):
        mail_registry.register_plugin(LargeGroup(number))
    expected_groups = {plugin.name: len(plugin.get_tools()) for plugin in mail_registry.plugins.values()}

    class ModelFixture:
        def __init__(self):
            self.group_batches = 0
            self.saw_mail_purpose = False
            self.description_scopes = []
            self.group_counts = {}

        def chat_structured(self, messages, schema, **options):
            payload = json.loads(messages[1]["content"])
            if "available_tool_groups" in payload:
                self.group_batches += 1
                groups = payload["available_tool_groups"]
                assert all(type(count) is int for count in groups.values())
                self.group_counts.update(groups)
                descriptions = payload.get("group_descriptions", {})
                self.description_scopes.append((set(descriptions), set(groups)))
                if "browser_mail" in groups:
                    self.saw_mail_purpose = all(word in descriptions.get("browser_mail", "")
                        for word in ("현황", "수신", "회신", "초안"))
                return json.dumps({"request_kind": "action" if "browser_mail" in groups else "unsupported",
                    "group_names": ["browser_mail"] if "browser_mail" in groups else [], "confidence": .99})
            if "request_kind" in schema["properties"]:
                assert "browser_mail_collect_summary" in {tool["tool"] for tool in payload["available_tools"]}
                summary = next(tool for tool in payload["available_tools"]
                               if tool["tool"] == "browser_mail_collect_summary")
                assert summary["freshness"] == "live" and summary["requires_sources"] is True
                return json.dumps({"request_kind": "action", "tool_names": ["browser_mail_collect_summary"],
                    "confidence": .99})
            return json.dumps({"relation": "new", "operation": "read",
                "tool_names": ["browser_mail_collect_summary"], "slots": {"providers": ["naver"]},
                "confidence": .99, "needs_clarification": False, "clarification_question": "",
                "control_scope": "current"})

    model = ModelFixture()
    decision = SemanticRequestInterpreter(model, mail_registry).interpret("네이버 메일 현황 알려줘")
    assert model.group_batches > 1
    assert model.group_counts == expected_groups
    assert all(descriptions == groups for descriptions, groups in model.description_scopes)
    assert model.saw_mail_purpose
    assert decision.grounded
    result = decision.to_resolution(mail_registry)
    assert result.ready and result.tool_name == "browser_mail_collect_summary"
    assert result.slots == {"providers": ["naver"]}
