import json
import pytest

from core.research import ResearchClaim, ResearchReport, ResearchSource
from core.tool_result import Evidence, ToolRunResult
from plugins.browser import BrowserPlugin
from core.assistant_settings import AssistantSettings
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry


def test_browser_research_returns_visited_sources_and_claim_evidence(monkeypatch):
    source = ResearchSource("S1", "https://example.com/article", "공식 자료",
                            "검증 본문", 100.0, published_at="2026-08-01", official=True)
    report = ResearchReport("질문", 100.0, 200.0, [source],
                            [ResearchClaim("검증된 주장입니다.", ["S1"], 1)], [])

    class Agent:
        def research(self, *_args, **_kwargs): return report

    plugin = BrowserPlugin()
    monkeypatch.setattr(plugin, "_get_research_agent", lambda: Agent())
    result = plugin.execute_tool("browser_research", {"query": "질문", "max_sources": 3})
    assert isinstance(result, ToolRunResult) and result.succeeded
    payload = json.loads(result.raw_output)
    assert payload["sources"][0]["published_at"] == "2026-08-01"
    assert payload["claims"][0]["citations"] == ["S1"]
    assert result.artifacts[0].metadata["official"] is True


def test_research_presenter_keeps_claim_level_citations_and_conflict_warning():
    plugin = BrowserPlugin()
    payload = json.dumps({
        "claims": [{"text": "가격 정보가 다릅니다.", "citations": ["S1", "S2"]}],
        "conflicts": [{"claim": "가격", "citations": ["S1", "S2"]}],
        "sources": [
            {"source_id": "S1", "url": "https://one.example"},
            {"source_id": "S2", "url": "https://two.example"},
        ],
    }, ensure_ascii=False)
    rendered = plugin.present_result("browser_research", payload)
    assert "[S1, S2]" in rendered
    assert "상충하는 정보 1건" in rendered
    assert "[S1] https://one.example" in rendered


def test_compound_web_learning_routes_and_persists_setting_and_rag(monkeypatch):
    plugin = BrowserPlugin(); registry = PluginRegistry(); registry.register_plugin(plugin)
    request = "웹 검색을 통해 승리의 여신 : 니케라는 게임에서 나오는 아니스 라는 캐릭터의 말투를 조사하고 분석해서 너의 말투에 반영해줘"
    resolution = IntentRouter(registry).resolve(request)
    assert resolution.intent_name == "web.research_and_apply_preference"
    assert resolution.ready and resolution.slots["setting"] == "response_style"
    assert "아니스" in resolution.slots["query"] and "말투" in resolution.slots["query"]
    assert resolution.slots["query"].count("승리의 여신") == 1

    payload = json.dumps({
        "expires_at": 9999,
        "sources": [{"source_id": "S1", "url": "https://official.example/anis",
                     "title": "공식 캐릭터 소개", "content": "아니스는 밝고 솔직하며 상황에 따라 진지하게 대화한다."}],
        "claims": [], "conflicts": [],
    }, ensure_ascii=False)
    monkeypatch.setattr(plugin, "_research_web", lambda _data: ToolRunResult.successful(
        tool_name="browser_research", raw_output=payload,
        evidence=[Evidence("research_report", "검증된 조사 결과", {})]))

    class Profile:
        def __init__(self): self.values = {}
        def get_preference(self, key, default=""): return self.values.get(key, default)
        def set_preference(self, key, value): self.values[key] = value
    settings = AssistantSettings(Profile())
    monkeypatch.setattr("core.assistant_settings.get_assistant_settings", lambda: settings)
    monkeypatch.setattr("core.llm.get_llm_client", lambda _role: type("LLM", (), {"chat": lambda self, _messages:
        '{"summary":"밝고 솔직하며 필요할 때 진지함","preference":"밝고 솔직한 자연스러운 반말을 쓰고 중요한 상황에서는 차분하고 진지하게 대답"}'})())

    captured = {}
    class Rag:
        def add_text_document(self, text, **kwargs): captured.update(text=text, **kwargs); return kwargs["doc_id"]
        def search_docs(self, _query, **_kwargs): return [{"doc_id": captured["doc_id"]}]
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: Rag())
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded
    assert "밝고 솔직한" in settings.get("response_style")
    assert captured["metadata"]["source_type"] == "web"
    assert captured["source_uri"].startswith("web-learning://")


def test_web_learning_follow_up_checks_persisted_setting_and_rag(monkeypatch):
    plugin = BrowserPlugin(); registry = PluginRegistry(); registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve("이제 해당 조사를 통해 말투를 학습했어?")
    assert resolution.intent_name == "web.learning_status"
    assert resolution.ready and resolution.slots["setting"] == "response_style"

    monkeypatch.setattr("core.assistant_settings.get_assistant_settings", lambda: type(
        "Settings", (), {"get": staticmethod(lambda _key: "밝고 자연스러운 반말")}
    )())
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: type(
        "Rag", (), {"search_docs": staticmethod(lambda *_args, **_kwargs: [{
            "doc_id": "web-learning-1", "source": "web-learning://web-learning-1",
        }])}
    )())
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded
    assert result.evidence[0].data["rag_recalled"] is True


def test_web_learning_recovers_when_first_structured_response_is_invalid(monkeypatch):
    plugin = BrowserPlugin()
    payload = json.dumps({
        "expires_at": 9999999999,
        "sources": [{"source_id": "S1", "url": "https://official.example/anis",
                     "title": "공식 소개", "content": "아니스는 밝고 솔직하지만 중요한 순간에는 진지하다."}],
    }, ensure_ascii=False)
    monkeypatch.setattr(plugin, "_research_web", lambda _data: ToolRunResult.successful(
        tool_name="browser_research", raw_output=payload,
        evidence=[Evidence("research_report", "검증", {})]))

    responses = iter(["", "밝고 솔직한 반말을 쓰되 중요한 상황에서는 차분하고 진지하게 대답"])
    monkeypatch.setattr("core.llm.get_llm_client", lambda _role: type(
        "LLM", (), {"chat": lambda self, _messages: next(responses)}
    )())
    monkeypatch.setattr("core.assistant_settings.get_assistant_settings", lambda: type(
        "Settings", (), {"set": staticmethod(lambda _key, value: value)}
    )())
    class Rag:
        def add_text_document(self, _text, **kwargs): self.doc_id = kwargs["doc_id"]
        def search_docs(self, *_args, **_kwargs): return [{"doc_id": self.doc_id}]
    rag = Rag(); monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    result = plugin.execute_tool("browser_research_and_apply_preference", {
        "query": "니케 아니스 캐릭터 말투 공식 자료", "setting": "response_style",
    })
    assert result.succeeded


def test_web_learning_rejects_identity_leakage_before_persistence():
    with pytest.raises(ValueError, match="(?:정체성|잘린 저품질)"):
        BrowserPlugin._validate_learned_preference(
            "승리의 여신 : 니케 아니스 캐릭터 성격 대사 말투 공식 자료",
            "니스는 간결하고 명료한 말투를 사용하며, 승리의 여신이라는 성격으로 자신감이 넘칩니다",
        )
