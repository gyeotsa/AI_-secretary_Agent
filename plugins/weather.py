"""Open-Meteo 기반 실제 현재 날씨 조회 플러그인."""
from typing import Any, Dict, List
import json
import requests

from core.plugin import BasePlugin, ToolSchema


class WeatherPlugin(BasePlugin):
    CITY_FALLBACKS = {
        "서울": "Seoul", "부산": "Busan", "인천": "Incheon", "대구": "Daegu",
        "대전": "Daejeon", "광주": "Gwangju", "울산": "Ulsan", "세종": "Sejong",
        "제주": "Jeju City",
    }
    def __init__(self):
        super().__init__()
        self.name = "weather"
        self.description = "Open-Meteo 현재 날씨 조회"

    def get_tools(self) -> List[ToolSchema]:
        return [ToolSchema("get_weather", "지정한 장소의 실제 현재 날씨와 오늘 최저·최고 기온을 조회합니다", {
            "type": "object", "properties": {
                "location": {"type": "string", "description": "시·구·동을 포함한 장소 이름"},
            }, "required": ["location"],
        }, [])]

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        if tool_name != "get_weather":
            return f"오류: 알 수 없는 툴 '{tool_name}'"
        location = str(tool_input.get("location", "")).strip()
        if not location:
            return "오류: 날씨를 조회할 장소가 필요합니다."
        try:
            queries = [location]
            queries.extend(english for korean, english in self.CITY_FALLBACKS.items() if korean in location)
            matches, geocoding_query = [], location
            for query in dict.fromkeys(queries):
                geo = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={
                    "name": query, "count": 1, "language": "ko", "format": "json",
                }, timeout=20)
                geo.raise_for_status()
                matches = geo.json().get("results") or []
                if matches:
                    geocoding_query = query
                    break
            if not matches:
                return f"오류: 장소를 찾을 수 없습니다: {location}"
            place = matches[0]
            forecast = requests.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": place["latitude"], "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto", "forecast_days": 1,
            }, timeout=20)
            forecast.raise_for_status()
            data = forecast.json()
            current, daily = data.get("current", {}), data.get("daily", {})
            result = {
                "source": "Open-Meteo", "requested_location": location,
                "geocoding_query": geocoding_query,
                "location_precision": "city" if geocoding_query != location else "exact",
                "resolved_location": ", ".join(filter(None, [place.get("name"), place.get("admin2"), place.get("admin1"), place.get("country")])),
                "observed_at": current.get("time"), "temperature_c": current.get("temperature_2m"),
                "apparent_temperature_c": current.get("apparent_temperature"),
                "humidity_percent": current.get("relative_humidity_2m"), "weather_code": current.get("weather_code"),
                "wind_speed_kmh": current.get("wind_speed_10m"),
                "today_min_c": (daily.get("temperature_2m_min") or [None])[0],
                "today_max_c": (daily.get("temperature_2m_max") or [None])[0],
                "precipitation_probability_percent": (daily.get("precipitation_probability_max") or [None])[0],
            }
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return f"오류: 날씨 조회 실패: {exc}"
