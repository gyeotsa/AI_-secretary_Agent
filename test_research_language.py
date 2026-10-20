import copy
from pathlib import Path

import pytest

from core.plugin import ToolCancelledError
from core.research import ResearchAgent, ResearchCache


def test_translation_changes_only_generated_prose_and_preserves_bound_evidence(tmp_path):
    original = "样本有20个，不能代表所有用户。"
    quote = "中文原文有20个，不能代表所有用户。"
    analysis = {"claims": [{"text": original, "caveat": "", "evidence": [{"source_id": "S1", "quote": quote}]}],
                "source_assessments": [{"source_id": "S1", "assessment": original}],
                "limitations": [original]}
    evidence = copy.deepcopy(analysis["claims"][0]["evidence"])
    calls = []
    def reasoner(messages):
        import json
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        assert quote not in str(payload)
        return {"T1": "표본은 20개이며 전체 이용자를 대표하지 않습니다."}
    agent = ResearchAgent(None, None, ResearchCache(str(tmp_path / "cache.db")), reasoner)
    limitations = [original]
    agent._localize_analysis(analysis, limitations)
    assert len(calls) == 1
    assert analysis["claims"][0]["text"] == limitations[0] == analysis["source_assessments"][0]["assessment"]
    assert analysis["claims"][0]["evidence"] == evidence


def test_translation_that_changes_measurements_is_excluded(tmp_path):
    agent = ResearchAgent(None, None, ResearchCache(str(tmp_path / "cache.db")),
                          lambda _: {"T1": "표본은 200개입니다."})
    analysis, limitations = {"claims": [{"text": "样本有20个。"}]}, []
    agent._localize_analysis(analysis, limitations)
    assert analysis["claims"][0]["text"] == ""
    assert analysis["_language_failed"] and limitations


@pytest.mark.parametrize("field", ["claims", "recommendations"])
def test_mixed_chinese_in_korean_generated_prose_is_translated(tmp_path, field):
    import json
    original = "S1:P2에 따르면 20개 영상을 만드는 것이前所未有的 쉬워졌지만 성과를 보장하지 않습니다."
    translated = "S1:P2에 따르면 20개 영상을 만드는 것이 전례 없이 쉬워졌지만 성과를 보장하지 않습니다."
    quote = "中文原文有20个，不能代表所有用户。"
    source = {"source_id": "S1", "title": "中文资料标题", "content": quote}
    analysis = {field: [{"text": original, "evidence": [{"source_id": "S1", "quote": quote}]}],
                "sources": [source]}
    evidence = copy.deepcopy(analysis[field][0]["evidence"])
    source_snapshot = copy.deepcopy(source)
    calls = []

    def reasoner(messages):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        assert payload == {"language_repair": {"T1": original}}
        return {"T1": translated}

    agent = ResearchAgent(None, None, ResearchCache(str(tmp_path / "cache.db")), reasoner)
    agent._localize_analysis(analysis, [])
    assert len(calls) == 1
    assert analysis[field][0]["text"] == translated
    assert analysis[field][0]["evidence"] == evidence
    assert source == source_snapshot


def test_translation_that_leaves_mixed_chinese_is_excluded(tmp_path):
    original = "영상 제작이前所未有的 쉬워졌다는 설명입니다."
    agent = ResearchAgent(None, None, ResearchCache(str(tmp_path / "cache.db")),
                          lambda _: {"T1": original})
    analysis, limitations = {"claims": [{"text": original}]}, []
    agent._localize_analysis(analysis, limitations)
    assert analysis["claims"][0]["text"] == ""
    assert analysis["_language_failed"] and limitations


def test_translation_cancellation_is_propagated(tmp_path):
    def cancelled(_):
        raise ToolCancelledError("cancelled")
    agent = ResearchAgent(None, None, ResearchCache(str(tmp_path / "cache.db")), cancelled)
    with pytest.raises(ToolCancelledError):
        agent._localize_analysis({"claims": [{"text": "生成中文内容。"}]}, [])


def test_failed_qualifier_translation_rejects_items_and_downgrades_rag_sources(tmp_path, monkeypatch):
    import json
    import time
    from config import Config
    from core.research import ResearchReport, ResearchSource
    from plugins.browser import BrowserPlugin
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    qualifier = "样本有20个，不能代表所有用户。"
    sources = [ResearchSource("S1", "https://example.org/one", "첫 자료", "First source observed 20 participants.", time.time()),
               ResearchSource("S2", "https://example.org/two", "다른 자료", "Second source observed 30 participants.", time.time())]
    quotes = [{"source_id": source.source_id, "quote": source.content} for source in sources]
    analysis = {
        "claims": [{"kind": "fact", "text": "자료의 첫 관찰입니다.", "caveat": qualifier, "evidence": [quotes[0]]},
                   {"kind": "fact", "text": "자료의 다른 관찰입니다.", "caveat": "", "evidence": [quotes[1]]}],
        "recommendations": [{"option": "中文选项一", "text": "한국어 실행 제안입니다.", "caveat": qualifier, "evidence": [quotes[0]]}],
        "conflicts": [{"claim": "자료의 관찰이 다릅니다.", "explanation": qualifier, "evidence": quotes}],
        "source_assessments": [{"source_id": "S1", "reliability": "medium", "assessment": "첫 관찰 범위입니다."},
                               {"source_id": "S2", "reliability": "medium", "assessment": qualifier}],
    }
    evidence = copy.deepcopy([item["evidence"] for item in analysis["claims"]])
    agent = ResearchAgent(None, None, ResearchCache(str(tmp_path / "cache.db")),
                          lambda _: {"T1": "표본은 200개이며 전체 이용자를 대표하지 않습니다."})
    limitations = []
    agent._localize_analysis(analysis, limitations)
    assert analysis["claims"][0]["text"] == analysis["recommendations"][0]["text"] == ""
    assert analysis["conflicts"][0]["claim"] == ""
    assert analysis["source_assessments"][1]["reliability"] == "unassessed"
    assert [item["evidence"] for item in analysis["claims"]] == evidence
    claims, conflicts, recommendations = agent._validated_analysis(analysis, sources, limitations)
    assert len(claims) == 1 and claims[0].citations == ["S2"]
    assert not conflicts and not recommendations
    indexed = []
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: indexed.append(True))
    report = ResearchReport("검증", time.time(), time.time()+60, sources, claims, conflicts,
                            recommendations=recommendations, limitations=limitations)
    payload = report.to_dict()
    BrowserPlugin()._save_research_report(payload)
    assert not indexed and not payload["rag_saved"]
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert [source["content"] for source in saved["sources"]] == [source.content for source in sources]
