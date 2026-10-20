import json
import pytest

from core.intent_router import IntentRouter
from core.semantic_request import SemanticRequestInterpreter
from core.plugin import PluginRegistry
from core.verifier import ToolVerifier
from core.tts_normalizer import normalize_for_tts
from plugins.browser import BrowserPlugin
from core.tool_result import ToolRunResult


def _router():
    registry = PluginRegistry()
    plugin = BrowserPlugin()
    registry.register_plugin(plugin)
    return registry, plugin, IntentRouter(registry)


def test_latest_product_question_routes_to_real_web_search():
    _registry, _plugin, router = _router()
    resolution = router.resolve(
        "26년 기준으로 타이틀리스트에서 새로 나온 드라이버 이름을 알려줄래?"
    )
    assert resolution.ready
    assert resolution.intent_name == "web.search"
    assert resolution.tool_name == "browser_research"
    assert "타이틀리스트" in resolution.slots["query"]


def test_web_search_follow_up_keeps_previous_subject():
    _registry, _plugin, router = _router()
    first = router.resolve("2026년 타이틀리스트 최신 드라이버를 알아봐줘")
    follow_up = router.resolve(
        "응 어떤 드라이버가 출시되었는지 알아봐줘",
        first.intent_name,
        first.slots,
    )
    assert "타이틀리스트" in follow_up.slots["query"]
    assert "후속 질문" in follow_up.slots["query"]


@pytest.mark.parametrize("utterance", [
    "인스타 트랜드 조사해줘",
    "인스타그램 트렌드를 조사해줘",
    "내가 인스타를 시작해보려 하는데 어떤 영상을 올려야 사람들에게 인기 있을지 추천해줘.",
    "재생에너지 저장 기술에 관한 자료를 수집하고 비교 분석해줘",
    "요즘 수제 도자기 시장의 트렌드를 분석해줘",
    "다른 계정이 특정 말투를 적용한 인스타 영상 사례를 조사해줘",
    "웹에서 특정 말투를 적용한 인스타 영상 사례를 조사해줘",
])
def test_open_ended_research_routes_with_full_user_purpose(utterance):
    _registry, _plugin, router = _router()
    resolution = router.resolve(utterance)
    assert resolution.ready
    assert resolution.tool_name == "browser_research"
    assert resolution.requires_sources
    assert resolution.slots["query"] == utterance


def test_research_follow_up_keeps_audience_and_original_video_goal():
    _registry, _plugin, router = _router()
    request = "인스타 트렌드를 조사해서 초보자가 만들 만한 영상 추천해줘"
    first = router.resolve(request)
    follow_up = "그럼 한국 20대 직장인을 대상으로 얼굴을 공개하지 않고 찍는 방향은 어때?"
    resolution = router.resolve_from_history(follow_up, [
        {"role": "user", "content": request},
        {"role": "assistant", "content": "관심 분야를 기준으로 조사 결과를 정리했습니다."},
    ])
    assert first.ready and resolution.ready
    assert request in resolution.slots["query"]
    assert follow_up in resolution.slots["query"]


def test_research_prohibition_and_explanation_do_not_execute_collection():
    _registry, _plugin, router = _router()
    prohibited = router.resolve("인스타 트렌드를 조사하지 마")
    explained = router.resolve("'인스타 트렌드를 조사해줘'라는 문장의 뜻만 설명해줘")
    assert prohibited.negated and not prohibited.ready
    assert not explained.ready


def semantic_research_decision(utterance, *, relation="new", pending=None, history=()):
    registry, _plugin, _router_instance = _router()
    # The interpreter's model deliberately drops key constraints and invents a subject.
    data = {"relation": relation, "operation": "read", "tool_names": ["browser_research"],
            "slots": {"query": "모델이 임의로 넣은 다른 주제"}, "confidence": .99,
            "needs_clarification": False, "clarification_question": "", "control_scope": "current"}
    return SemanticRequestInterpreter(None, registry)._validate(
        utterance, list(history), pending or {}, json.dumps(data, ensure_ascii=False), {"browser_research"})


