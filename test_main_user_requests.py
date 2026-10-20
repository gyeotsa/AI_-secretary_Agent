"""GUI request routing preserves consent and the exact waiting conversation."""
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from core.permission import Permission, PermissionLevel, PermissionManager
from core.dialogue_state import DialogueStateStore
from core.memory import ConversationMemory
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context
from main_qt import JarvisApp, TurnEnvelope, TurnResult


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    application = QApplication.instance() or QApplication([])
    yield application


def bare_app():
    app = JarvisApp.__new__(JarvisApp)
    app._runtime_shutdown_started = False
    app.session_id = "chat"
    app._active_turn_id = "turn"
    app._is_processing_ai = False
    app.window = Mock()
    app.executor = Mock()
    app._on_user_input = Mock()
    app.signals = SimpleNamespace(permission_action_request=Mock())
    return app


@pytest.mark.parametrize("persist,answer,expected", [
    (True, True, True), (True, False, False), (True, None, None),
    (False, True, True), (False, False, False), (False, None, False),
])
def test_permission_worker_distinguishes_dismissal_from_saved_refusal(persist, answer, expected):
    app = bare_app()
    permission = Permission("filesystem_write", "파일 쓰기", "검증용 파일 저장", PermissionLevel.CONFIRM,
                            persist_decision=persist)
    requests = []
    def respond(request):
        requests.append(request)
        request["result"] = answer
        request["event"].set()
    app.signals.permission_action_request.emit.side_effect = respond
    assert app._request_action_permission(permission) is expected
    assert len(requests) == 1 and requests[0]["cancelled"].is_set()


def test_permission_wait_unblocks_when_turn_is_cancelled():
    app = bare_app()
    permission = Permission("computer_control", "컴퓨터 제어", "검증용 1회 동작", PermissionLevel.CONFIRM,
                            persist_decision=False)
    context = TurnExecutionContext("turn", "chat")
    requests = []
    def cancel(request):
        requests.append(request)
        context.cancel()
    app.signals.permission_action_request.emit.side_effect = cancel
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        app._request_action_permission(permission)
    assert len(requests) == 1 and requests[0]["cancelled"].is_set()


def test_concurrent_permission_requests_cannot_receive_each_others_answer():
    app = bare_app()
    received = []
    delivered = threading.Event()
    guard = threading.Lock()
    def capture(request):
        with guard:
            received.append(request)
            if len(received) == 2:
                delivered.set()
    app.signals.permission_action_request.emit.side_effect = capture
    results = {}
    def run(key):
        permission = Permission(key, key, "검증용 요청", PermissionLevel.CONFIRM, persist_decision=False)
        results[key] = app._request_action_permission(permission)
    workers = [threading.Thread(target=run, args=(key,), daemon=True) for key in ("first", "second")]
    for worker in workers:
        worker.start()
    try:
        assert delivered.wait(2), "both requests must become independently visible"
        for request in reversed(received):
            request["result"] = request["name"] == "second"
            request["event"].set()
        for worker in workers:
            worker.join(timeout=2)
        assert all(not worker.is_alive() for worker in workers)
        assert results == {"first": False, "second": True}
    finally:
        for request in received:
            request["cancelled"].set()
            request["event"].set()


def microphone_app():
    app = bare_app()
    app._microphone_generation = 1
    app._microphone_requested = True
    app._microphone_switching = False
    app.permission_manager = Mock()
    app.permission_manager.request_permission.return_value = True
    app.permission_manager.check_permission.return_value = True
    app.hardware_manager = SimpleNamespace(microphone_active=False, start_continuous_listen=Mock(),
                                           stop_continuous_listen=Mock(return_value="마이크를 껐습니다."))
    app.audio_processor = object()
    app.microphone_timer = Mock()
    app.signals.microphone_status = Mock()
    app.signals.voice_text_detected = Mock()
    return app


