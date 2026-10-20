import json
import sqlite3

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from core.dialogue_state import DialogueStateStore
from core.memory import ConversationMemory, EpisodeMemoryManager
from ui.main_window import SessionManagerDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_empty_session_is_listed_and_can_be_reset_and_deleted(tmp_path):
    memory = ConversationMemory(str(tmp_path / "memory.db"))
    session_id = memory.create_session("업무 대화")
    assert memory.list_session_details()[0]["title"] == "업무 대화"
    assert memory.list_session_details()[0]["message_count"] == 0

    memory.save_message(session_id, "user", "안녕")
    memory.save_message(session_id, "assistant", "안녕하세요")
    assert len(memory.load_session(session_id)) == 2
    assert memory.clear_session(session_id) is True
    assert memory.load_session(session_id) == []
    assert memory.delete_session(session_id) is True
    assert memory.list_session_details() == []


def test_chat_name_and_pin_persist_without_changing_conversation(tmp_path):
    db_path = str(tmp_path / "memory.db")
    memory = ConversationMemory(db_path)
    session_id = memory.create_session("이전 이름")
    memory.save_message(session_id, "user", "남아 있어야 할 질문")
    before = memory.list_session_details()[0]

    assert memory.rename_session(session_id, "  바꾼 이름  ") is True
    assert memory.set_session_pinned(session_id, True) is True

    restored = ConversationMemory(db_path)
    session = restored.list_session_details()[0]
    assert session["title"] == "바꾼 이름"
    assert session["is_pinned"] is True
    assert session["start_time"] == before["start_time"]
    assert session["end_time"] == before["end_time"]
    assert restored.load_session(session_id) == [{"role": "user", "content": "남아 있어야 할 질문"}]
    with pytest.raises(ValueError, match="대화 이름"):
        restored.rename_session(session_id, "  ")
    assert restored.list_session_details()[0]["title"] == "바꾼 이름"
    assert restored.rename_session("missing", "이름") is False
    assert restored.set_session_pinned("missing", True) is False

    assert restored.set_session_pinned(session_id, False) is True
    assert ConversationMemory(db_path).list_session_details()[0]["is_pinned"] is False


def test_all_project_sessions_keep_scoped_defaults_and_pin_first_order(tmp_path):
    memory = ConversationMemory(str(tmp_path / "memory.db"))
    memory.set_namespace("workspace-a")
    oldest = memory.create_session("A 이전 대화")
    newer = memory.create_session("A 최근 대화")
    memory.set_namespace("workspace-b")
    newest = memory.create_session("B 대화")
    with sqlite3.connect(memory.episode_manager.db_path) as conn:
        for updated_at, session_id in enumerate((oldest, newer, newest), start=1):
            conn.execute(
                "UPDATE conversation_sessions SET updated_at = ? WHERE session_id = ?",
                (updated_at, session_id),
            )
    memory.set_session_pinned(oldest, True)

    assert [s["session_id"] for s in memory.list_session_details()] == [newest]
    assert [s[0] for s in memory.list_sessions()] == [newest]
    all_sessions = memory.list_session_details(all_workspaces=True)
    assert [s["session_id"] for s in all_sessions] == [oldest, newest, newer]
    assert [s["workspace_namespace"] for s in all_sessions] == ["workspace-a", "workspace-b", "workspace-a"]
    assert [s["session_id"] for s in memory.episode_manager.list_sessions()] == [oldest, newest, newer]

    memory.set_namespace("workspace-a")
    assert [s["session_id"] for s in memory.list_session_details()] == [oldest, newer]
    memory.set_session_pinned(oldest, False)
    assert [s[0] for s in memory.list_sessions()] == [newer, oldest]