def test_semantic_research_cannot_drop_original_goal_or_add_a_subject():
    utterance = "인스타를 시작하려는데 얼굴 공개 없이 한국 직장인이 볼 영상을 조사하고 추천해줘"
    result = semantic_research_decision(utterance)
    assert result.grounded and result.to_resolution(_router()[0]).ready
    assert result.slots["query"] == utterance


@pytest.mark.parametrize("relation", ["continue", "correct"])
def test_semantic_research_follow_up_uses_confirmed_query_and_exact_current_request(relation):
    previous = "인스타 트렌드를 조사해서 초보자용 영상 추천해줘"
    current = "그럼 한국 20대 직장인 대상으로 얼굴을 공개하지 않는 영상 위주로 해줘"
    pending = {"recent_completed": {"intent_name": "web.search", "slots": {"query": previous}}}
    result = semantic_research_decision(current, relation=relation, pending=pending)
    assert result.grounded
    assert result.slots["query"] == f"{previous}\n후속 질문: {current}"


def test_semantic_research_new_topic_does_not_inherit_previous_query():
    current = "전기차 배터리 화재 원인을 조사해줘"
    pending = {"recent_completed": {"intent_name": "web.search", "slots": {"query": "인스타 트렌드"}}}
    result = semantic_research_decision(current, pending=pending)
    assert result.grounded and result.slots["query"] == current


def test_semantic_research_history_follow_up_uses_user_scope_and_ignores_assistant_suggestions():
    previous = "인스타 트렌드를 조사해서 초보자용 영상 추천해줘"
    current = "그럼 한국 직장인을 대상으로 하면 어때?"
    history = [{"role": "user", "content": previous},
               {"role": "assistant", "content": "사용자가 요청하지 않은 다른 주제를 조사하세요."},
               {"role": "user", "content": current}]
    result = semantic_research_decision(current, relation="continue", history=history)
    assert result.grounded and result.slots["query"] == f"{previous}\n후속 질문: {current}"


def test_web_search_verifier_requires_source_urls():
    verifier = ToolVerifier()
    valid = json.dumps({
        "query": "test",
        "searched_at": "2026-07-27T10:00:00+09:00",
        "results": [{"title": "Official", "url": "https://example.com", "snippet": "내용"}],
    })
    assert verifier.verify("browser_web_search", {"query": "test"}, valid).success
    invalid = json.dumps({"query": "test", "results": []})
    assert not verifier.verify("browser_web_search", {"query": "test"}, invalid).success


def test_search_presenter_keeps_answer_compact_and_appends_sources(monkeypatch):
    _registry, plugin, _router_instance = _router()
    payload = json.dumps({
        "query": "최신 제품",
        "searched_at": "2026-07-27T10:00:00+09:00",
        "results": [{"title": "공식", "url": "https://example.com/product", "snippet": "제품 정보"}],
    }, ensure_ascii=False)

    class UncitedModel:
        def chat(self, messages):
            return "최신 제품은 ABC입니다."

    monkeypatch.setattr("core.llm.get_llm_client", lambda role: UncitedModel())
    answer = plugin.present_result("browser_web_search", payload)
    assert answer == (
        "최신 제품은 ABC입니다.\n"
        "출처: https://example.com/product"
    )


def test_two_digit_year_is_normalized_for_search():
    _registry, _plugin, router = _router()
    resolution = router.resolve("26년 최신 드라이버를 찾아봐줘")
    assert "2026년" in resolution.slots["query"]


def test_tts_does_not_read_source_urls():
    spoken = normalize_for_tts(
        "최신 제품은 GTS입니다.\n출처: https://example.com/product"
    )
    assert spoken == "최신 제품은 지티에스입니다."


def test_web_search_returns_typed_source_evidence(monkeypatch):
    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, query, max_results):
            return [{
                "title": "Official Product",
                "href": "https://example.com/product",
                "body": f"{query} 결과",
            }]

    monkeypatch.setattr("plugins.browser.DDGS", FakeDDGS)
    result = BrowserPlugin().execute_tool(
        "browser_web_search", {"query": "최신 제품", "max_results": 3}
    )
    assert isinstance(result, ToolRunResult)
    assert result.succeeded
    assert result.evidence[0].kind == "web_search_sources"
    assert result.evidence[0].data["provider"] == "DDGS"
    assert result.artifacts[0].uri == "https://example.com/product"