def test_microphone_toggle_runs_in_worker_and_reflects_confirmed_hardware_state(monkeypatch):
    app = microphone_app()
    app.signals.microphone_status.emit.side_effect = app._on_microphone_status
    def start(*_args):
        app.hardware_manager.microphone_active = True
        return "[성공] 마이크 연결: 검증 장치"
    app.hardware_manager.start_continuous_listen.side_effect = start
    class InlineThread:
        def __init__(self, *, target, args, daemon):
            assert daemon is True
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    monkeypatch.setattr("main_qt.threading.Thread", InlineThread)
    app._on_microphone_toggled(True)
    app.permission_manager.request_permission.assert_called_once_with("microphone")
    app.hardware_manager.start_continuous_listen.assert_called_once_with(
        app._on_continuous_text_detected, app.audio_processor)
    app.window.set_microphone_state.assert_any_call(True, busy=True)
    assert app.window.set_microphone_state.call_args.args == (True,)
    assert app._microphone_requested and not app._microphone_switching
    app._on_microphone_toggled(False)
    app.hardware_manager.stop_continuous_listen.assert_called_once()
    app.window.set_microphone_state.assert_any_call(False, busy=False)
    assert app.window.set_microphone_state.call_args.args == (False,)
    assert not app._microphone_requested and not app._microphone_switching
    assert app.microphone_timer.stop.called


def test_microphone_permission_denial_leaves_ui_off_without_opening_device():
    app = microphone_app()
    app.permission_manager.request_permission.return_value = False
    app.signals.microphone_status.emit.side_effect = app._on_microphone_status
    app._start_continuous_listen(1)
    app.hardware_manager.start_continuous_listen.assert_not_called()
    app.window.set_microphone_state.assert_called_once_with(False)
    assert not app._microphone_requested
    assert "권한" in app.window.status_label.setText.call_args.args[0]


def test_muting_while_permission_is_pending_never_opens_microphone():
    app = microphone_app()
    def answer_after_muting(_permission):
        app._microphone_requested = False
        app._microphone_generation += 1
        return True
    app.permission_manager.request_permission.side_effect = answer_after_muting
    app._start_continuous_listen(1)
    app.hardware_manager.start_continuous_listen.assert_not_called()
    app.signals.microphone_status.emit.assert_not_called()


def test_muting_during_device_start_stops_device_and_never_restores_old_state():
    app = microphone_app()
    def delayed_start(*_args):
        app._microphone_requested = False
        app._microphone_generation += 1
        return "[성공] 마이크 연결: 검증 장치"
    app.hardware_manager.start_continuous_listen.side_effect = delayed_start
    app._start_continuous_listen(1)
    app.hardware_manager.stop_continuous_listen.assert_called_once()
    app.signals.microphone_status.emit.assert_not_called()
    app._on_microphone_status({"generation": 1, "enabled": True, "message": "늦은 시작 결과"})
    app.window.set_microphone_state.assert_not_called()


def test_stale_microphone_stop_does_not_close_a_more_recent_start():
    app = microphone_app()
    app._microphone_generation = 3
    app.hardware_manager.microphone_active = True
    app._stop_continuous_listen(2)
    app.hardware_manager.stop_continuous_listen.assert_not_called()
    app.signals.microphone_status.emit.assert_not_called()
    assert app._microphone_requested and app.hardware_manager.microphone_active


def test_old_microphone_start_completion_does_not_stop_newer_on_request():
    app = microphone_app()
    def finish_after_newer_start(*_args):
        app._microphone_generation = 3
        app._microphone_requested = True
        app.hardware_manager.microphone_active = True
        return "[성공] 마이크 연결: 검증 장치"
    app.hardware_manager.start_continuous_listen.side_effect = finish_after_newer_start
    app._start_continuous_listen(1)
    app.hardware_manager.stop_continuous_listen.assert_not_called()
    app.signals.microphone_status.emit.assert_not_called()
    assert app._microphone_requested and app.hardware_manager.microphone_active


def test_muted_or_revoked_microphone_discards_already_queued_speech():
    app = microphone_app()
    app._on_voice_text_detected("실행해 줘")
    app._on_user_input.assert_called_once_with("실행해 줘")
    app._on_user_input.reset_mock()
    app.permission_manager.check_permission.return_value = False
    app._on_voice_text_detected("권한 철회 후 발화")
    app._on_user_input.assert_not_called()
    app._microphone_requested = False
    app._on_continuous_text_detected("마이크를 끈 뒤 발화")
    app.signals.voice_text_detected.emit.assert_not_called()


