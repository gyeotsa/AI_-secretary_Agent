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
        "sources": [
            {"source_id": "S1", "url": "https://official.example/anis",
             "title": "니케 아니스 공식 캐릭터 소개", "content": "니케의 아니스는 밝고 솔직하며 상황에 따라 진지하게 대화한다."},
            {"source_id": "S2", "url": "https://guide.example/anis",
             "title": "니케 아니스 가이드", "content": "니케 아니스의 말투와 성격을 설명한다."},
        ],
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


def test_learning_subject_handles_game_e_and_quoted_character_wording():
    plugin = BrowserPlugin(); registry = PluginRegistry(); registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve(
        "웹에서 승리의 여신 : 니케 라는 게임에 등장하는 '아니스'라는 캐릭터에 대해서 "
        "조사하고 해당 캐릭터가 사용하는 말투를 학습해서 너의 말투로 사용해줘"
    )
    assert resolution.intent_name == "web.research_and_apply_preference"
    assert resolution.slots["query"] == "승리의 여신 : 니케 아니스 캐릭터 성격 대사 말투 공식 자료"


def test_learning_sources_require_both_character_and_game_context():
    sources = [
        {"title": "니케 아니스 캐릭터 소개", "content": "아니스의 성격과 말투를 소개한다."},
        {"title": "아니스 향수", "content": "아니스 향과 사용법"},
        {"title": "승리의 여신 니케 소식", "content": "신규 이벤트 안내"},
    ]
    relevant = BrowserPlugin._relevant_learning_sources(
        "승리의 여신 : 니케 아니스 캐릭터 성격 대사 말투 공식 자료", sources,
    )
    assert relevant == [sources[0]]


def test_learning_excerpt_prefers_character_context_over_navigation():
    source = {
        "title": "니케 아니스 소개",
        "content": "메뉴\n광고\n카테고리\n아니스의 성격은 밝고 솔직하다.\n대화에서는 장난스러운 말투를 쓴다.\n푸터",
    }
    excerpt = BrowserPlugin._learning_excerpt(
        "승리의 여신 니케 아니스 캐릭터 성격 대사 말투 공식 자료", source,
    )
    assert "밝고 솔직" in excerpt and "장난스러운 말투" in excerpt


def test_learning_candidate_ranking_prefers_dialogue_over_build_guide():
    dialogue = {"title": "니케 아니스 대사와 성격", "snippet": "아니스의 대화 말투와 보이스"}
    build = {"title": "니케 아니스 육성 스킬 공략", "snippet": "장비와 큐브 추천"}
    assert BrowserPlugin._candidate_learning_score(dialogue, "승리의 여신 니케", "아니스") > (
        BrowserPlugin._candidate_learning_score(build, "승리의 여신 니케", "아니스")
    )


def test_youtube_learning_routes_and_requires_real_transcript(monkeypatch):
    plugin = BrowserPlugin(); registry = PluginRegistry(); registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve(
        "유튜브에 아니스가 등장하는 영상을 보고 학습해. https://www.youtube.com/watch?v=test123"
    )
    assert resolution.intent_name == "web.video_learning"
    assert resolution.ready and resolution.slots["setting"] == "response_style"
    assert resolution.slots["url"].startswith("https://www.youtube.com/")

    monkeypatch.setattr(plugin, "_youtube_transcript", lambda _url: (
        "밝고 솔직하게 대화를 이어간다. 장난스럽게 말하지만 중요한 상황에서는 차분하고 진지하다. " * 3,
        "공식 캐릭터 영상", "https://www.youtube.com/watch?v=test123",
    ))
    monkeypatch.setattr(plugin, "_local_speaker_corpus", lambda _subject: (
        "농담도. 어디 쉽고 편한 임무라도 들어왔어? 걱정 마. 중요한 순간에는 우리가 지킬게. " * 4
    ))
    monkeypatch.setattr("core.llm.get_llm_client", lambda _role: type("LLM", (), {"chat": lambda self, _messages:
        '{"summary":"밝고 솔직하며 필요할 때 진지함","preference":"밝고 솔직한 반말로 대답하고 중요한 상황에서는 차분하고 진지하게 표현해"}'})())
    monkeypatch.setattr("core.assistant_settings.get_assistant_settings", lambda: type(
        "Settings", (), {"set": staticmethod(lambda _key, value: value)}
    )())
    class Rag:
        def add_text_document(self, _text, **kwargs): self.doc_id = kwargs["doc_id"]
        def search_docs(self, *_args, **_kwargs): return [{"doc_id": self.doc_id}]
    rag = Rag(); monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.succeeded and result.evidence[0].data["transcript_chars"] > 80


