import json
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

from core.scheduler import AutomationEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr("core.scheduler.Config.DB_PATH", str(tmp_path / "assistant.db"))
    scheduler = AutomationEngine()
    yield scheduler
    scheduler.stop()


@pytest.mark.parametrize("delay", [0, -1, True, 1.5, "5", None, 10 ** 30])
def test_alarm_rejects_invalid_delay_without_persisting(engine, delay):
    assert engine.add_alarm(delay).startswith("알람 등록 오류:")
    assert not engine.get_job_records()


def test_alarm_does_not_report_success_when_scheduler_cannot_start(engine, monkeypatch):
    monkeypatch.setattr(engine, "start", lambda: "자동화 엔진 시작 오류: 검증 실패")

    assert engine.add_alarm(300).startswith("알람 등록 오류:")
    assert not engine.get_job_records()


def test_failed_alarm_registration_cannot_fire_after_restart(engine, monkeypatch):
    def fail_registration(*args):
        raise ValueError("등록 실패")

    monkeypatch.setattr(engine, "_schedule_job", fail_registration)
    assert engine.add_alarm(300).startswith("알람 등록 오류:")
    assert not engine.get_job_records()
    assert not engine.scheduled_jobs


def test_invalid_legacy_job_cannot_prevent_restoring_alarm(engine):
    target = datetime.now().astimezone() + timedelta(minutes=5)
    with sqlite3.connect(engine.scheduler_db_path) as conn:
        conn.execute(
            "INSERT INTO jobs(description,schedule_type,schedule_value,prompt,created_at) "
            "VALUES('예전 작업', 'invalid', 'bad', '무시', ?)", (target.isoformat(),)
        )
        conn.execute(
            "INSERT INTO jobs(description,schedule_type,schedule_value,prompt,action_type,created_at) "
            "VALUES('알람', 'once_at', ?, '알람 시간입니다.', 'alarm', ?)",
            (target.isoformat(), target.isoformat()),
        )

    engine._load_jobs_from_db()
    assert engine._restored_jobs == len(engine.scheduled_jobs) == 1
    assert engine.scheduled_jobs[0]["action_type"] == "alarm"


def test_fired_alarm_is_disabled_and_does_not_fire_after_restart(engine):
    fired = threading.Event()
    received = []

    def callback(event):
        received.append(event)
        fired.set()

    engine.set_result_callback(callback)
    receipt = json.loads(engine.add_alarm(1, "복원 검증"))
    assert fired.wait(3)
    engine.stop()

    record = engine.get_job_record(receipt["job_id"])
    assert record["enabled"] == 0 and record["last_run"]
    assert not engine.scheduled_jobs
    assert len(engine.get_job_history_records(receipt["job_id"])) == 1

    restored = AutomationEngine()
    restored.set_result_callback(received.append)
    try:
        restored.start()
        assert not restored.scheduled_jobs
        restored.scheduler.run_all()
        assert len(received) == 1
    finally:
        restored.stop()


def test_overdue_alarm_restores_once_with_original_target_and_message(engine):
    target = datetime.now().astimezone() - timedelta(minutes=1)
    with sqlite3.connect(engine.scheduler_db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO jobs(description,schedule_type,schedule_value,prompt,action_type,created_at) "
            "VALUES(?, 'once_at', ?, ?, 'alarm', ?)",
            ("복원 알람", target.isoformat(), "복원된 알람입니다.", target.isoformat()),
        )
        job_id = cursor.lastrowid
    fired = threading.Event()
    received = []
    engine.set_result_callback(lambda event: (received.append(event), fired.set()))

    engine.start()
    assert fired.wait(3)
    engine.stop()
    assert len(received) == 1
    assert received[0]["result"] == "복원된 알람입니다."
    assert engine.get_job_record(job_id)["schedule_value"] == target.isoformat()
    assert engine.get_job_record(job_id)["enabled"] == 0


def test_scheduled_model_work_cannot_delay_alarm(engine, monkeypatch):
    model_busy = threading.Event()
    release_model = threading.Event()
    model_completed = threading.Event()
    alarm_fired = threading.Event()

    class SlowClient:
        def chat(self, messages):
            model_busy.set()
            assert release_model.wait(5)
            return "예약 작업 완료"

    monkeypatch.setattr("core.llm.get_llm_client", lambda: SlowClient())
    monkeypatch.setattr("core.memory.build_memory_context", lambda *args, **kwargs: "")

    def callback(event):
        if event["action_type"] == "alarm":
            alarm_fired.set()
        else:
            model_completed.set()

    engine.set_result_callback(callback)
    engine.add_job("느린 모델 작업", "every_minutes", "1", "응답 대기")
    engine.start()
    engine.scheduler.jobs[0].next_run = datetime.now() - timedelta(seconds=1)
    try:
        assert model_busy.wait(3)
        assert json.loads(engine.add_alarm(1))["status"] == "scheduled"
        assert alarm_fired.wait(3)
        assert not model_completed.is_set()
    finally:
        release_model.set()
        assert model_completed.wait(3)


def test_disabled_alarm_never_fires_from_stale_scheduler_job(engine):
    receipt = json.loads(engine.add_alarm(300))
    engine.stop()
    received = []
    engine.set_result_callback(received.append)
    with sqlite3.connect(engine.scheduler_db_path) as conn:
        conn.execute("UPDATE jobs SET enabled=0 WHERE id=?", (receipt["job_id"],))

    engine.scheduler.run_all()
    assert not received
    assert not engine.get_job_history_records(receipt["job_id"])


def test_reenabling_alarm_preserves_action_and_one_scheduler_entry(engine):
    receipt = json.loads(engine.add_alarm(300))
    engine.stop()
    job_id = receipt["job_id"]

    engine.toggle_job(job_id, False)
    engine.toggle_job(job_id, True)
    engine.toggle_job(job_id, True)

    assert len(engine.scheduled_jobs) == len(engine.scheduler.jobs) == 1
    assert engine.scheduled_jobs[0]["action_type"] == "alarm"


def test_result_storage_failure_releases_job_guard_for_retry(engine, monkeypatch):
    receipt = json.loads(engine.add_alarm(300))
    engine.stop()
    original_save = engine._save_job_result

    def fail_save(*args):
        raise sqlite3.OperationalError("busy")

    monkeypatch.setattr(engine, "_save_job_result", fail_save)

    with pytest.raises(sqlite3.OperationalError):
        engine.scheduler.run_all()
    assert not engine._running_jobs

    received = []
    engine.set_result_callback(received.append)
    monkeypatch.setattr(engine, "_save_job_result", original_save)
    engine.scheduler.run_all()
    assert len(received) == 1
    assert engine.get_job_record(receipt["job_id"])["enabled"] == 0
