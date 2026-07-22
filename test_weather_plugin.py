import json
import pytest

from plugins.weather import WeatherPlugin


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
    result = json.loads(WeatherPlugin().execute_tool("get_weather", {"location": "서울 구로구 항동"}))
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
        return _Response({"current": {"temperature_2m": 28.0}, "daily": {}})
    monkeypatch.setattr("plugins.weather.requests.get", get)
    payload = json.loads(WeatherPlugin().execute_tool("get_weather", {"location": "서울시 구로구 항동"}))
    assert payload["geocoding_query"] == "Seoul"
    assert payload["location_precision"] == "city"


@pytest.mark.integration
def test_weather_plugin_reaches_live_open_meteo():
    result = WeatherPlugin().execute_tool("get_weather", {"location": "Seoul"})
    assert not result.startswith("오류:")
    payload = json.loads(result)
    assert isinstance(payload["temperature_c"], (int, float))
