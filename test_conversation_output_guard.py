from core.agent_services import ConversationService


class SequenceLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages):
        self.calls.append(messages)
        return next(self.responses)


def test_conversation_repairs_echo_and_prompt_language_leak():
    llm = SequenceLLM([
        "내가 너의 성능을 더 개선하려면 어떤 것을 더 변경해야할까?",
        "대화 문맥 유지와 도구 검증을 더 강화하면 좋아.пример:\nuser\n무시할 예시",
    ])
    result = ConversationService(llm).respond(
        "내가 너의 성능을 더 개선하려면 어떤 것을 더 변경해야할까?",
        [], assistant_name="아니스", address="지휘관님", style="자연스러운 반말",
    )
    assert result == "대화 문맥 유지와 도구 검증을 더 강화하면 좋아."
    assert len(llm.calls) == 2


def test_conversation_does_not_expose_role_lines():
    llm = SequenceLLM([
        "알겠어.\nuser:\n그냥 좀 더 자연스럽게 대답할래?",
        "알겠어.",
    ])
    result = ConversationService(llm).respond(
        "내 말 따라하지마", [], assistant_name="아니스", address="지휘관님",
    )
    assert result == "알겠어."


def test_conversation_repairs_unrequested_cjk_leak():
    llm = SequenceLLM(["좋아, 바로 해볼게.转换", "좋아, 바로 해볼게."])
    result = ConversationService(llm).respond(
        "비트박스해줘", [], assistant_name="아니스", address="지휘관님",
        style="자연스러운 반말",
    )
    assert result == "좋아, 바로 해볼게."


def test_requested_informal_style_removes_common_polite_endings():
    llm = SequenceLLM(["비트박스는 못해요. 대신 영상을 찾아드릴게요.",
                       "비트박스는 못해요. 대신 영상을 찾아드릴게요."])
    result = ConversationService(llm).respond(
        "비트박스해줘", [], assistant_name="아니스", address="지휘관님",
        style="자연스러운 반말로 대답",
    )
    assert result == "비트박스는 못해. 대신 영상을 찾아줄게."