def test_legacy_session_table_migrates_without_losing_titles(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE conversation_sessions (session_id TEXT PRIMARY KEY, "
                     "title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("INSERT INTO conversation_sessions VALUES ('legacy', '기존 이름', 1, 2)")

    manager = EpisodeMemoryManager(db_path)
    session = manager.list_sessions()[0]
    assert session["title"] == "기존 이름"
    assert session["workspace_namespace"] == "global"
    assert session["is_pinned"] is False
    assert manager.rename_session("legacy", "새 이름") is True
    assert manager.set_session_pinned("legacy", True) is True
    restored = EpisodeMemoryManager(db_path).list_sessions()[0]
    assert restored["title"] == "새 이름"
    assert restored["is_pinned"] is True


def test_detaching_project_preserves_chats_in_general_conversations_after_reopen(tmp_path):
    db_path = str(tmp_path / "memory.db")
    memory = ConversationMemory(db_path)
    memory.set_namespace("workspace-a")
    first = memory.create_session("A 첫 대화")
    empty = memory.create_session("A 빈 대화")
    memory.save_message(first, "user", "남길 질문")
    memory.save_message(first, "assistant", "남길 답변")
    memory.set_session_pinned(first, True)
    before = memory.list_session_details()
    memory.set_namespace("workspace-b")
    other = memory.create_session("B 대화")
    memory.save_message(other, "user", "다른 프로젝트 질문")

    assert memory.detach_workspace("workspace-a") == 2
    assert memory.detach_workspace("workspace-a") == 0
    assert memory.detach_workspace("global") == 0
    restored = ConversationMemory(db_path)
    moved = restored.list_session_details()
    assert {s["session_id"] for s in moved} == {first, empty}
    for previous, current in zip(before, moved):
        assert current == {**previous, "workspace_namespace": "global"}
    assert restored.load_session(first) == [
        {"role": "user", "content": "남길 질문"},
        {"role": "assistant", "content": "남길 답변"},
    ]
    assert all(e.metadata["workspace_namespace"] == "global"
               for e in restored.episode_manager.get_session_episodes(first))
    restored.set_namespace("workspace-a")
    assert restored.list_session_details() == []
    restored.set_namespace("workspace-b")
    assert [s["session_id"] for s in restored.list_session_details()] == [other]
    assert restored.episode_manager.get_session_episodes(other)[0].metadata == {
        "workspace_namespace": "workspace-b"
    }


def test_detach_preserves_other_metadata_and_unreadable_legacy_metadata(tmp_path):
    memory = ConversationMemory(str(tmp_path / "memory.db"))
    memory.set_namespace("workspace-a")
    session_id = memory.create_session("기존 대화")
    for text in ("정상 메타데이터", "손상된 메타데이터", "이전 형식"):
        memory.save_message(session_id, "user", text)
    original_metadata = [
        json.dumps({"workspace_namespace": "workspace-a", "source": "가져온 대화"}),
        "{unreadable legacy metadata",
        '["legacy", "metadata"]',
    ]
    with sqlite3.connect(memory.episode_manager.db_path) as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM episodes ORDER BY id")]
        conn.executemany("UPDATE episodes SET metadata = ? WHERE id = ?",
                         zip(original_metadata, ids))

    assert memory.detach_workspace("workspace-a") == 1

    with sqlite3.connect(memory.episode_manager.db_path) as conn:
        metadata = [row[0] for row in conn.execute("SELECT metadata FROM episodes ORDER BY id")]
    assert json.loads(metadata[0]) == {"workspace_namespace": "global", "source": "가져온 대화"}
    assert metadata[1:] == original_metadata[1:]


def test_dialogue_reset_removes_pending_task_and_intent(tmp_path):
    store = DialogueStateStore(str(tmp_path / "dialogue.db"))
    task = store.create_task("session-a", "파일 생성")
    store.create("session-a", "파일 생성", "이름은?", [], task.task_id)
    store.save_intent_state(task.task_id, "session-a", "file.create", {}, "파일 생성")
    store.save_recent_intent("session-a", "file.create", {}, "파일 생성")

    store.clear_session("session-a")

    assert store.list("session-a") == []
    assert store.list_tasks("session-a") == []
    assert store.get_intent_state(task.task_id) is None
    assert store.get_recent_intent("session-a") is None


def test_session_dialog_shows_full_conversation(app, tmp_path):
    memory = ConversationMemory(str(tmp_path / "memory.db"))
    session_id = memory.create_session("테스트")
    memory.save_message(session_id, "user", "첫 질문")
    memory.save_message(session_id, "assistant", "첫 답변")

    dialog = SessionManagerDialog(memory, session_id)

    assert dialog.session_list.count() == 1
    assert "첫 질문" in dialog.history.toPlainText()
    assert "첫 답변" in dialog.history.toPlainText()
    dialog.close()