def test_video_learning_recovers_from_invalid_json(monkeypatch):
    plugin = BrowserPlugin()
    monkeypatch.setattr(plugin, "_youtube_transcript", lambda _url: (
        "여러 인물이 대화하는 영상 전체 자막 " * 20, "영상", "https://youtu.be/test",
    ))
    monkeypatch.setattr(plugin, "_local_speaker_corpus", lambda _subject: (
        "장난스럽고 밝게 말한다. 중요한 순간에는 솔직하고 진지하게 말한다. " * 20
    ))
    responses = iter(["", "밝고 장난스러운 반말로 대답하고 중요한 상황에서는 솔직하고 진지하게 표현해"])
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
    result = plugin.execute_tool("browser_learn_video_preference", {
        "url": "https://youtu.be/test", "setting": "response_style", "subject": "Anis",
    })
    assert result.succeeded


def test_video_learning_follow_up_reuses_subject_slot():
    plugin = BrowserPlugin(); registry = PluginRegistry(); registry.register_plugin(plugin)
    router = IntentRouter(registry)
    first = router.resolve(
        "유튜브에 아니스가 등장하는 영상을 보고 학습해. https://www.youtube.com/watch?v=one"
    )
    second = router.resolve(
        "https://www.youtube.com/watch?v=two 이 영상을 보고 더 제대로 학습해봐",
        first.intent_name, first.slots,
    )
    assert second.ready and second.slots["subject"] == "아니스"
    assert second.slots["url"].endswith("v=two")


def test_local_anis_speaker_corpus_is_available():
    corpus = BrowserPlugin._local_speaker_corpus("Anis")
    assert "지휘관님" in corpus and len(corpus) > 200


def test_corpus_style_fallback_uses_observed_language_features():
    corpus = "\n".join([
        "지휘관님, 어디 놀러 갈래? 농담도.",
        "걱정 마. 지휘관님은 우리가 지키니까.",
        "오? 나 주는 거야? 진짜?",
        "아니, 포기 안 해. 같이 힘내자!",
    ] * 5)
    style = BrowserPlugin._corpus_style_instruction(corpus)
    assert "님을 붙이고" in style and "반말" in style
    assert "장난" in style and "진지" in style


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
        "sources": [
            {"source_id": "S1", "url": "https://official.example/anis",
             "title": "니케 아니스 공식 소개", "content": "니케 아니스는 밝고 솔직하지만 중요한 순간에는 진지한 말투를 쓴다."},
            {"source_id": "S2", "url": "https://guide.example/anis",
             "title": "니케 아니스 가이드", "content": "니케 아니스의 말투와 성격을 설명한다."},
        ],
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


def test_subject_led_style_is_normalized_to_reusable_instruction():
    query = "승리의 여신 : 니케 아니스 캐릭터 성격 대사 말투 공식 자료"
    normalized = BrowserPlugin._normalize_learned_preference(
        query, "아니스의 말투는 밝고 솔직한 반말이며 중요한 순간에는 진지하다",
    )
    assert "아니스" not in normalized and "승리의 여신" not in normalized
    assert normalized.endswith("대답해")
    BrowserPlugin._validate_learned_preference(query, normalized)
