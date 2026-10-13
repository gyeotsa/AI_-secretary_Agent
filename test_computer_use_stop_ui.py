import ctypes
from ctypes import wintypes
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QLabel

from core.runtime.event_bus import Event, EventBus
from core.state_machine import State
from core.turn_context import TurnExecutionContext
from main_qt import AppSignals, JarvisApp
from ui.computer_use_control import EmergencyStopHotkey


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def user32(monkeypatch):
    import ui.computer_use_control as control
    native = Mock()
    native.RegisterHotKey.return_value = True
    monkeypatch.setattr(control.sys, "platform", "win32")
    monkeypatch.setattr(control.ctypes, "windll", SimpleNamespace(user32=native), raising=False)
    return native


def test_global_hotkey_dispatches_only_its_own_windows_message_and_releases_once(user32):
    application, stop = Mock(), Mock()
    hotkey = EmergencyStopHotkey(application, stop)
    user32.RegisterHotKey.assert_called_once_with(None, hotkey.HOTKEY_ID, 0x4006, 0x7A)
    application.installNativeEventFilter.assert_called_once_with(hotkey)
    message = wintypes.MSG()
    message.message = hotkey.WM_HOTKEY
    message.wParam = hotkey.HOTKEY_ID + 1
    assert hotkey.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message)) == (False, 0)
    message.wParam = hotkey.HOTKEY_ID
    assert hotkey.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message)) == (True, 0)
    stop.assert_called_once_with()
    hotkey.close()
    hotkey.close()
    user32.UnregisterHotKey.assert_called_once_with(None, hotkey.HOTKEY_ID)
    application.removeNativeEventFilter.assert_called_once_with(hotkey)
    assert not hotkey.registered
    assert hotkey.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message)) == (False, 0)


def test_registration_conflict_does_not_claim_global_stop_or_install_filter(user32):
    user32.RegisterHotKey.return_value = False
    application = Mock()
    hotkey = EmergencyStopHotkey(application, Mock())
    assert not hotkey.registered
    application.installNativeEventFilter.assert_not_called()
    hotkey.close()
    user32.UnregisterHotKey.assert_not_called()


def test_failed_filter_installation_rolls_back_hotkey(user32):
    application = Mock()
    application.installNativeEventFilter.side_effect = RuntimeError("Qt shutdown")
    hotkey = EmergencyStopHotkey(application, Mock())
    assert not hotkey.registered
    user32.UnregisterHotKey.assert_called_once_with(None, hotkey.HOTKEY_ID)


def bare_app(qt_app):
    app = JarvisApp.__new__(JarvisApp)
    app.app = qt_app
    app.signals = AppSignals()
    app.window = SimpleNamespace(status_label=QLabel(), update_state=Mock())
    app.state_machine = SimpleNamespace(state=State.EXECUTING)
    app._active_turn_id = "current"
    app._active_turn_context = TurnExecutionContext("current", "session")
    app.session_id = "session"
    app._runtime_shutdown_started = False
    app._queued_specialist_payloads = {"old": {}}
    app._queued_dispatch_inflight = {"old"}
    app.executor = SimpleNamespace(current_agent_task_id="", dialogue_state=None)
    return app


def test_tool_events_use_qt_bridge_keep_progress_and_clear_finished(qt_app, monkeypatch):
    import core.runtime.event_bus as runtime_events
    import main_qt
    bus = EventBus()
    monkeypatch.setattr(runtime_events, "get_event_bus", lambda: bus)
    hotkey = SimpleNamespace(registered=True, close=Mock())
    monkeypatch.setattr(main_qt, "EmergencyStopHotkey", lambda *_: hotkey)
    app = bare_app(qt_app)
    app._install_computer_use_controls()
    event = Event("computer_use.step", "computer_use", data={
        "turn_id": "current", "target": "메모장", "step": 0, "action": "type_text"})
    thread = threading.Thread(target=lambda: bus.publish(event))
    thread.start()
    thread.join()
    assert not app.window.status_label.text()
    qt_app.processEvents()
    assert "메모장 · 1단계 입력" in app.window.status_label.text()
    assert "Ctrl+Shift+F11" in app.window.status_label.text()
    app._on_state_changed(State.PROCESSING, State.EXECUTING)
    assert app.window.status_label.text() == app._computer_use_progress_text
    bus.publish(Event("computer_use.finished", "computer_use", data={"turn_id": "current", "status": "succeeded"}))
    assert not app._computer_use_active and not app._computer_use_progress_text
    app.window.update_state.assert_called_with(State.EXECUTING)
    app._shutdown_computer_use_controls()
    hotkey.close.assert_called_once_with()
    assert all(not subscribers for subscribers in bus._subscribers.values())


def test_stale_and_cancelled_turn_events_cannot_reopen_progress(qt_app):
    app = bare_app(qt_app)
    app._emergency_stop_hotkey = SimpleNamespace(registered=True)
    for turn_id in ("previous", "current"):
        if turn_id == "current":
            app._active_turn_context.cancel()
        app._on_computer_use_event(Event("computer_use.started", "computer_use", data={
            "turn_id": turn_id, "target": "메모장"}))
    assert not app.window.status_label.text()


