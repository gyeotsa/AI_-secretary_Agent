import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from core.permission import PermissionManager
from core.tools import get_tool_executor
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
        return f"오늘 날짜는 {get_tool_executor().execute_tool('get_date', {})}입니다."


class _Memory:
    def save_message(self, *_args):
        pass


class _SpeechTools:
    def speak_text(self, text, _audio_processor):
        return f"spoken: {text}"


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


def test_proactive_message_is_displayed_and_saved_without_user_input():
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
