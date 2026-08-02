import json
import sqlite3
import time
from datetime import datetime, timedelta

from core.proactive import ProactiveNotificationPolicy
from core.proactive_runtime import InterruptionContextManager, ProactiveStore
from core.runtime.event_bus import Event
from core.scheduler import AutomationEngine
from plugins.proactive_policy import ProactivePolicyPlugin


def _policy(tmp_path, messages, **context):
    manager = InterruptionContextManager(str(tmp_path / "context.json"))
    if context:
        manager.update(**context)
    store = ProactiveStore(str(tmp_path / "proactive.db"))
    return ProactiveNotificationPolicy(messages.append, debounce_seconds=30,
                                       store=store, context=manager), store, manager


def test_focus_mode_defers_and_digest_explains_why(tmp_path):
    messages = []
    policy, store, context = _policy(tmp_path, messages, focus_mode=True)
    event = Event("file_modified", "observer", data={"path": "C:/workspace/report.xlsx"})
    result = policy.handle_event(event)
    assert result.startswith("보류됨:") and not messages
    pending = store.pending()
    assert len(pending) == 1 and pending[0]["why"] and pending[0]["evidence"]["event_id"]
    context.update(focus_mode=False)
    digest = policy.flush_digest()
    assert "알림 1건" in digest and "감시 중인 업무 파일" in digest
    assert len(messages) == 1 and not store.pending()


def test_critical_notification_bypasses_do_not_disturb(tmp_path):
    messages = []
    policy, store, _ = _policy(tmp_path, messages, do_not_disturb=True, meeting=True)
    result = policy.handle_event(Event("security_alert", "security", data={
        "title": "권한 위반", "message": "차단된 실행 요청이 있습니다.",
        "why": "허용 경로 밖 쓰기 시도가 감지되었습니다.", "severity": "critical",
    }))
    assert "알림 이유:" in result and messages
    assert not store.pending()


def test_duplicate_events_are_suppressed_before_queue(tmp_path):
    messages = []
    clock = [10.0]
    policy, store, _ = _policy(tmp_path, messages)
    policy.clock = lambda: clock[0]
    event = Event("file_modified", "observer", data={"path": "C:/workspace/code.py"})
    assert policy.handle_event(event)
    assert policy.handle_event(event) is None
    assert len(messages) == 1


def test_proposal_approval_never_executes_target_tool(tmp_path):
    plugin = ProactivePolicyPlugin()
    plugin.store = ProactiveStore(str(tmp_path / "proactive.db"))
    proposed = plugin.execute_tool("proactive_create_proposal", {
        "description": "보고서를 팀에 전송", "tool_name": "slack_send",
        "tool_input": {"channel": "C1", "text": "완료"}, "reason": "마감 시간이 임박했습니다.",
    })
    assert proposed.succeeded
    proposal_id = proposed.evidence[0].data["proposal_id"]
    assert plugin.store.get_proposal(proposal_id)["status"] == "proposed"
    approved = plugin.execute_tool("proactive_approve_proposal", {"proposal_id": proposal_id})
    assert approved.succeeded and approved.evidence[0].data["execution_required"] is True
    assert plugin.store.get_proposal(proposal_id)["status"] == "approved"


def test_scheduler_restores_enabled_jobs_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.DB_PATH", str(tmp_path / "main.db"))
    first = AutomationEngine()
    target = datetime.now().astimezone() + timedelta(minutes=5)
    with sqlite3.connect(first.scheduler_db_path) as conn:
        conn.execute("INSERT INTO jobs(description,schedule_type,schedule_value,prompt,action_type,created_at) "
                     "VALUES(?, 'once_at', ?, ?, 'alarm', ?)",
                     ("복원 알람", target.isoformat(), "복원됨", datetime.now().isoformat()))
    second = AutomationEngine()
    second._load_jobs_from_db()
    diagnostics = second.runtime_diagnostics()
    assert diagnostics["restored_jobs"] == 1 and diagnostics["scheduled_jobs"] == 1
    assert "last_restore" in diagnostics["state"]


def test_scheduler_heartbeat_and_accelerated_soak(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.DB_PATH", str(tmp_path / "main.db"))
    engine = AutomationEngine()
    result = engine.soak_test(5000)
    assert result["iterations"] == 5000 and not result["failures"]
    assert "시작" in engine.start()
    time.sleep(1.2)
    health = engine.runtime_diagnostics()
    engine.stop()
    assert health["running"] and health["thread_alive"] and "heartbeat" in health["state"]


def test_scheduler_detects_sleep_gap_and_reloads_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.DB_PATH", str(tmp_path / "main.db"))
    engine = AutomationEngine()
    calls = []
    original = engine._load_jobs_from_db

    def tracked_load():
        calls.append(True)
        return original()

    engine._load_jobs_from_db = tracked_load
    engine.start()
    engine._last_tick_monotonic = time.monotonic() - 20
    time.sleep(1.2)
    engine.stop()
    assert len(calls) >= 2  # initial boot restore plus sleep-gap restore


def test_plugin_contract_separates_read_manage_and_execution_permissions():
    tools = {tool.name: tool for tool in ProactivePolicyPlugin().get_tools()}
    assert tools["interruption_status"].required_permissions == ["proactive_read"]
    assert tools["interruption_update"].required_permissions == ["proactive_manage"]
    assert tools["proactive_approve_proposal"].required_permissions == ["automation"]
    assert tools["scheduler_accelerated_soak"].side_effect == "execute"


def test_context_change_event_flushes_deferred_digest(tmp_path):
    messages = []
    policy, store, context = _policy(tmp_path, messages, meeting=True)
    policy.handle_event(Event("task_completed", "executor", data={
        "message": "장기 작업이 끝났습니다.", "why": "완료 상태와 산출물이 검증되었습니다."
    }))
    assert store.pending() and not messages
    context.update(meeting=False)
    result = policy.handle_event(Event("interruption_context_changed", "settings"))
    assert result and messages and not store.pending()
