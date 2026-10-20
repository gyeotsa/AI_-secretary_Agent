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
from ui.main_window import JarvisMainWindow, PermissionRequestDialog


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
    @staticmethod
    def render_outcome(outcome, *_args):
        return outcome

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

    def cancel_tts(self):
        self.cancelled = True


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
    jarvis.executor = SimpleNamespace(render_outcome=lambda outcome, *_args: outcome)
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
    jarvis.executor = SimpleNamespace(render_outcome=lambda outcome, *_args: outcome)
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
    jarvis.signals = AppSignals()
    jarvis.signals.proactive_message.connect(jarvis._on_proactive_message)
    jarvis._is_processing_ai = True
    _ControllableExecutor.queued = []

    jarvis._on_user_input("새 작업: 내일 일정도 확인해줘")

    assert _ControllableExecutor.queued == [("내일 일정도 확인해줘", 0)]
    assert _pump_until(lambda: bool(jarvis.window.assistants))
    assert "feedbeef" in jarvis.window.assistants[-1]


def test_normal_request_supersedes_running_turn_instead_of_queueing(monkeypatch):
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.executor = _ControllableExecutor()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "queue-normal-session"
    jarvis._is_processing_ai = True
    jarvis._active_turn_id = "old-turn"
    _ControllableExecutor.queued = []
    started = []
    monkeypatch.setattr(
        jarvis, "_process_ai",
        lambda turn, *_args: started.append(turn),
    )

    jarvis._on_user_input("디코 꺼줘")

    assert _ControllableExecutor.queued == []
    assert _pump_until(lambda: bool(started))
    assert started[0].user_text == "디코 꺼줘"
    assert jarvis._active_turn_id == started[0].turn_id


def test_tts_suspends_microphone_until_output_finishes():
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.tool_executor = _SpeechTools()
    jarvis.assistant_settings = SimpleNamespace(tts_enabled=True)
    jarvis.audio_processor = None
    jarvis.hardware_manager = _Hardware()
    jarvis.signals = AppSignals()

    jarvis._speak_with_check("테스트 응답")

    assert jarvis.hardware_manager.output_states == [True, False]


def test_tts_off_skips_speech_without_waiting():
    jarvis = JarvisApp.__new__(JarvisApp)
    speaker = _SpeechTools()
    jarvis.tool_executor = speaker
    jarvis.audio_processor = None
    jarvis.hardware_manager = _Hardware()
    jarvis.signals = AppSignals()
    jarvis.assistant_settings = SimpleNamespace(tts_enabled=False)

    jarvis._speak_with_check("읽으면 안 되는 응답")

    assert jarvis.hardware_manager.output_states == []
    assert not hasattr(speaker, "cancelled")


def test_voice_output_toggle_updates_label_and_emits_state():
    _app()
    window = JarvisMainWindow()
    emitted = []
    window.voice_output_toggled.connect(emitted.append)

    window.set_voice_output_enabled(False)
    assert window.voice_output_btn.text() == "음성 OFF"
    assert window.voice_output_btn.isChecked() is False
    window.voice_output_btn.click()

    assert emitted == [True]
    assert window.voice_output_btn.text() == "음성 ON"
    window.close()


