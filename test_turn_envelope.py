from types import SimpleNamespace

import main_qt


class _Window:
    def set_assistant_identity(self, _name):
        pass

    def show_assistant_text(self, _text):
        pass

    def show_specialist_result(self, _text, _key):
        pass


class _Memory:
    def __init__(self):
        self.saved = []

    def save_message(self, session_id, role, content):
        self.saved.append((session_id, role, content))


class _StateMachine:
    def start_responding(self):
        pass


class _Thread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def _bare_app(monkeypatch):
    app = main_qt.JarvisApp.__new__(main_qt.JarvisApp)
    app.window = _Window()
    app.memory = _Memory()
    app.messages = []
    app.session_id = "current-session"
    app.last_response = ""
    app._is_processing_ai = True
    app.workspace_manager = None
    app.state_machine = _StateMachine()
    app._personalize_address = lambda text: text
    app._archived = []
    app._consolidated = []
    app._archive_obsidian_exchange_async = (
        lambda user, assistant, *, session_id="":
        app._archived.append((session_id, user, assistant))
    )
    app._consolidate_memory_async = (
        lambda user, assistant, *, session_id="", namespace="":
        app._consolidated.append((session_id, user, assistant, namespace))
    )
    monkeypatch.setattr(
        main_qt, "present_channels",
        lambda text, _request: SimpleNamespace(technical_text=text, screen_text=text),
    )
    monkeypatch.setattr(
        main_qt, "get_assistant_settings",
        lambda: SimpleNamespace(assistant_name="Anis"),
    )
    monkeypatch.setattr(main_qt.threading, "Thread", _Thread)
    return app


def test_out_of_order_results_keep_exact_turn_attribution(monkeypatch):
    app = _bare_app(monkeypatch)
    first = main_qt.TurnEnvelope(
        "turn-1", "session-a", "첫 요청", tuple(), "document", "workspace-a",
    )
    second = main_qt.TurnEnvelope(
        "turn-2", "session-b", "둘째 요청", tuple(), "coding", "workspace-b",
    )

    app._on_ai_response(main_qt.TurnResult(second, "둘째 응답"))
    app._on_ai_response(main_qt.TurnResult(first, "첫 응답"))

    assert app.memory.saved == [
        ("session-b", "user", "둘째 요청"),
        ("session-b", "assistant", "둘째 응답"),
        ("session-a", "user", "첫 요청"),
        ("session-a", "assistant", "첫 응답"),
    ]
    assert app._archived == [
        ("session-b", "둘째 요청", "둘째 응답"),
        ("session-a", "첫 요청", "첫 응답"),
    ]
    assert app._consolidated == [
        ("session-b", "둘째 요청", "둘째 응답", "workspace-b"),
        ("session-a", "첫 요청", "첫 응답", "workspace-a"),
    ]


def test_failed_model_turn_is_not_consolidated(monkeypatch):
    app = _bare_app(monkeypatch)
    turn = main_qt.TurnEnvelope("turn-f", "session-a", "요청", tuple())

    app._on_ai_response(main_qt.TurnResult(
        turn, "AI 모델 응답 시간이 초과되었습니다.", "failed", "timeout",
    ))

    assert app._consolidated == []
    assert app._archived == [
        ("session-a", "요청", "AI 모델 응답 시간이 초과되었습니다."),
    ]
