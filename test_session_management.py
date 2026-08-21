import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from core.dialogue_state import DialogueStateStore
from core.memory import ConversationMemory
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
