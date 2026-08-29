import os
from datetime import date
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from core.permission import PermissionManager
from main_qt import AppSignals, JarvisApp
from ui.main_window import PermissionRequestDialog


_QT_APP = None


def _app():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def _pump_until(predicate, timeout=5.0):
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Window:
    def __init__(self):
        self.users = []
        self.assistants = []

    def show_user_text(self, text):
        self.users.append(text)

    def show_assistant_text(self, text):
        self.assistants.append(text)

    def set_soundbar_speaking(self, _value):
        pass

    def set_soundbar_audio_level(self, _value):
        pass

    def reset_soundbar(self):
        pass


class _StateMachine:
    def start_listening(self):
        pass

    def start_processing(self):
        pass

    def start_responding(self):
        pass

    def go_idle(self):
        pass


class _Rag:
    def search_docs(self, _text):
        raise RuntimeError("RAG is intentionally unavailable in this focused test")


class _Executor:
    def execute_goal(self, _text, _session_id, _conversation_history=None):
        # This test validates the GUI thread round trip, not cold Plugin Registry
        # startup. Depending on a process-global ToolExecutor made the result
        # order-dependent and could exceed the UI pump timeout on a cold run.
        return f"오늘 날짜는 {date.today().isoformat()}입니다."


class _ControllableExecutor(_Executor):
    queued = []

    @staticmethod
    def is_control_command(text):
        return text.endswith("취소")

    @staticmethod
    def handle_control_command(_text, _session_id):
        return type("Outcome", (), {"response": "작업 취소를 요청했습니다, 보스.", "next_goal": ""})()

    @classmethod
    def enqueue_goal(cls, goal, _session_id, priority=0):
        cls.queued.append((goal, priority))
        return type("Task", (), {"task_id": "feedbeef"})()


class _Memory:
    def save_message(self, *_args):
        pass


class _SpeechTools:
    def speak_text(self, text, _audio_processor):
        return f"spoken: {text}"


class _Hardware:
    def __init__(self):
        self.output_states = []

    def set_output_active(self, active):
        self.output_states.append(active)


def test_gui_conversation_tool_response_round_trip():
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.rag_manager = _Rag()
    jarvis.executor = _Executor()
    jarvis.memory = _Memory()
    jarvis.tool_executor = _SpeechTools()
    jarvis.audio_processor = None
    jarvis.mode_manager = object()
    jarvis.signals = AppSignals()
    jarvis.signals.ai_response_ready.connect(jarvis._on_ai_response)
    jarvis.signals.tts_finished.connect(jarvis._reset_all)
    jarvis.messages = []
    jarvis.session_id = "gui-integration"
    jarvis.last_response = ""
    jarvis._is_processing_ai = False

    jarvis._on_user_input("오늘 날짜를 알려줘")

    assert _pump_until(lambda: bool(jarvis.window.assistants))
    assert jarvis.window.users == ["오늘 날짜를 알려줘"]
    assert "오늘 날짜는" in jarvis.window.assistants[-1]
    assert jarvis.last_response == jarvis.window.assistants[-1]
    assert jarvis._is_processing_ai is False


def test_permission_dialog_allow_and_deny_buttons_complete_modal_loop():
    _app()
    allow_dialog = PermissionRequestDialog("파일 쓰기", "테스트 파일을 저장합니다")
    QTimer.singleShot(0, allow_dialog._on_allow)
    allow_dialog.exec()
    assert allow_dialog.result_value is True

    deny_dialog = PermissionRequestDialog("명령 실행", "테스트 명령을 실행합니다")
    QTimer.singleShot(0, deny_dialog._on_deny)
    deny_dialog.exec()
    assert deny_dialog.result_value is False


def test_permission_manager_worker_to_gui_signal_round_trip(tmp_path):
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.signals = AppSignals()
    jarvis._permission_result = None
    jarvis._permission_event = threading.Event()
    jarvis.window = type("Window", (), {"request_permission": lambda *_args: True})()
    jarvis.signals.permission_request.connect(jarvis._on_permission_request)

    manager = PermissionManager(str(tmp_path / "permissions.json"))

    def callback(permission):
        jarvis._permission_result = None
        jarvis._permission_event.clear()
        jarvis.signals.permission_request.emit(permission.name, permission.description)
        return jarvis._permission_event.wait(timeout=2) and jarvis._permission_result

    manager.set_request_callback(callback)
    result = []
    worker = threading.Thread(target=lambda: result.append(manager.request_permission("filesystem_write")))
    worker.start()

    assert _pump_until(lambda: not worker.is_alive())
    worker.join(timeout=1)
    assert result == [True]