def test_permission_revocation_stops_active_microphone_on_next_refresh():
    app = microphone_app()
    app.permission_manager.check_permission.return_value = False
    app._on_microphone_toggled = Mock()
    app._refresh_microphone_state()
    app._on_microphone_toggled.assert_called_once_with(False)


@pytest.mark.parametrize("permission_id,service", [("automation", "automation_engine"),
                                                    ("proactive_read", "proactive_policy")])
def test_permission_change_controls_existing_background_service(permission_id, service):
    app = bare_app()
    worker = Mock()
    setattr(app, service, worker)
    app._on_permission_changed(permission_id, False)
    worker.stop.assert_called_once()
    worker.start.assert_not_called()
    app._on_permission_changed(permission_id, True)
    worker.start.assert_called_once()


def permission_request(*, persist=True, context=None):
    return {"id": "permission-request", "name": "파일 저장", "description": "검증용 파일 저장",
            "permission_id": "filesystem_write", "persist_decision": persist, "context": context,
            "microphone_generation": 0, "event": threading.Event(),
            "cancelled": threading.Event(), "result": None}


@pytest.mark.parametrize("persist", [True, False])
@pytest.mark.parametrize("choice,expected", [("허용", True), ("거절", False), ("close", None)])
def test_permission_card_requires_explicit_answer_and_explains_decision_scope(persist, choice, expected):
    app = bare_app()
    app.request_notifications = SimpleNamespace(enqueue=Mock(return_value=True))
    request = permission_request(persist=persist)
    app._on_action_permission_request(request)
    args, kwargs = app.request_notifications.enqueue.call_args
    assert args[0] == request["id"]
    assert ("저장됩니다" if persist else "이번 동작") in args[2]
    assert dict(args[3]) == {"거절": False, "허용": True}
    assert kwargs["is_valid"]() and not request["event"].is_set()
    kwargs["on_cancel"]() if choice == "close" else args[4](dict(args[3])[choice])
    assert request["event"].is_set() and request["result"] is expected
    app.window.request_permission.assert_not_called()


def test_permission_card_late_allow_cannot_apply_after_turn_cancelled():
    app = bare_app()
    app.request_notifications = SimpleNamespace(enqueue=Mock(return_value=True))
    context = TurnExecutionContext("turn", "chat")
    request = permission_request(context=context)
    app._on_action_permission_request(request)
    args, kwargs = app.request_notifications.enqueue.call_args
    context.cancel()
    assert not kwargs["is_valid"]()
    args[4](True)
    assert request["result"] is None and request["event"].is_set()


def test_unavailable_or_already_cancelled_request_does_not_leave_worker_waiting():
    app = bare_app()
    app.request_notifications = SimpleNamespace(enqueue=Mock(return_value=False))
    request = permission_request()
    app._on_action_permission_request(request)
    assert request["event"].is_set() and request["result"] is None
    app.request_notifications.enqueue.reset_mock()
    request = permission_request()
    request["cancelled"].set()
    app._on_action_permission_request(request)
    assert request["event"].is_set()
    app.request_notifications.enqueue.assert_not_called()


def pending_question_app(tmp_path, *, status="awaiting_user", question=None):
    app = bare_app()
    store = DialogueStateStore(str(tmp_path / "questions.db"))
    task = store.create_task("chat", "문서 저장", task_id="abc12345", workspace_path="C:/qa-project")
    store.update_task(task.task_id, status=status)
    app.executor = SimpleNamespace(dialogue_state=store)
    app.request_notifications = SimpleNamespace(enqueue=Mock(return_value=True))
    context = TurnExecutionContext("turn", "chat", "C:/qa-project")
    turn = TurnEnvelope("turn", "chat", "문서를 저장해 줘", (), workspace_path="C:/qa-project", execution=context)
    question = question or "어떤 형식으로 저장할까요?\n1. PDF\n2. DOCX"
    result = TurnResult(turn, question, status, question=question, task_id=task.task_id)
    return app, store, result


def test_question_button_resumes_exact_stored_task_with_chosen_literal(tmp_path):
    app, _store, result = pending_question_app(tmp_path)
    app._offer_pending_question(result)
    args, kwargs = app.request_notifications.enqueue.call_args
    assert args[0] == "question:turn" and args[2] == result.question
    assert kwargs["is_valid"]()
    args[4](dict(args[3])["DOCX"])
    app._on_user_input.assert_called_once_with("DOCX", execution_text="abc12345 재개: DOCX")


