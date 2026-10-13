"""Literal alarm requests must schedule the user's duration without inference."""
import json
from types import SimpleNamespace

import pytest

from core.plugin import PluginRegistry
from core.semantic_request import SemanticRequestInterpreter
from plugins.alarm import AlarmPlugin


class NoModel:
    def chat(self, *args, **kwargs):
        raise AssertionError("A relative alarm must not need a model call")


@pytest.fixture
def interpreter():
    registry = PluginRegistry()
    registry.register_plugin(AlarmPlugin())
    return SemanticRequestInterpreter(NoModel(), registry)


@pytest.mark.parametrize("text,seconds", [
    ("5분뒤에 알람 맞춰줘", 300),
    ("10초 뒤에 알람 맞춰줘", 10),
    ("1시간 30분 뒤에 타이머 설정해줘", 5400),
    ("1시간30분 뒤에 타이머 설정해줘", 5400),
    ("한 시간 30분 5초 후에 알람 울려줘", 5405),
    ("한 시간 반 있다가 깨워줘", 5400),
    ("다섯 분 뒤에 깨워줘", 300),
    ("0.5분 뒤에 알람 맞춰줘", 30),
    ("지금부터 1시간 5분 뒤에 알려줘", 3900),
    ("타이머 5분 맞춰줘", 300),
    ("타이머5분 맞춰줘", 300),
])
def test_relative_alarm_uses_exact_complete_duration_without_model(interpreter, text, seconds):
    decision = interpreter.interpret(text)
    assert decision.source == "alarm_contract"
    assert decision.grounded and not decision.needs_clarification
    assert decision.slots == {"delay_seconds": seconds}
    assert decision.to_resolution(interpreter.registry).ready


@pytest.mark.parametrize("text", [
    "알람 맞춰줘", "0초 뒤에 알람 맞춰줘", "-1분 뒤에 알람 맞춰줘",
    "-1시간 30분 뒤에 알람 맞춰줘", "0.5초 뒤에 알람 맞춰줘",
    "5분~10분 뒤에 알람 맞춰줘", "약 5분 뒤에 알람 맞춰줘",
    "내일 오후 5시 30분에 알람 맞춰줘",
    "열한 분 뒤에 알람 맞춰줘", "열다섯분 뒤에 알람 맞춰줘",
    "스물한분 뒤에 알람 맞춰줘", "1e3분 뒤에 알람 맞춰줘",
    "5분마다 알람 맞춰줘", "매일 5분 뒤에 알람 맞춰줘",
    "5분 전으로 알람 맞춰줘", "이틀 뒤에 5분 알람 맞춰줘",
])
def test_missing_invalid_or_absolute_delay_asks_instead_of_guessing(interpreter, text):
    decision = interpreter.interpret(text)
    assert decision.source == "alarm_contract"
    assert decision.needs_clarification
    assert "delay_seconds" not in decision.slots
    assert not decision.to_resolution(interpreter.registry).ready


def test_alarm_duration_followup_and_new_alarm_do_not_share_slots(interpreter):
    first = interpreter.interpret("알람 맞춰줘 메시지: 약 먹기")
    pending = {"intent_name": first.intent_name, "slots": first.slots,
               "question": first.clarification_question}
    followup = interpreter.interpret("5분 뒤", pending=pending)
    assert followup.relation == "continue"
    assert followup.slots == {"delay_seconds": 300, "message": "약 먹기"}
    new = interpreter.interpret("10초 뒤에 알람 맞춰줘", pending=pending)
    assert new.relation == "new" and new.slots == {"delay_seconds": 10}


def test_alarm_message_is_preserved_and_its_duration_is_not_added(interpreter):
    decision = interpreter.interpret("5분 뒤에 알람 맞춰줘 메시지: 10분 동안 쉬기")
    assert decision.slots == {"delay_seconds": 300, "message": "10분 동안 쉬기"}


@pytest.mark.parametrize("text", [
    "'5분 뒤에 알람 맞춰줘'라는 문장을 설명해줘",
    "5분 뒤에 알람 맞추지 마", "회의가 끝나면 5분 뒤에 알람 맞춰줘",
    "파일 만들어줘 그리고 5분 뒤에 알람 맞춰줘",
])
def test_alarm_fast_path_respects_current_speech_scope(interpreter, text):
    decision = SemanticRequestInterpreter(None, interpreter.registry).interpret(text)
    assert decision.source != "alarm_contract"
    assert not decision.to_resolution(interpreter.registry).ready


def test_alarm_respects_tool_scope(interpreter):
    decision = SemanticRequestInterpreter(None, interpreter.registry).interpret(
        "5분 뒤에 알람 맞춰줘", allowed_tools=[])
    assert not decision.grounded


@pytest.mark.parametrize("delay", [True, False, 0, -1, 1.5, "60", None])
def test_invalid_direct_tool_delay_never_reaches_scheduler(monkeypatch, delay):
    def forbidden():
        raise AssertionError("Invalid input must not reach the scheduler")
    monkeypatch.setattr("plugins.alarm.get_automation_engine", forbidden)
    assert not AlarmPlugin().execute_tool("set_alarm", {"delay_seconds": delay}).succeeded


def test_receipt_accepts_successful_alarm_already_fired(monkeypatch):
    payload = {"job_id": 42, "delay_seconds": 1, "fire_at": "2026-10-12T13:00:01"}
    record = {"id": 42, "action_type": "alarm", "schedule_type": "once_at",
              "schedule_value": payload["fire_at"], "prompt": "알람 시간입니다.",
              "enabled": 0, "last_run": "2026-10-12T13:00:01"}
    engine = SimpleNamespace(add_alarm=lambda *args: json.dumps(payload), scheduled_jobs=[],
                             get_job_record=lambda job_id: record, is_running=lambda: True)
    monkeypatch.setattr("plugins.alarm.get_automation_engine", lambda: engine)
    result = AlarmPlugin().execute_tool("set_alarm", {"delay_seconds": 1})
    assert result.succeeded and result.evidence[0].data["job_id"] == 42


def test_combined_duration_receipt_is_readable():
    response = AlarmPlugin().present_result("set_alarm", json.dumps({"delay_seconds": 5405}))
    assert response == "1시간 30분 5초 뒤에 알람을 울릴게요, 보스."
