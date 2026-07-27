import json
import pytest

from plugins.weather import WeatherPlugin
from core.tool_result import ToolRunResult


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_weather_plugin_returns_structured_observation(monkeypatch):
    responses = iter([
        _Response({"results": [{"name": "항동", "admin2": "구로구", "admin1": "서울특별시",
                                 "country": "대한민국", "latitude": 37.48, "longitude": 126.82}]}),
        _Response({"current": {"time": "2026-07-22T18:00", "temperature_2m": 29.2,
                                "apparent_temperature": 31.0, "relative_humidity_2m": 70,
                                "weather_code": 1, "wind_speed_10m": 8.0},
                   "daily": {"temperature_2m_min": [24.0], "temperature_2m_max": [31.0],
                             "precipitation_probability_max": [20]}}),
    ])
    monkeypatch.setattr("plugins.weather.requests.get", lambda *_args, **_kwargs: next(responses))
    tool_result = WeatherPlugin().execute_tool("get_weather", {"location": "서울 구로구 항동"})
    assert isinstance(tool_result, ToolRunResult)
    assert tool_result.evidence[0].kind == "weather_observation"
    result = json.loads(tool_result.raw_output)
    assert result["temperature_c"] == 29.2
    assert result["resolved_location"].startswith("항동, 구로구")
    assert result["source"] == "Open-Meteo"


def test_weather_falls_back_to_parent_city_for_korean_dong_address(monkeypatch):
    calls = []
    def get(_url, params, **_kwargs):
        calls.append(params)
        if "geocoding-api" in _url:
            if params["name"] != "Seoul":
                return _Response({"results": []})
            return _Response({"results": [{"name": "서울", "admin1": "서울특별시", "country": "대한민국",
                                             "latitude": 37.57, "longitude": 126.98}]})
        return _Response({
            "current": {"time": "2026-07-22T18:00", "temperature_2m": 28.0},
            "daily": {},
        })
    monkeypatch.setattr("plugins.weather.requests.get", get)
    tool_result = WeatherPlugin().execute_tool("get_weather", {"location": "서울시 구로구 항동"})
    payload = json.loads(tool_result.raw_output)
    assert payload["geocoding_query"] == "Seoul"
    assert payload["location_precision"] == "city"


def test_weather_without_observation_time_is_not_reported_as_success(monkeypatch):
    responses = iter([
        _Response({"results": [{
            "name": "서울", "country": "대한민국",
            "latitude": 37.57, "longitude": 126.98,
        }]}),
        _Response({"current": {"temperature_2m": 28.0}, "daily": {}}),
    ])
    monkeypatch.setattr(
        "plugins.weather.requests.get",
        lambda *_args, **_kwargs: next(responses),
    )
    result = WeatherPlugin().execute_tool("get_weather", {"location": "서울"})
    assert isinstance(result, ToolRunResult)
    assert not result.succeeded
    assert "관측 시각" in result.error


@pytest.mark.integration
def test_weather_plugin_reaches_live_open_meteo():
    result = WeatherPlugin().execute_tool("get_weather", {"location": "Seoul"})
    assert isinstance(result, ToolRunResult)
    assert result.succeeded
    payload = json.loads(result.raw_output)
    assert isinstance(payload["temperature_c"], (int, float))
