import json

from core.research import ResearchClaim, ResearchReport, ResearchSource
from core.tool_result import ToolRunResult
from plugins.browser import BrowserPlugin


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