def test_emergency_stop_cancels_real_turn_queue_and_pending_permission(qt_app):
    app = bare_app(qt_app)
    app._is_processing_ai = True
    app._computer_use_active = True
    app._computer_use_progress_text = "진행 중"
    app._permission_event = threading.Event()
    app._permission_result = None
    app._cancel_speech_only = Mock()
    app._reset_all = Mock()
    state = Mock()
    state.list_tasks.return_value = [SimpleNamespace(task_id="queued", status="queued")]
    app.executor = SimpleNamespace(current_agent_task_id="running", dialogue_state=state,
                                   _workspace_scope=lambda: "", handle_control_command=Mock())
    app._on_emergency_stop()
    assert app._active_turn_context.cancelled
    app.executor.handle_control_command.assert_called_once_with("running 취소", "session")
    state.transition_task.assert_called_once_with("queued", "cancelled", result="새 사용자 입력으로 대체됨")
    assert not app._is_processing_ai and not app._computer_use_active
    assert not app._queued_specialist_payloads and not app._queued_dispatch_inflight
    assert app._permission_result is False and app._permission_event.is_set()
    app._cancel_speech_only.assert_called_once_with()
    app._reset_all.assert_called_once_with()
    assert "중지 요청" in app.window.status_label.text()


def test_superseding_turn_clears_previous_computer_progress(qt_app):
    app = bare_app(qt_app)
    app._computer_use_active = True
    app._computer_use_progress_text = "old target"
    app._cancel_pending_work()
    assert not app._computer_use_active and not app._computer_use_progress_text


def test_ui_and_executor_input_diagnostics_never_print_command_text(qt_app, monkeypatch, capsys):
    import main_qt
    app = bare_app(qt_app)
    app.window = Mock()
    app.state_machine = Mock()
    app.memory = SimpleNamespace(workspace_namespace="global")
    app.messages = []
    app.workspace_manager = None
    app.assistant_settings = SimpleNamespace(wake_word="아니스")
    app._is_processing_ai = False
    app._cancel_speech_only = Mock()
    app._cancel_pending_work = Mock()
    secret = "private-command-marker"
    app.executor = SimpleNamespace(execute_turn=Mock(return_value=SimpleNamespace(
        response=secret, status="completed", next_goal="")))
    monkeypatch.setattr(main_qt.threading, "Thread", Mock())
    app._on_user_input(f"아니스, 메모장에 {secret} 입력해줘")
    turn = main_qt.threading.Thread.call_args.kwargs["args"][0]
    app._process_ai(turn)
    output = capsys.readouterr().out
    assert secret not in output
    assert "UI input received" in output and "executor input received" in output
    assert "호출어 제거됨" in output
    assert secret in app.executor.execute_turn.call_args.args[0]


@pytest.mark.parametrize("prefix", ("", "[GAME]", "[WORK]"))
def test_response_diagnostics_preserve_ui_without_printing_private_output(qt_app, monkeypatch, capsys, prefix):
    import main_qt
    app = bare_app(qt_app)
    app.window = Mock()
    app.state_machine = Mock()
    app.memory = Mock()
    app.messages = []
    app.workspace_manager = None
    app.mode_manager = Mock()
    app._personalize_address = lambda text: text
    app._archive_obsidian_exchange_async = Mock()
    app._consolidate_memory_async = Mock()
    app._tts_enabled = lambda: True
    secret = "private-response-marker"
    response = prefix + secret
    monkeypatch.setattr(main_qt, "present_channels", lambda *_: SimpleNamespace(
        technical_text=secret, screen_text=response, speech_text=secret))
    monkeypatch.setattr(main_qt, "get_assistant_settings", lambda: SimpleNamespace(assistant_name="아니스"))
    monkeypatch.setattr(main_qt.threading, "Thread", Mock())
    turn = main_qt.TurnEnvelope("current", "session", "요청", tuple())
    app._on_ai_response(main_qt.TurnResult(turn, response))
    output = capsys.readouterr().out
    assert secret not in output
    assert "technical output" in output and "screen output" in output
    assert secret in app.window.show_assistant_text.call_args.args[0]


@pytest.mark.parametrize("raises", (False, True))
def test_speech_diagnostics_do_not_print_response_or_error_content(qt_app, capsys, raises):
    app = bare_app(qt_app)
    app._tts_enabled = lambda: True
    secret = "private-spoken-response-marker"
    speaker = Mock(return_value="TTS 오류: " + secret)
    if raises:
        speaker.side_effect = ValueError(secret)
    app.tool_executor = SimpleNamespace(speak_text=speaker)
    app.audio_processor = Mock()
    app._speak_with_check(secret)
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    speaker.assert_called_once_with(secret, app.audio_processor)


