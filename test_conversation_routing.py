from types import SimpleNamespace

from core.dialogue_state import DialogueStateStore
from core.executor import Executor
from core.intent_router import IntentRouter
from core.plugin import BasePlugin, PluginRegistry, ToolSchema


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

    assert outcome.response == executor.llm.response
    assert executor.llm.messages[-1] == {"role": "user", "content": "아니스"}
    assert "지휘관님" in executor.llm.messages[0]["content"]
    assert not executor.dialogue_state.list("anis-call")


def test_conversation_path_blocks_unexecuted_completion_claim(tmp_path):
    executor = make_executor(tmp_path)
    executor.llm.response = "요청하신 파일을 생성했습니다."

    outcome = executor.execute_turn("새 형식의 파일을 만들어줘", "claim-guard")

    assert "실제 작업을 실행하지 않았습니다" in outcome.response
    assert "완료로 보고하지 않겠습니다" in outcome.response
    assert executor.llm.messages is None


def test_conversation_guard_does_not_rewrite_ordinary_chat(tmp_path):
    executor = make_executor(tmp_path)
    executor.llm.response = "정말 잘 마무리했네. 고생했어!"

    outcome = executor.execute_turn("오늘 숙제를 다 끝냈어", "ordinary-chat")

    assert outcome.response == "정말 잘 마무리했네. 고생했어!"


class DescriptorOnlyPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "descriptor_only"

    def get_tools(self):
        return [ToolSchema(
            "inspect_presentation_format",
            "프레젠테이션 서식과 슬라이드 구성을 검사합니다",
            {"type": "object", "properties": {}},
            side_effect="read",
        )]

    def execute_tool(self, tool_name, tool_input):
        return "검사 결과"


def test_unmatched_action_uses_registry_descriptors_but_social_chat_does_not(tmp_path):
    executor = make_executor(tmp_path)
    registry = PluginRegistry()
    registry.register_plugin(DescriptorOnlyPlugin())
    executor.intent_router = IntentRouter(registry)

    assert executor._should_attempt_registry_execution("프레젠테이션 서식을 검사해줘") is True
    assert executor._should_attempt_registry_execution("프레젠테이션 서식 검토 부탁해") is True
    assert executor._should_attempt_registry_execution("안녕, 오늘 기분은 어때?") is False
