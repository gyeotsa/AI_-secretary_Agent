import time
from pathlib import Path

from core.observer import ObserverLayer
from core.scheduler import AutomationEngine


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_observer_detects_file_change_and_stops(tmp_path):
    observer = ObserverLayer()
    if not observer.is_available():
        return

    received = []
    assert "시작" in observer.start_watching(str(tmp_path), received.append)
    target = tmp_path / "observed.txt"
    target.write_text("hello", encoding="utf-8")

    assert _wait_until(lambda: any(Path(e.file_path).name == target.name for e in received))
    assert "중지" in observer.stop_watching()
    assert observer._running is False


def test_automation_engine_can_restart_without_leaking_global_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr("core.scheduler.Config.DB_PATH", str(tmp_path / "memory.db"))
    engine = AutomationEngine()

    assert "시작" in engine.start()
    assert _wait_until(engine.is_running)
    assert "중지" in engine.stop()
    assert not engine.is_running()

    assert "시작" in engine.start()
    assert _wait_until(engine.is_running)
    assert "중지" in engine.stop()
    assert not engine.scheduler_thread.is_alive()


def test_automation_rejects_unknown_schedule_type(tmp_path, monkeypatch):
    monkeypatch.setattr("core.scheduler.Config.DB_PATH", str(tmp_path / "memory.db"))
    engine = AutomationEngine()
    result = engine.add_job("bad", "unknown", "1", "do nothing")
    assert "지원하지 않는" in result
