from types import SimpleNamespace

from core.dialogue_state import DialogueStateStore
from core.executor import Executor
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry


class RecordingLLM:
    def __init__(self, response="반가워! 오늘은 무슨 이야기부터 해볼까?"):
        self.response = response
        self.messages = None

    def chat(self, messages):
        self.messages = messages
        return self.response


def make_executor(tmp_path, voice="", address="보스"):
    executor = Executor.__new__(Executor)
    executor.llm = RecordingLLM()
    executor.tool_executor = SimpleNamespace(
        tts_settings=SimpleNamespace(
            selected_custom_voice=voice,
            selected_address=address,
        )
    )
    executor.dialogue_state = DialogueStateStore(str(tmp_path / "dialogue.db"))
    executor.intent_router = IntentRouter(PluginRegistry())
    executor._progress_callback = None
    executor.current_agent_task_id = ""
    executor._task_controls = {}
    return executor


def test_greeting_uses_conversation_path_without_planner(tmp_path):
    executor = make_executor(tmp_path)
    outcome = executor.execute_turn(
        "안녕",
        "chat-session",
        [{"role": "assistant", "content": "서울 날씨는 맑습니다."}],
    )
    assert outcome.response == "반가워! 오늘은 무슨 이야기부터 해볼까?"
    assert "일반 대화" in executor.llm.messages[0]["content"]
    assert executor.llm.messages[-1] == {"role": "user", "content": "안녕"}


def test_custom_voice_profile_enables_contextual_conversation_style(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.executor.load_custom_voice_profiles",
        lambda: [{
            "id": "Anis",
            "conversation_style": "평소에는 밝고 장난스럽게, 중요한 상황에서는 진지하게 답한다.",
        }],
    )
    executor = make_executor(tmp_path, voice="Anis", address="지휘관님")
    executor.execute_turn("오늘 좀 피곤하네", "anis-chat")
    system_prompt = executor.llm.messages[0]["content"]
    assert "밝고 장난스럽게" in system_prompt
    assert "중요한 상황에서는 진지하게" in system_prompt
    assert "호칭은 반드시 '지휘관님'" in system_prompt


def test_profile_assistant_name_is_treated_as_a_call_not_a_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.executor.load_custom_voice_profiles",
        lambda: [{"id": "Anis", "assistant_name": "아니스"}],
    )
    executor = make_executor(tmp_path, voice="Anis", address="지휘관님")

    outcome = executor.execute_turn(
        "아니스",
        "anis-call",
        [{"role": "assistant", "content": "이제 아니스로 불러줘."}],
    )

    assert outcome.response == "응, 듣고 있어. 지휘관님."
    assert executor.llm.messages is None