def test_long_chat_is_scrollable_without_growing_window_or_hiding_input():
    from PyQt6.QtCore import QPoint
    app = _app()
    window = JarvisMainWindow()
    try:
        window.resize(800, 800)
        window.show()
        app.processEvents()
        initial = window.size()
        assert not window.message_scroll.widget().autoFillBackground()
        assert not window.message_scroll.viewport().autoFillBackground()
        samples = [
            "안녕!", "매우 긴 설명입니다. " * 1000,
            "```python\n" + "print('원문 <tag> 🙂')\n" * 200 + "```",
            "X" * 10000, "짧은 답변으로 돌아왔어.",
        ]
        for sample in samples:
            window.show_user_text(sample)
            window.show_assistant_text(sample)
            app.processEvents()
            app.processEvents()
            assert window.size() == initial
            assert window.assistant_text_label.text() == sample
            assert window.user_text_label.text() == sample
            assert window.rect().contains(window.text_input.mapTo(
                window, QPoint(window.text_input.width()-1, window.text_input.height()-1)))
            if "매우 긴" in sample or "```" in sample:
                assert window.message_scroll.verticalScrollBar().maximum() > 0
        window.set_chat_collapsed(True)
        app.processEvents()
        assert not window.message_scroll.isVisible()
        window.set_chat_collapsed(False)
        app.processEvents()
        assert window.text_input.isVisible()
        assert window.size() == initial
        window.show_user_text("긴 질문입니다. " * 1000)
        window.show_assistant_text("짧은 답변도 바로 보여야 해.")
        for _ in range(3):
            app.processEvents()
        reply_top = window.assistant_text_label.mapTo(
            window.message_scroll.viewport(), QPoint(0, 0)).y()
        assert 0 <= reply_top < window.message_scroll.viewport().height()
        assert window.message_scroll.verticalScrollBar().value() > 0
        from PyQt6.QtWidgets import QAbstractSlider
        bar = window.message_scroll.verticalScrollBar()
        bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
        manual_position = bar.value()
        assert not window._follow_reply
        window.resize(800, 850)
        for _ in range(3):
            app.processEvents()
        assert bar.value() == manual_position
        window.show_assistant_text("곧바로 새 질문에 대체될 답변")
        window.show_user_text("새 질문 " * 1000)
        for _ in range(3):
            app.processEvents()
        assert window.message_scroll.verticalScrollBar().value() > 0
        assert not window._follow_reply
    finally:
        window._allow_close = True
        window.close()
        window.deleteLater()
        app.processEvents()


def test_research_report_is_readable_clickable_and_archives_its_canonical_text(tmp_path, monkeypatch):
    from pathlib import Path
    from PyQt6.QtCore import Qt, QUrl
    from PyQt6.QtGui import QTextDocument
    from config import Config
    from ui.codex_shell import QDesktopServices

    app = _app()
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    directory = tmp_path / "research_reports"
    directory.mkdir()
    markdown = directory / "actual report.md"
    markdown.write_text("실제 보고서", encoding="utf-8")
    data = directory / "actual report.json"
    data.write_text("{}", encoding="utf-8")
    report = (
        "# 조사 결과: 인스타\n\n## 수집 근거 분석\n\n"
        "- 자료의 주장: 과정 소개 영상에서 저장 비율이 높았습니다. [S1]\n\n"
        "<script>alert('외부 지시')</script>\n\n"
        "<img src='file:///untrusted-image.png'>\n\n"
        "![요청하지 않은 이미지](file:///untrusted-markdown-image.png)\n\n"
        "[S1 공식 자료](https://example.org/study)\n\n"
        f"[보고서 열기]({markdown})\n[수집 자료 JSON]({data})"
    )
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    window = JarvisMainWindow()
    try:
        window.show_user_text("인스타 트렌드를 조사해줘")
        window.show_assistant_text(report)
        app.processEvents()
        label = window.assistant_text_label
        assert label.textFormat() == Qt.TextFormat.RichText
        assert label.property("canonicalText") == report
        assert label.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse
        assert not label.openExternalLinks()
        assert "<script" not in label.text().casefold() and "<img" not in label.text().casefold()
        document = QTextDocument()
        document.setHtml(label.text())
        assert document.toPlainText().startswith("조사 결과: 인스타")
        assert "## 수집 근거 분석" not in document.toPlainText()
        assert "https://example.org/study" in label.text()
        assert QUrl.fromLocalFile(str(markdown)).toString(QUrl.ComponentFormattingOption.FullyEncoded) in label.text()
        from ui.theme import theme_manager
        theme = theme_manager()
        original_mode = theme.mode
        try:
            for mode in ("dark", "light"):
                theme.set_mode(mode, persist=False)
                assert theme.colors["accent"] in label.text()
        finally:
            theme.set_mode(original_mode, persist=False)
        label.linkActivated.emit("https://example.org/study")
        label.linkActivated.emit(str(markdown))
        label.linkActivated.emit(QUrl.fromLocalFile(str(data)).toString())
        label.linkActivated.emit("javascript:alert(1)")
        label.linkActivated.emit("file://untrusted-host/share/report.md")
        label.linkActivated.emit("file:relative-report.md")
        label.linkActivated.emit("file:///invalid%00report.md")
        outside = tmp_path / "outside.md"
        outside.write_text("범위 밖 파일", encoding="utf-8")
        label.linkActivated.emit(QUrl.fromLocalFile(str(outside)).toString())
        assert [url.scheme() for url in opened] == ["https", "file", "file"]
        assert Path(opened[1].toLocalFile()) == markdown

        window.show_user_text("다음 질문")
        archived = window._archived_messages[-1]
        assert archived.property("canonicalText") == report
        assert archived.textFormat() == Qt.TextFormat.RichText
        archived.linkActivated.emit("https://example.org/study")
        assert len(opened) == 4
        code = "```python\nprint('<script>원문</script>')\n```"
        window.show_assistant_text(code)
        assert window.assistant_text_label.textFormat() == Qt.TextFormat.PlainText
        assert window.assistant_text_label.text() == code
        assert window.assistant_text_label.property("canonicalText") == code
        window.show_user_text(report)
        assert window.user_text_label.textFormat() == Qt.TextFormat.PlainText
        assert window.user_text_label.text() == report
        assert window._archived_messages[-1].textFormat() == Qt.TextFormat.PlainText
        assert window._archived_messages[-1].text() == code
    finally:
        window._allow_close = True
        window.close()
        window.deleteLater()
        app.processEvents()


