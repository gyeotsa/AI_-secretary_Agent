import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core.mail_brief import MailBriefError
from core.turn_context import TurnExecutionContext, current_turn_context
from main_qt import AppSignals, JarvisApp, TurnEnvelope


_QT_APP = None


def app_with_brief(monkeypatch):
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    app = JarvisApp.__new__(JarvisApp)
    app._runtime_shutdown_started = False
    app._mail_startup_context = None
    app._is_processing_ai = False
    app.session_id = "current-chat"
    app.window = SimpleNamespace(status_label=Mock())
    app.mail_brief = Mock()
    app.signals = AppSignals()
    app.signals.mail_brief_ready.connect(app._on_startup_mail_brief)
    app.signals.mail_brief_progress.connect(app._on_startup_mail_progress)
    app._on_proactive_message = Mock()

    class InlineThread:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr("main_qt.threading.Thread", InlineThread)
    return app


def test_first_start_reuses_collect_and_commits_after_visible_delivery(monkeypatch):
    app = app_with_brief(monkeypatch)
    app.mail_brief.claim_startup.return_value = "boot-one"
    summary = "네이버 신규 3건 · 회신 검토 1건 · 미발송 초안 2건"
    order = []

    def collect(*, checkpoint, progress, wait):
        assert wait is True
        assert current_turn_context().session_id == "startup-mail"
        checkpoint()
        # Changing chats during collection must not drop the first boot notification.
        app.session_id = "new-chat"
        progress({"provider": "naver", "phase": "analysis", "processed": 1,
                  "skipped": 2, "failed": 1, "partial": 1, "total": 5})
        return {"summary": summary}

    app.mail_brief.collect.side_effect = collect
    app._on_proactive_message.side_effect = lambda payload: order.append(("show", payload))
    app.mail_brief.finish_startup.side_effect = lambda *args, **kwargs: order.append(("commit", args))
    app._start_startup_mail_brief()
    assert order == [("show", {"session_id": "new-chat", "text": summary}), ("commit", ("boot-one",))]
    app.mail_brief.finish_startup.assert_called_once_with("boot-one", delivered=True)
    assert any("5/5" in str(call) for call in app.window.status_label.setText.call_args_list)
    assert app._mail_startup_context is None


def test_same_boot_does_not_collect_or_notify(monkeypatch):
    app = app_with_brief(monkeypatch)
    app.mail_brief.claim_startup.return_value = None
    app._start_startup_mail_brief()
    app.mail_brief.collect.assert_not_called()
    app._on_proactive_message.assert_not_called()
    app.mail_brief.finish_startup.assert_not_called()


def test_boot_detection_failure_is_safe_status_and_no_notification(monkeypatch):
    app = app_with_brief(monkeypatch)
    app.mail_brief.claim_startup.side_effect = MailBriefError("Windows 부팅 기준 확인 불가")
    app._start_startup_mail_brief()
    app.mail_brief.collect.assert_not_called()
    app._on_proactive_message.assert_not_called()
    app.window.status_label.setText.assert_called_once_with("Windows 부팅 기준 확인 불가")


def test_collection_error_does_not_expose_private_exception_or_mark_boot(monkeypatch):
    app = app_with_brief(monkeypatch)
    app.mail_brief.claim_startup.return_value = "boot-one"
    app.mail_brief.collect.side_effect = RuntimeError("private-mail-and-token-marker")
    app._start_startup_mail_brief()
    app.mail_brief.cancel_startup.assert_called_once()
    app.mail_brief.finish_startup.assert_not_called()
    app._on_proactive_message.assert_not_called()
    assert "private" not in str(app.window.status_label.setText.call_args)


def test_cancelled_late_result_is_never_delivered(monkeypatch):
    app = app_with_brief(monkeypatch)
    context = TurnExecutionContext("turn", "startup-mail")
    app._mail_startup_context = context
    context.cancel()
    app._on_startup_mail_brief({"context": context, "boot_id": "boot", "summary": "counts"})
    app._on_proactive_message.assert_not_called()
    app.mail_brief.cancel_startup.assert_called_once()
    app.mail_brief.finish_startup.assert_not_called()


def test_commit_failure_is_reported_and_lease_released(monkeypatch):
    app = app_with_brief(monkeypatch)
    context = TurnExecutionContext("turn", "startup-mail")
    app._mail_startup_context = context
    app.mail_brief.finish_startup.side_effect = OSError("private-vault-marker")
    app._on_startup_mail_brief({"context": context, "boot_id": "boot", "summary": "counts"})
    app._on_proactive_message.assert_called_once()
    app.mail_brief.cancel_startup.assert_called_once()
    assert "저장" in app.window.status_label.setText.call_args.args[0]
    assert "private" not in app.window.status_label.setText.call_args.args[0]


def test_duplicate_launch_callback_does_not_start_another_worker(monkeypatch):
    app = app_with_brief(monkeypatch)
    app._mail_startup_context = TurnExecutionContext("turn", "startup-mail")
    app._start_startup_mail_brief()
    app.mail_brief.claim_startup.assert_not_called()


def test_conversation_preserves_verified_workflow_counts_without_model_rewrite():
    app = JarvisApp.__new__(JarvisApp)
    app.signals = SimpleNamespace(ai_response_ready=Mock())
    app.executor = Mock()
    app.workflow_runtime = Mock()
    app.workflow_runtime.match_trigger.return_value = "mail_brief"
    summary = "네이버 신규 3건 · Gmail 신규 2건 · 미발송 초안 4건"
    app.workflow_runtime.execute.return_value = {
        "status": "completed", "results": [{"status": "completed", "output": {"presentation": summary}}],
    }
    app.workflow_runtime.present_run.return_value = summary
    turn = TurnEnvelope("mail-turn", "mail-chat", "메일 현황 알려줘", tuple())
    app._process_ai(turn)
    result = app.signals.ai_response_ready.emit.call_args.args[0]
    assert result.response_text == summary
    assert result.status == "completed"
    app.executor.render_outcome.assert_not_called()
    app.executor.execute_turn.assert_not_called()


def test_failed_workflow_conversation_keeps_failed_status():
    app = JarvisApp.__new__(JarvisApp)
    app.signals = SimpleNamespace(ai_response_ready=Mock())
    app.executor = Mock()
    app.workflow_runtime = Mock()
    app.workflow_runtime.match_trigger.return_value = "mail_brief"
    app.workflow_runtime.execute.return_value = {"status": "failed", "results": []}
    app.workflow_runtime.present_run.return_value = "메일 수집 실패"
    app.executor.render_outcome.return_value = SimpleNamespace(response="메일 수집 실패")
    app._process_ai(TurnEnvelope("mail-turn", "mail-chat", "메일 현황 알려줘", tuple()))
    result = app.signals.ai_response_ready.emit.call_args.args[0]
    assert result.status == "failed"
    assert app.executor.render_outcome.call_args.args[0].status == "failed"