@pytest.mark.parametrize("cancel_at", ("before_start", "during", "tool_result"))
def test_cancelled_worker_clears_current_processing_and_allows_new_chat(qt_app, cancel_at):
    import main_qt
    from core.plugin import ToolCancelledError
    from core.state_machine import StateMachine
    app = bare_app(qt_app)
    app.window = Mock()
    app.state_machine = StateMachine()
    app.state_machine.start_listening()
    app.state_machine.start_processing()
    app.state_machine.state_changed.connect(app._on_state_changed)
    app._is_processing_ai = True
    app._computer_use_active = True
    app._computer_use_progress_text = "이전 진행"
    app.messages = [{"role": "user", "content": "이전 요청"}]
    app.memory = Mock()
    app.memory.create_session.return_value = "new-session"
    app.memory.load_session.return_value = []
    app._run_idle_memory_maintenance = Mock()
    app._run_next_queued_task = Mock()
    context = app._active_turn_context
    if cancel_at == "before_start":
        context.cancel()

    def execute(*_):
        if cancel_at == "during":
            context.cancel()
            raise ToolCancelledError("private-cancellation-marker")
        return SimpleNamespace(response="", status="cancelled", next_goal="")

    app.executor.execute_turn = Mock(side_effect=execute)
    app.signals.ai_response_ready.connect(app._on_ai_response)
    turn = main_qt.TurnEnvelope("current", "session", "요청", tuple(), execution=context)
    worker = threading.Thread(target=app._process_ai, args=(turn,))
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert app._is_processing_ai
    qt_app.processEvents()
    assert not app._is_processing_ai and not app._computer_use_active
    assert not app._computer_use_progress_text and app.state_machine.state == State.IDLE
    assert "취소" in app.window.show_assistant_text.call_args.args[0]
    app._create_session("새 대화")
    assert app.session_id == "new-session" and app.messages == []


@pytest.mark.parametrize("turn_id,session_id", (("previous", "session"), ("current", "other-session")))
def test_late_cancelled_turn_never_clears_newer_processing(qt_app, turn_id, session_id):
    import main_qt
    app = bare_app(qt_app)
    app.window = Mock()
    app._is_processing_ai = True
    app._computer_use_progress_text = "현재 작업"
    app._reset_all = Mock()
    context = TurnExecutionContext(turn_id, session_id)
    context.cancel()
    turn = main_qt.TurnEnvelope(turn_id, session_id, "이전 요청", tuple(), execution=context)
    app._on_ai_response(main_qt.TurnResult(turn, "", "cancelled"))
    assert app._is_processing_ai and app._computer_use_progress_text == "현재 작업"
    app._reset_all.assert_not_called()
    app.window.show_assistant_text.assert_not_called()


@pytest.mark.parametrize("accept_after", (350, None))
def test_action_permission_allows_reading_time_but_still_expires(qt_app, monkeypatch, accept_after):
    import main_qt
    done, cancelled = Mock(), threading.Event()
    done.wait.side_effect = ([False] * 1200 if accept_after is None
                             else [False] * accept_after + [True])
    monkeypatch.setattr(main_qt, "threading", SimpleNamespace(Event=Mock(side_effect=(done, cancelled))))
    app = bare_app(qt_app)
    app.signals = SimpleNamespace(permission_action_request=Mock())
    app.signals.permission_action_request.emit.side_effect = lambda request: request.update(result=True)
    assert app._request_action_permission(SimpleNamespace(name="화면 조작", description="이번 작업")) is (accept_after is not None)
    assert done.wait.call_count == (1200 if accept_after is None else accept_after + 1)
    assert all(call.args == (0.1,) for call in done.wait.call_args_list)
    assert cancelled.is_set()


def test_action_permission_remains_cancellable_while_waiting(qt_app, monkeypatch):
    import main_qt
    from core.plugin import ToolCancelledError
    from core.turn_context import bind_turn_context
    done, cancelled = Mock(), threading.Event()
    context = TurnExecutionContext("current", "session")
    done.wait.side_effect = lambda _timeout: context.cancel() or False
    monkeypatch.setattr(main_qt, "threading", SimpleNamespace(Event=Mock(side_effect=(done, cancelled))))
    app = bare_app(qt_app)
    app.signals = SimpleNamespace(permission_action_request=Mock())
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        app._request_action_permission(SimpleNamespace(name="화면 조작", description="이번 작업"))
    done.wait.assert_called_once_with(0.1)
    assert cancelled.is_set()


def test_action_permission_dialog_is_visible_and_raised_before_modal_wait(qt_app, monkeypatch):
    import ui.main_window as main_window
    from PyQt6.QtWidgets import QDialog
    calls = []

    class VisibleDialog(QDialog):
        result_value = True

        def show(self):
            super().show()
            calls.append("show")

        def raise_(self):
            calls.append("raise")

        def activateWindow(self):
            calls.append("activate")

        def exec(self):
            assert self.isVisible()
            calls.append("exec")
            self.close()

    monkeypatch.setattr(main_window, "PermissionRequestDialog", lambda *_args, **_kwargs: VisibleDialog())
    assert main_window.JarvisMainWindow.request_permission(None, "화면 조작", "이번 작업", persist_decision=False)
    assert calls == ["show", "raise", "activate", "exec"]
