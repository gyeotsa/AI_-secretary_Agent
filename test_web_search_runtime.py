import json

from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.verifier import ToolVerifier
from core.tts_normalizer import normalize_for_tts
from plugins.browser import BrowserPlugin


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
    assert resolution.tool_name == "browser_web_search"
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
