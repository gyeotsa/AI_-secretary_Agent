import json
from datetime import datetime

from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.response_presenter import present_response
from core.tts_normalizer import normalize_for_tts
from plugins.system_tools import SystemToolsPlugin
from plugins.weather import WeatherPlugin


def _router(*plugins):
    registry = PluginRegistry()
    for plugin in plugins:
        registry.register_plugin(plugin)
    return IntentRouter(registry), registry


def test_current_time_question_routes_to_system_clock():
    router, registry = _router(SystemToolsPlugin())
    resolution = router.resolve("현재 시각 몇시야?")

    assert resolution.ready
    assert resolution.tool_name == "get_time"
    raw = registry.execute_tool(resolution.tool_name, resolution.slots)
    parsed = datetime.fromisoformat(raw)
    assert parsed.tzinfo is not None
    assert abs((datetime.now().astimezone() - parsed).total_seconds()) < 2
    assert "현재 시간은" in registry.present_result("get_time", raw)


def test_weather_intent_extracts_location_and_uses_live_result_contract():
    router, registry = _router(WeatherPlugin())
    resolution = router.resolve("서울 현재 날씨와 온도 알려줘")

    assert resolution.ready
    assert resolution.tool_name == "get_weather"
    assert resolution.slots["location"] == "서울"

    raw = json.dumps({
        "requested_location": "서울",
        "location_precision": "exact",
        "observed_at": "2026-07-27T15:00",
        "temperature_c": 29.4,
        "apparent_temperature_c": 31.0,
        "humidity_percent": 70,
        "weather_code": 2,
        "today_min_c": 24.0,
        "today_max_c": 31.0,
    }, ensure_ascii=False)
    presented = registry.present_result("get_weather", raw)
    assert "부분적으로 흐림" in presented
    assert "29.4도" in presented
    assert "관측 시각은 15:00" in presented


def test_weather_without_location_asks_instead_of_guessing():
    router, _registry = _router(WeatherPlugin())
    resolution = router.resolve("오늘 날씨 어때?")

    assert resolution.matched
    assert not resolution.ready
    assert "어느 지역" in resolution.question


def test_weather_polite_request_is_not_mistaken_for_a_location():
    router, _registry = _router(WeatherPlugin())

    resolution = router.resolve("오늘 날씨 알려줄래?")
    detailed = router.resolve("서울시 구로구 항동의 날씨를 알려줘")

    assert not resolution.ready
    assert "location" not in resolution.slots
    assert detailed.slots["location"] == "서울시 구로구 항동"


def test_tts_normalizer_reads_words_clocks_numbers_and_units_naturally():
    spoken = normalize_for_tts("wet, 현재 17:05이고 25.7C, 습도 70%입니다.")

    assert "웻" in spoken
    assert "오후 다섯 시 오 분" in spoken
    assert "이십오 점 칠 도" in spoken
    assert "칠십 퍼센트" in spoken
    assert "더블유" not in spoken


def test_uppercase_dictionary_word_is_pronounced_as_a_word_not_an_acronym():
    assert normalize_for_tts("GAME API GPU") == "게임 에이피아이 지피유"


def test_read_aloud_intent_returns_literal_text_without_llm_generation():
    router, registry = _router(SystemToolsPlugin())
    resolution = router.resolve("hello를 읽어봐")

    assert resolution.ready
    assert resolution.tool_name == "repeat_text"
    assert registry.execute_tool(resolution.tool_name, resolution.slots) == "hello"


def test_unrequested_cjk_output_is_removed_but_korean_prefix_survives():
    raw = (
        "현재 시간은 15:00입니다, 지휘관님. 치과 예약까지还有一些翻译：\n\n"
        "当前时间是15:00。"
    )
    assert present_response(raw, "현재 시각 몇 시야?") == "현재 시간은 15:00입니다, 지휘관님."
    assert "当前时间" in present_response(raw, "중국어 원문도 보여줘")
