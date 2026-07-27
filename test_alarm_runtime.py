import json
import threading

from config import Config
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.response_presenter import present_response
from core.scheduler import AutomationEngine
from core.tts_normalizer import normalize_for_tts
from plugins.alarm import AlarmPlugin


def test_pending_task_id_is_hidden_from_normal_user_response():
    response = "어느 지역의 날씨를 확인할까요, 보스?\n대기 작업 ID: 272abd7e"

    assert present_response(response, "오늘 날씨 알려줘") == (
        "어느 지역의 날씨를 확인할까요, 보스?"
    )
    assert "272abd7e" in present_response(response, "대기 작업 ID를 자세히 알려줘")


def test_real_acronym_id_is_pronounced_as_korean_letter_names():
    assert normalize_for_tts("작업 ID와 GAME") == "작업 아이디와 게임"


def test_alarm_intent_converts_relative_minutes_to_seconds():
    registry = PluginRegistry()
    registry.register_plugin(AlarmPlugin())
    resolution = IntentRouter(registry).resolve("3분 뒤에 알람 맞춰줘")

    assert resolution.ready
    assert resolution.tool_name == "set_alarm"
    assert resolution.slots["delay_seconds"] == 180


def test_one_shot_alarm_fires_and_disables_itself(tmp_path):
    original_db_path = Config.API_CONFIG.DB_PATH
    engine = None
    try:
        Config.API_CONFIG.DB_PATH = str(tmp_path / "assistant.db")
        engine = AutomationEngine()
        received = []
        fired = threading.Event()

        def callback(event):
            received.append(event)
            fired.set()

        engine.set_result_callback(callback)
        result = json.loads(engine.add_alarm(1, "테스트 알람 시간입니다."))

        assert result["status"] == "scheduled"
        assert engine.is_running()
        assert fired.wait(3)
        assert received[0]["action_type"] == "alarm"
        assert received[0]["result"] == "테스트 알람 시간입니다."
    finally:
        if engine is not None:
            engine.stop()
        Config.API_CONFIG.DB_PATH = original_db_path
