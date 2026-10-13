"""The chat timer receipt and persisted delay must agree without model rewriting."""
from datetime import datetime
import json

from core.response_realizer import ResponseRealizer
from core.scheduler import AutomationEngine
from plugins.alarm import AlarmPlugin
from test_semantic_executor_flow import make_executor


class NoModelCalls:
    def chat(self, *_args, **_kwargs):
        raise AssertionError("A simple alarm must not depend on model inference")


def test_chat_alarm_persists_exact_delay_and_does_not_rewrite_receipt(make_executor, tmp_path, monkeypatch):
    monkeypatch.setattr("core.scheduler.Config.DB_PATH", str(tmp_path / "assistant.db"))
    engine = AutomationEngine()
    monkeypatch.setattr("plugins.alarm.get_automation_engine", lambda: engine)
    executor = make_executor(model=NoModelCalls())
    executor.response_realizer = ResponseRealizer(NoModelCalls())
    executor.intent_router.registry.register_plugin(AlarmPlugin())
    try:
        before = datetime.now().astimezone()
        result = executor.execute_turn("5분뒤에 알람 맞춰줘", "alarm-flow")
        after = datetime.now().astimezone()
        assert result.status == "completed" and result.response_generated
        assert "5분 뒤에 알람을 울릴게요" in result.response
        receipt = json.loads(result.tool_result.raw_output)
        record = engine.get_job_record(receipt["job_id"])
        deadline = datetime.fromisoformat(record["schedule_value"])
        assert record["action_type"] == "alarm" and record["enabled"] == 1
        assert 300 <= (deadline - before).total_seconds()
        assert (deadline - after).total_seconds() <= 300
        assert receipt["delay_seconds"] == 300 and not executor.conversation_calls
    finally:
        engine.stop()