def test_proactive_message_is_displayed_and_saved_without_user_input(monkeypatch):
    monkeypatch.setattr("main_qt.get_assistant_settings", lambda: type(
        "Settings", (), {"get": staticmethod(lambda _key: "자연스러운 존댓말로 대답")}
    )())
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "proactive-session"
    jarvis.last_response = ""
    jarvis.signals = AppSignals()
    jarvis.signals.proactive_message.connect(jarvis._on_proactive_message)

    jarvis.notify_user("보스, 예약 작업을 완료했습니다.")

    assert _pump_until(lambda: bool(jarvis.window.assistants))
    assert jarvis.window.assistants[-1] == "보스, 예약 작업을 완료했습니다."
    assert jarvis.messages[-1]["role"] == "assistant"


def test_selected_voice_address_is_applied_at_gui_boundary(monkeypatch):
    monkeypatch.setattr("main_qt.get_assistant_settings", lambda: type(
        "Settings", (), {"get": staticmethod(lambda _key: "자연스러운 존댓말로 대답")}
    )())
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "address-session"
    jarvis.last_response = ""
    jarvis.tool_executor = type(
        "SpeechTools",
        (),
        {
            "tts_settings": type(
                "Settings",
                (),
                {
                    "personalize_address": staticmethod(
                        lambda text: text.replace("보스", "지휘관님")
                    )
                },
            )()
        },
    )()
    jarvis.signals = AppSignals()
    jarvis.signals.proactive_message.connect(jarvis._on_proactive_message)

    jarvis.notify_user("보스, 예약 작업을 완료했습니다.")

    assert _pump_until(lambda: bool(jarvis.window.assistants))
    assert jarvis.window.assistants[-1] == "지휘관님, 예약 작업을 완료했습니다."


def test_control_command_is_accepted_while_ai_is_processing():
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.executor = _ControllableExecutor()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "control-session"
    jarvis._is_processing_ai = True
    jarvis.signals = AppSignals()
    jarvis.signals.control_response_ready.connect(jarvis._on_control_response)

    jarvis._on_user_input("작업 abcdef12 취소")

    assert _pump_until(lambda: bool(jarvis.window.assistants))
    assert "취소를 요청" in jarvis.window.assistants[-1]
    assert jarvis._is_processing_ai is True


def test_new_request_is_queued_while_current_task_is_processing():
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.executor = _ControllableExecutor()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "queue-session"
    jarvis._is_processing_ai = True
    _ControllableExecutor.queued = []

    jarvis._on_user_input("새 작업: 내일 일정도 확인해줘")

    assert _ControllableExecutor.queued == [("내일 일정도 확인해줘", 0)]
    assert "feedbeef" in jarvis.window.assistants[-1]


def test_normal_request_is_queued_instead_of_rejected_while_busy():
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.executor = _ControllableExecutor()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "queue-normal-session"
    jarvis._is_processing_ai = True
    _ControllableExecutor.queued = []

    jarvis._on_user_input("디코 꺼줘")

    assert _ControllableExecutor.queued == [("디코 꺼줘", 0)]
    assert "대기 작업 ID" in jarvis.window.assistants[-1]


def test_tts_suspends_microphone_until_output_finishes():
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.tool_executor = _SpeechTools()
    jarvis.audio_processor = None
    jarvis.hardware_manager = _Hardware()
    jarvis.signals = AppSignals()

    jarvis._speak_with_check("테스트 응답")

    assert jarvis.hardware_manager.output_states == [True, False]


def test_standalone_wake_word_does_not_reach_planner():
    _app()
    class PlannerMustNotRun:
        def execute_goal(self, *_args, **_kwargs):
            raise AssertionError("단독 호출어가 Planner에 전달되었습니다.")

    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.executor = PlannerMustNotRun()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "wake-chat-session"
    jarvis.last_response = ""
    jarvis._is_processing_ai = False
    jarvis.assistant_settings = SimpleNamespace(wake_word="자비스")

    jarvis._on_user_input("자비스")

    assert jarvis.window.assistants[-1] == "네, 보스. 말씀하세요."
    assert jarvis._is_processing_ai is False
