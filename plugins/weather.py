"""Open-Meteo 기반 실제 현재 날씨 조회 플러그인."""
from typing import Any, Dict, List
import json
import re
import requests

from core.plugin import BasePlugin, ToolSchema, IntentSchema, SlotSchema


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

    def get_intents(self) -> List[IntentSchema]:
        return [IntentSchema(
            "weather.current", "실시간 현재 날씨 조회", "get_weather",
            ["날씨", "기온", "온도", "습도", "비 와", "비가 와"],
            [SlotSchema("location", "조회할 장소", "어느 지역의 날씨를 확인할까요, 보스?")],
            execution_hints=["날씨", "기온", "온도", "습도", "비"],
            follow_up_hints=["거기는", "그곳은", "지금은", "오늘은"],
        )]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name != "weather.current":
            return slots
        candidate = re.sub(
            r"(오늘|지금|현재|실시간|날씨|기온|온도|습도|비가?|오는지|어때|어떻게|"
            r"알려\s*줘|알려줘|말해\s*줘|말해줘|확인해\s*줘|확인해줘|몇\s*도|야|요)",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(r"(?<!\S)(?:은|는|이|가|에서|의|와|과)(?!\S)", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" ?!.,")
        if candidate and candidate not in {"여기", "이곳", "우리 동네"}:
            slots["location"] = candidate
        return slots

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

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name != "get_weather" or result.startswith("오류:"):
            return result
        data = json.loads(result)
        weather_labels = {
            0: "맑음", 1: "대체로 맑음", 2: "부분적으로 흐림", 3: "흐림",
            45: "안개", 48: "서리 안개", 51: "약한 이슬비", 53: "이슬비",
            55: "강한 이슬비", 61: "약한 비", 63: "비", 65: "강한 비",
            71: "약한 눈", 73: "눈", 75: "강한 눈", 80: "약한 소나기",
            81: "소나기", 82: "강한 소나기", 95: "뇌우",
        }
        condition = weather_labels.get(data.get("weather_code"), "관측 정보 확인")
        observed_at = str(data.get("observed_at") or "")
        if "T" in observed_at:
            observed_at = observed_at.rsplit("T", 1)[-1]
        precision = (
            " 정확한 동 단위 관측소 값이 아닌 도시 기준 근삿값입니다."
            if data.get("location_precision") == "city" else ""
        )
        return (
            f"{data.get('requested_location')}의 현재 날씨는 {condition}, "
            f"기온 {data.get('temperature_c')}도, 체감 {data.get('apparent_temperature_c')}도, "
            f"습도 {data.get('humidity_percent')}퍼센트입니다. "
            f"오늘 최저 {data.get('today_min_c')}도, 최고 {data.get('today_max_c')}도이며 "
            f"관측 시각은 {observed_at}입니다, 보스.{precision}"
        )