@pytest.mark.parametrize("literal", ["__write__", "__later__", "later", "approve", "cancel"])
def test_question_literal_choices_never_collide_with_notification_commands(tmp_path, literal):
    question = f"어느 항목을 선택할까요?\n1. {literal}\n2. 다른 항목"
    app, _store, result = pending_question_app(tmp_path, question=question)
    app._offer_pending_question(result)
    args = app.request_notifications.enqueue.call_args.args
    args[4](dict(args[3])[literal])
    app._on_user_input.assert_called_once_with(literal, execution_text=f"abc12345 재개: {literal}")


@pytest.mark.parametrize("change", ["session", "turn", "cancel", "shutdown", "completed", "revised", "deleted"])
def test_queued_question_cannot_resume_changed_conversation_or_task(tmp_path, change):
    app, store, result = pending_question_app(tmp_path)
    app._offer_pending_question(result)
    args, kwargs = app.request_notifications.enqueue.call_args
    if change == "session":
        app.session_id = "another-chat"
    elif change == "turn":
        app._active_turn_id = "new-turn"
    elif change == "cancel":
        result.turn.execution.cancel()
    elif change == "shutdown":
        app._runtime_shutdown_started = True
    elif change == "completed":
        store.update_task(result.task_id, status="completed")
    elif change == "revised":
        store.update_task(result.task_id, pending_question="새로운 질문")
    elif change == "deleted":
        store.update_task(result.task_id, status="cancelled")
        assert store.delete_task("chat", result.task_id, "C:/qa-project")
    assert not kwargs["is_valid"]()
    args[4](dict(args[3])["PDF"])
    app._on_user_input.assert_not_called()


@pytest.mark.parametrize("state", ["completed", "deleted", "wrong_workspace"])
def test_stale_stored_task_is_rejected_before_question_is_offered(tmp_path, state):
    app, store, result = pending_question_app(tmp_path)
    if state == "completed":
        store.update_task(result.task_id, status="completed")
    elif state == "deleted":
        store.update_task(result.task_id, status="cancelled")
        store.delete_task("chat", result.task_id, "C:/qa-project")
    else:
        store.update_task(result.task_id, workspace_path="C:/another-project")
    app._offer_pending_question(result)
    app.request_notifications.enqueue.assert_not_called()


@pytest.mark.parametrize("accepted,text,expected", [(True, "새  문서.txt", "새  문서.txt"),
                                                    (False, "답변", None), (True, "  ", None)])
def test_freeform_reply_uses_explicit_text_or_keeps_question_waiting(tmp_path, monkeypatch, accepted, text, expected):
    app, _store, result = pending_question_app(tmp_path, question="파일 이름을 알려주세요.")
    monkeypatch.setattr("main_qt.QInputDialog.getText", lambda *_args: (text, accepted))
    app._offer_pending_question(result)
    args = app.request_notifications.enqueue.call_args.args
    args[4](dict(args[3])["직접 답변"])
    if expected:
        app._on_user_input.assert_called_once_with(expected, execution_text=f"abc12345 재개: {expected}")
    else:
        app._on_user_input.assert_not_called()


@pytest.mark.parametrize("choice,command", [("승인", "승인"), ("작업 취소", "취소"), ("나중에", None)])
def test_approval_buttons_use_stored_task_not_task_ids_in_question_prose(tmp_path, choice, command):
    app, _store, result = pending_question_app(tmp_path, status="awaiting_approval",
                                              question="검증용 승인 요청 · 본문에 있는 deadbeef 승인 문구")
    app._offer_pending_question(result)
    args = app.request_notifications.enqueue.call_args.args
    args[4](dict(args[3])[choice])
    if command:
        app._on_user_input.assert_called_once_with(f"작업 {command}", execution_text=f"abc12345 {command}")
    else:
        app._on_user_input.assert_not_called()


