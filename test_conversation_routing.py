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


def make_executor(tmp_path, voice=""):
    executor = Executor.__new__(Executor)
    executor.llm = RecordingLLM()
    executor.tool_executor = SimpleNamespace(
        tts_settings=SimpleNamespace(selected_custom_voice=voice)
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


def test_anis_voice_enables_bright_conversation_style(tmp_path):
    executor = make_executor(tmp_path, voice="Anis")
    executor.execute_turn("오늘 좀 피곤하네", "anis-chat")
    system_prompt = executor.llm.messages[0]["content"]
    assert "밝고 활기차며 솔직하고" in system_prompt
    assert "특정 작품의 대사" in system_prompt