def test_superseded_turn_result_never_reaches_ui_or_tts():
    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.session_id = "new-session"
    jarvis._active_turn_id = "new-turn"
    jarvis.last_response = ""
    result = __import__("main_qt").TurnResult(
        turn=__import__("main_qt").TurnEnvelope(
            turn_id="old-turn", session_id="new-session", user_text="옛 요청",
            conversation_history=tuple(),
        ),
        response_text="옛 응답",
    )

    jarvis._on_ai_response(result)

    assert jarvis.window.assistants == []
    assert jarvis.last_response == ""


def test_standalone_wake_word_uses_generated_conversation():
    _app()
    from core.executor import ExecutionOutcome
    class DialogueExecutor:
        calls = []
        def execute_turn(self, text, *_args, **_kwargs):
            self.calls.append(text)
            return ExecutionOutcome("응, 무슨 일이야?", response_generated=True)

    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.window = _Window()
    jarvis.state_machine = _StateMachine()
    jarvis.executor = DialogueExecutor()
    jarvis.signals = AppSignals()
    jarvis.signals.ai_response_ready.connect(jarvis._on_ai_response)
    jarvis.tool_executor = _SpeechTools()
    jarvis.hardware_manager = _Hardware()
    jarvis.memory = _Memory()
    jarvis.messages = []
    jarvis.session_id = "wake-chat-session"
    jarvis.audio_processor = None
    jarvis.last_response = ""
    jarvis._is_processing_ai = False
    jarvis.assistant_settings = SimpleNamespace(wake_word="자비스")

    jarvis._on_user_input("자비스")

    assert _pump_until(lambda: bool(jarvis.window.assistants))
    assert jarvis.window.assistants[-1] == "응, 무슨 일이야?"
    assert jarvis.executor.calls == ["자비스"]
    assert jarvis._is_processing_ai is False


