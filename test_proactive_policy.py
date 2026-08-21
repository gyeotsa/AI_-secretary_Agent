from pathlib import Path

from core.proactive import ProactiveNotificationPolicy
from core.runtime.event_bus import Event


def test_important_file_event_notifies_once_within_debounce(tmp_path):
    messages = []
    current_time = [100.0]
    policy = ProactiveNotificationPolicy(messages.append, 30, lambda: current_time[0])
    event = Event("file_modified", "test", data={"path": str(Path("C:/workspace/report.xlsx"))})

    assert policy.handle_event(event)
    assert policy.handle_event(event) is None
    current_time[0] += 31
    assert policy.handle_event(event)
    assert len(messages) == 2


def test_cache_and_unimportant_files_are_silent(tmp_path):
    messages = []
    policy = ProactiveNotificationPolicy(messages.append)

    assert policy.handle_event(Event(
        "file_created", "test", data={"path": str(tmp_path / ".pytest_cache" / "note.md")}
    )) is None
    assert policy.handle_event(Event(
        "file_modified", "test", data={"path": str(tmp_path / "image.png")}
    )) is None
    assert messages == []