def test_background_result_keeps_question_and_task_identity_for_visible_notification():
    app = bare_app()
    app.signals.ai_response_ready = Mock()
    app.signals.progress_update = Mock()
    app.executor.execute_turn.return_value = SimpleNamespace(
        response="확인이 필요합니다.", status="awaiting_user", question="어떤 형식으로 저장할까요?",
        task_id="abc12345", next_goal="")
    turn = TurnEnvelope("turn", "chat", "문서를 저장해 줘", (),
                        execution=TurnExecutionContext("turn", "chat"))
    app._process_ai(turn)
    result = app.signals.ai_response_ready.emit.call_args.args[0]
    assert result.turn is turn and result.status == "awaiting_user"
    assert result.task_id == "abc12345" and result.question == "어떤 형식으로 저장할까요?"


def test_blocking_permission_in_settings_invalidates_pending_allow_button(tmp_path):
    app = bare_app()
    app.permission_manager = PermissionManager(str(tmp_path / "permissions.json"))
    app.request_notifications = SimpleNamespace(enqueue=Mock(return_value=True))
    request = permission_request()
    app._on_action_permission_request(request)
    args, kwargs = app.request_notifications.enqueue.call_args
    assert kwargs["is_valid"]()
    app.permission_manager.revoke_permission("filesystem_write")
    assert not kwargs["is_valid"]()
    args[4](True)
    assert request["result"] is None and request["event"].is_set()
    assert app.permission_manager.is_blocked("filesystem_write")
    assert PermissionManager(str(tmp_path / "permissions.json")).is_blocked("filesystem_write")


def test_setting_permission_block_during_prompt_cannot_be_overwritten_by_late_allow(tmp_path):
    manager = PermissionManager(str(tmp_path / "permissions.json"))
    def answer_after_settings_changed(permission):
        manager.revoke_permission(permission.id)
        return True
    manager.set_request_callback(answer_after_settings_changed)
    assert not manager.request_permission("filesystem_write")
    assert manager.is_blocked("filesystem_write")
    assert PermissionManager(str(tmp_path / "permissions.json")).is_blocked("filesystem_write")


@pytest.mark.parametrize("visible,internal", [("DOCX", "abc12345 재개: DOCX"),
                                                ("작업 승인", "abc12345 승인")])
def test_actual_input_and_response_flow_never_displays_or_saves_internal_task_command(tmp_path, monkeypatch, visible, internal):
    app = bare_app()
    del app._on_user_input  # Exercise the real UI input handler and result persistence.
    app.memory = ConversationMemory(str(tmp_path / "memory.db"))
    app.session_id = app.memory.create_session("검증용 대화")
    app.messages = []
    app.state_machine = Mock()
    app.assistant_settings = SimpleNamespace(wake_word="", assistant_name="아니스", tts_enabled=False)
    monkeypatch.setattr("main_qt.get_assistant_settings", lambda: app.assistant_settings)
    app._cancel_speech_only = Mock()
    app._cancel_pending_work = Mock()
    app._archive_obsidian_exchange_async = Mock()
    app._consolidate_memory_async = Mock()
    app._personalize_address = lambda text: text
    app._reset_all = Mock()
    app.executor.execute_turn.return_value = SimpleNamespace(
        response="선택한 답변을 확인했습니다.", status="completed", question="", task_id="abc12345", next_goal="")
    delivered = []
    app.signals.ai_response_ready = SimpleNamespace(emit=delivered.append)
    app.signals.progress_update = Mock()
    class InlineThread:
        def __init__(self, *, target, args=(), daemon):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    monkeypatch.setattr("main_qt.threading.Thread", InlineThread)
    app._on_user_input(visible, execution_text=internal)
    assert len(delivered) == 1
    result = delivered[0]
    assert result.turn.user_text == visible and result.turn.execution_text == internal
    app.window.show_user_text.assert_called_once_with(visible)
    assert app.executor.execute_turn.call_args.args[0] == internal
    app._on_ai_response(result)
    assert app.memory.load_session(app.session_id)[0] == {"role": "user", "content": visible}
    assert all("abc12345" not in item["content"] for item in app.memory.load_session(app.session_id))
    assert all("abc12345" not in item["content"] for item in app.messages)
    assert app._archive_obsidian_exchange_async.call_args.args[0] == visible
    assert app._consolidate_memory_async.call_args.args[0] == visible