def _session_routing_app(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from core.memory import ConversationMemory
    from core.workspace import WorkspaceManager

    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.memory = ConversationMemory(str(tmp_path / "conversations.db"))
    jarvis.workspace_manager = WorkspaceManager(str(tmp_path / "workspaces.json"))
    jarvis.workspace_manager.get_git_status = lambda *_args: {"is_repository": False}
    jarvis.signals = AppSignals()
    jarvis.signals.workspace_state_ready.connect(jarvis._on_workspace_state_ready)
    jarvis._runtime_shutdown_started = False
    jarvis.window = Mock()
    jarvis.rag_manager = Mock(namespace="global")
    jarvis.rag_manager.set_namespace.side_effect = (
        lambda namespace: setattr(jarvis.rag_manager, "namespace", namespace)
    )
    jarvis.project_indexer = Mock(project_root=None)
    jarvis.project_indexer.set_project_root.side_effect = (
        lambda path: setattr(jarvis.project_indexer, "project_root", path)
    )
    jarvis.user_profile = Mock()
    jarvis.state_machine = _StateMachine()
    jarvis._run_idle_memory_maintenance = Mock()
    jarvis._cancel_pending_work = Mock()
    jarvis._is_processing_ai = False
    jarvis.session_id = jarvis.memory.create_session("일반 대화")
    jarvis.messages = []
    jarvis.last_response = ""
    monkeypatch.setattr("main_qt.threading.Thread", Mock())
    return jarvis


def _project_chat(jarvis, path, title, content):
    path.mkdir()
    assert jarvis.workspace_manager.set_workspace(str(path))
    jarvis.memory.set_namespace(jarvis.workspace_manager.get_namespace())
    session_id = jarvis.memory.create_session(title)
    jarvis.memory.save_message(session_id, "user", content)
    jarvis._activate_workspace_context()
    return session_id


def test_sidebar_chat_selection_restores_project_before_next_message(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    first_path, second_path = tmp_path / "project-a", tmp_path / "project-b"
    first = _project_chat(jarvis, first_path, "첫 프로젝트", "첫 질문")
    second = _project_chat(jarvis, second_path, "둘째 프로젝트", "둘째 질문")

    jarvis._select_session(first)

    assert jarvis.session_id == first
    assert jarvis.workspace_manager.get_workspace_path() == str(first_path.resolve())
    first_namespace = jarvis.workspace_manager.namespace_for(first_path)
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == first_namespace
    assert jarvis.project_indexer.project_root == str(first_path.resolve())
    assert jarvis.messages == [{"role": "user", "content": "첫 질문"}]
    jarvis.memory.save_message(jarvis.session_id, "assistant", "첫 답변")

    jarvis._select_session(second)
    assert jarvis.workspace_manager.get_workspace_path() == str(second_path.resolve())
    assert jarvis.messages == [{"role": "user", "content": "둘째 질문"}]
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == (
        jarvis.workspace_manager.namespace_for(second_path)
    )
    assert jarvis.memory.load_session(first)[-1] == {"role": "assistant", "content": "첫 답변"}
    assert jarvis.last_response == ""


def test_general_and_missing_project_chats_do_not_inherit_selected_folder(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    general = jarvis.session_id
    project = tmp_path / "selected-project"
    project_chat = _project_chat(jarvis, project, "프로젝트 대화", "프로젝트 질문")
    jarvis._select_session(project_chat)

    jarvis._select_session(general)

    assert not jarvis.workspace_manager.is_set()
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == "global"
    assert jarvis.project_indexer.project_root is None
    jarvis._create_session("새 일반 대화")
    assert jarvis.memory.workspace_namespace == "global"
    assert len(jarvis.memory.list_session_details()) == 2

    jarvis.memory.set_namespace("workspace-unavailable")
    unavailable = jarvis.memory.create_session("폴더를 찾을 수 없는 대화")
    jarvis.memory.save_message(unavailable, "user", "남아 있는 기록")
    jarvis._select_session(project_chat)
    jarvis._select_session(unavailable)

    assert not jarvis.workspace_manager.is_set()
    assert jarvis.project_indexer.project_root is None
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == "workspace-unavailable"
    assert jarvis.messages == [{"role": "user", "content": "남아 있는 기록"}]
    jarvis._create_session("같은 프로젝트의 새 대화")
    assert {item["workspace_namespace"] for item in jarvis.memory.list_session_details()} == {
        "workspace-unavailable"
    }


def test_folder_selection_loads_or_creates_chat_in_that_project(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    first_path, second_path, empty_path = (
        tmp_path / "project-a", tmp_path / "project-b", tmp_path / "empty-project"
    )
    first = _project_chat(jarvis, first_path, "첫 프로젝트", "첫 질문")
    second = _project_chat(jarvis, second_path, "둘째 프로젝트", "둘째 질문")
    jarvis._select_session(first)

    jarvis._on_workspace_selected(str(second_path))

    assert jarvis.session_id == second
    assert jarvis.messages == [{"role": "user", "content": "둘째 질문"}]
    empty_path.mkdir()
    jarvis._on_workspace_selected(str(empty_path))
    assert jarvis.session_id not in {first, second}
    assert jarvis.messages == []
    assert jarvis.memory.workspace_namespace == jarvis.workspace_manager.namespace_for(empty_path)
    assert [item["session_id"] for item in jarvis.memory.list_session_details()] == [jarvis.session_id]
    assert jarvis.memory.load_session(first) == [{"role": "user", "content": "첫 질문"}]


def test_busy_chat_selection_preserves_session_and_project(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    project = tmp_path / "project"
    other = _project_chat(jarvis, project, "다른 대화", "다른 질문")
    jarvis._select_session(other)
    active = jarvis.session_id
    scope = jarvis.memory.workspace_namespace
    jarvis._is_processing_ai = True

    jarvis._select_session("missing-session")

    assert jarvis.session_id == active
    assert jarvis.memory.workspace_namespace == scope
    assert jarvis.workspace_manager.get_workspace_path() == str(project.resolve())
    assert jarvis.messages == [{"role": "user", "content": "다른 질문"}]


def test_folder_new_chat_uses_clicked_project_and_preserves_existing_chats(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    first_path, second_path = tmp_path / "project-a", tmp_path / "project-b"
    first = _project_chat(jarvis, first_path, "첫 프로젝트", "첫 질문")
    second = _project_chat(jarvis, second_path, "둘째 프로젝트", "둘째 질문")
    jarvis._select_session(second)
    namespace = jarvis.workspace_manager.namespace_for(first_path)

    jarvis._create_project_chat(namespace)

    created = jarvis.session_id
    assert created not in {first, second}
    assert jarvis.messages == []
    assert jarvis.workspace_manager.get_workspace_path() == str(first_path.resolve())
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == namespace
    assert {s["session_id"] for s in jarvis.memory.list_session_details()} == {first, created}
    assert jarvis.memory.load_session(first) == [{"role": "user", "content": "첫 질문"}]
    assert jarvis.memory.load_session(second) == [{"role": "user", "content": "둘째 질문"}]

    jarvis._create_project_chat("global")
    assert jarvis.session_id not in {first, second, created}
    assert not jarvis.workspace_manager.is_set()
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == "global"
    assert jarvis.project_indexer.project_root is None


def test_unavailable_folder_new_chat_warns_without_changing_current_project(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    missing_path = tmp_path / "removed-project"
    previous = _project_chat(jarvis, missing_path, "기존 대화", "보존할 질문")
    namespace = jarvis.workspace_manager.namespace_for(missing_path)
    missing_path.rmdir()
    other = _project_chat(jarvis, tmp_path / "other-project", "다른 대화", "다른 질문")
    jarvis._select_session(other)
    current_namespace = jarvis.memory.workspace_namespace
    before = jarvis.memory.list_session_details(all_workspaces=True)

    jarvis._create_project_chat(namespace)

    assert jarvis.session_id == other
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == current_namespace
    assert jarvis.memory.list_session_details(all_workspaces=True) == before
    jarvis.window.show_assistant_text.assert_called()
    assert jarvis.memory.load_session(previous) == [{"role": "user", "content": "보존할 질문"}]


def test_uncatalogued_folder_new_chat_does_not_inherit_current_project_path(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    namespace = "workspace-legacy"
    jarvis.memory.set_namespace(namespace)
    previous = jarvis.memory.create_session("기존 대화")
    other = _project_chat(jarvis, tmp_path / "other-project", "다른 대화", "다른 질문")
    jarvis._select_session(other)

    jarvis._create_project_chat(namespace)

    assert not jarvis.workspace_manager.is_set()
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == namespace
    assert jarvis.project_indexer.project_root is None
    assert {s["session_id"] for s in jarvis.memory.list_session_details()} == {
        previous, jarvis.session_id
    }


def test_removing_other_project_keeps_current_scope_and_preserves_files_and_chats(tmp_path, monkeypatch):
    from core.workspace import WorkspaceManager

    jarvis = _session_routing_app(tmp_path, monkeypatch)
    removed_path, active_path = tmp_path / "remove-project", tmp_path / "active-project"
    removed = _project_chat(jarvis, removed_path, "남길 대화", "남길 질문")
    marker = removed_path / "keep.txt"
    marker.write_text("실제 파일은 남아야 함", encoding="utf-8")
    jarvis.memory.set_session_pinned(removed, True)
    active = _project_chat(jarvis, active_path, "현재 대화", "현재 질문")
    jarvis._select_session(active)
    namespace = jarvis.workspace_manager.namespace_for(removed_path)
    active_namespace = jarvis.memory.workspace_namespace

    jarvis._remove_project(namespace)

    assert jarvis.session_id == active
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == active_namespace
    assert jarvis.workspace_manager.get_workspace_path() == str(active_path.resolve())
    assert jarvis.messages == [{"role": "user", "content": "현재 질문"}]
    assert marker.read_text(encoding="utf-8") == "실제 파일은 남아야 함"
    assert jarvis.memory.load_session(removed) == [{"role": "user", "content": "남길 질문"}]
    detail = next(s for s in jarvis.memory.list_session_details(all_workspaces=True)
                  if s["session_id"] == removed)
    assert detail["workspace_namespace"] == "global"
    assert detail["title"] == "남길 대화" and detail["is_pinned"]
    restored = WorkspaceManager(str(tmp_path / "workspaces.json"))
    assert [p.namespace for p in restored.list_workspaces(include_unavailable=True)] == [active_namespace]
    jarvis.window.set_project_workspaces.assert_called()
    jarvis.window.refresh_session_list.assert_called()


def test_removing_active_project_selects_general_chat_and_removes_only_catalog_entry(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    general = jarvis.session_id
    path = tmp_path / "active-project"
    active = _project_chat(jarvis, path, "프로젝트 대화", "질문 보존")
    jarvis._select_session(active)
    namespace = jarvis.memory.workspace_namespace

    jarvis._remove_project(namespace)

    assert not jarvis.workspace_manager.is_set()
    assert jarvis.workspace_manager.list_workspaces(include_unavailable=True) == []
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == "global"
    assert jarvis.project_indexer.project_root is None
    assert jarvis.session_id in {general, active}
    assert {s["session_id"] for s in jarvis.memory.list_session_details()} == {general, active}
    assert path.is_dir()
    assert jarvis.memory.load_session(active) == [{"role": "user", "content": "질문 보존"}]


def test_busy_project_actions_preserve_current_chat_catalog_and_scope(tmp_path, monkeypatch):
    jarvis = _session_routing_app(tmp_path, monkeypatch)
    first = _project_chat(jarvis, tmp_path / "project-a", "첫 대화", "첫 질문")
    second = _project_chat(jarvis, tmp_path / "project-b", "현재 대화", "현재 질문")
    jarvis._select_session(second)
    first_namespace = jarvis.workspace_manager.namespace_for(tmp_path / "project-a")
    active_namespace = jarvis.memory.workspace_namespace
    before = jarvis.memory.list_session_details(all_workspaces=True)
    catalog = jarvis.workspace_manager.list_workspaces(include_unavailable=True)
    jarvis._is_processing_ai = True

    jarvis._create_project_chat(first_namespace)
    jarvis._remove_project(first_namespace)

    assert jarvis.session_id == second
    assert jarvis.memory.workspace_namespace == jarvis.rag_manager.namespace == active_namespace
    assert jarvis.memory.list_session_details(all_workspaces=True) == before
    assert jarvis.workspace_manager.list_workspaces(include_unavailable=True) == catalog
    assert jarvis.memory.load_session(first) == [{"role": "user", "content": "첫 질문"}]


def test_clearing_project_keeps_catalog_but_does_not_restore_folder(tmp_path):
    from core.workspace import WorkspaceManager

    project = tmp_path / "project"
    project.mkdir()
    state_path = str(tmp_path / "workspaces.json")
    manager = WorkspaceManager(state_path)
    assert manager.set_workspace(str(project), alias="저장된 프로젝트")

    manager.clear_workspace()

    assert not manager.is_set()
    assert manager.get_workspace_path() is None
    assert manager.get_namespace() == "global"
    assert manager.get_info().path == ""
    restored = WorkspaceManager(state_path)
    assert not restored.is_set()
    assert restored.list_workspaces()[0].alias == "저장된 프로젝트"
    project.rmdir()
    assert restored.list_workspaces() == []
    assert restored.list_workspaces(include_unavailable=True)[0].namespace == (
        manager.namespace_for(project)
    )


def test_clearing_project_index_hides_previous_project_files(tmp_path, monkeypatch):
    from core.project_indexer import ProjectIndexer
    from core.workspace import WorkspaceManager

    manager = WorkspaceManager(str(tmp_path / "workspaces.json"))
    monkeypatch.setattr("core.project_indexer.get_workspace_manager", lambda: manager)
    project = tmp_path / "project"
    project.mkdir()
    (project / "example.py").write_text("def project_function():\n    return 1\n", encoding="utf-8")
    indexer = ProjectIndexer(str(tmp_path / "project-index.db"))
    assert indexer.set_project_root(str(project))
    assert indexer.sync_changes()["added"] == 1
    assert indexer.search_files("example")
    assert indexer.search_symbols("project_function")

    assert indexer.set_project_root(None)

    assert indexer.project_root is None
    assert indexer.search_files("example") == []
    assert indexer.search_symbols("project_function") == []
    assert indexer.set_project_root(str(project))
    assert indexer.search_files("example")


def test_sidebar_rename_pin_and_delete_update_storage_and_clear_raw_copies(tmp_path, monkeypatch):
    from unittest.mock import Mock

    jarvis = _session_routing_app(tmp_path, monkeypatch)
    active = jarvis.session_id
    other = jarvis.memory.create_session("삭제할 대화")
    jarvis.memory.save_message(other, "user", "임시 기록")
    jarvis.executor = Mock()
    jarvis.memory_pipeline = Mock()
    jarvis.obsidian_vault = Mock()
    jarvis.continuity_service = Mock()

    jarvis._rename_session(other, "변경한 이름")
    jarvis._pin_session(other, True)
    detail = next(item for item in jarvis.memory.list_session_details() if item["session_id"] == other)
    assert detail["title"] == "변경한 이름" and detail["is_pinned"]
    jarvis._delete_session(other)

    assert jarvis.session_id == active
    assert jarvis.memory.load_session(other) == []
    assert [item["session_id"] for item in jarvis.memory.list_session_details()] == [active]
    jarvis.executor.dialogue_state.clear_session.assert_called_once_with(other)
    jarvis.memory_pipeline.delete_session.assert_called_once_with(other)
    jarvis.obsidian_vault.delete_session_archive.assert_called_once_with(other)
    jarvis.continuity_service.delete_session_transcript.assert_called_once_with(other)
    assert jarvis.window.refresh_session_list.call_count == 3

    jarvis._is_processing_ai = True
    jarvis._delete_session(active)
    assert jarvis.memory.list_sessions()[0][0] == active

    jarvis._is_processing_ai = False
    jarvis._delete_session(active)
    assert jarvis.session_id != active
    assert jarvis.messages == []
    assert len(jarvis.memory.list_sessions()) == 1


def test_workspace_refresh_keeps_gui_responsive_and_never_overlaps_scans(tmp_path):
    from unittest.mock import Mock

    _app()
    jarvis = JarvisApp.__new__(JarvisApp)
    path = str(tmp_path / "project")
    entered, release = threading.Event(), threading.Event()
    info = SimpleNamespace(path=path, name="project", alias="프로젝트")
    scan_threads = []
    gui_threads = []

    def refresh_info(scanned_path):
        scan_threads.append(threading.get_ident())
        assert scanned_path == path
        entered.set()
        assert release.wait(timeout=3)
        return info

    jarvis.workspace_manager = Mock()
    jarvis.workspace_manager.is_set.return_value = True
    jarvis.workspace_manager.get_info.return_value = info
    jarvis.workspace_manager.get_workspace_path.return_value = path
    jarvis.workspace_manager.refresh_info.side_effect = refresh_info
    jarvis.workspace_manager.get_git_status.return_value = {"is_repository": False}
    jarvis.project_indexer = Mock()
    jarvis.window = Mock()
    jarvis.window.set_workspace_info.side_effect = lambda *_args: gui_threads.append(threading.get_ident())
    jarvis._runtime_shutdown_started = False
    jarvis._workspace_refresh_running = False
    jarvis.signals = AppSignals()
    jarvis.signals.workspace_state_ready.connect(jarvis._on_workspace_state_ready)
    main_thread = threading.get_ident()
    try:
        started_at = time.monotonic()
        jarvis._refresh_workspace_state()
        assert time.monotonic() - started_at < 0.5
        assert entered.wait(timeout=1)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        assert _pump_until(lambda: bool(heartbeat), timeout=0.5)
        jarvis._refresh_workspace_state()
        assert jarvis.workspace_manager.refresh_info.call_count == 1
        assert jarvis._workspace_refresh_running is True
    finally:
        release.set()
    assert _pump_until(lambda: not jarvis._workspace_refresh_running)
    assert scan_threads and all(thread_id != main_thread for thread_id in scan_threads)
    assert gui_threads == [main_thread]
    jarvis.workspace_manager.get_git_status.assert_called_once_with(path)
    jarvis.project_indexer.sync_changes.assert_called_once_with(root_path=path)
    jarvis.window.set_workspace_info.assert_called_once_with("프로젝트", path, {"is_repository": False})


def test_stale_or_shutdown_workspace_results_do_not_change_display():
    from unittest.mock import Mock

    jarvis = JarvisApp.__new__(JarvisApp)
    jarvis.workspace_manager = Mock()
    jarvis.workspace_manager.get_workspace_path.return_value = "current-project"
    jarvis.window = Mock()
    jarvis._runtime_shutdown_started = False
    jarvis._workspace_refresh_running = True
    result = {
        "path": "previous-project",
        "info": SimpleNamespace(path="previous-project", name="old", alias="이전 프로젝트"),
        "git_status": {"is_repository": False}, "error": "",
    }

    jarvis._on_workspace_state_ready(result)

    assert jarvis._workspace_refresh_running is False
    jarvis.window.set_workspace_info.assert_not_called()
    jarvis._runtime_shutdown_started = True
    jarvis._workspace_refresh_running = True
    result["path"] = "current-project"
    jarvis._on_workspace_state_ready(result)
    assert jarvis._workspace_refresh_running is False
    jarvis.window.set_workspace_info.assert_not_called()
