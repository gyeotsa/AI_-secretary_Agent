"""Offscreen mail settings UI tests with an injected fake service only."""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent, QTimer, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QMessageBox
import pytest

from ui.mail_account_dialog import MailAccountDialog, _ACTIVE_CHECKS


_QT_APP = None


def app():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def pump_until(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app().processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakeService:
    def __init__(self, configured=False):
        self.current_status = {"configured": configured, "username": "fixture@naver.com" if configured else "",
                               "verified_at": None, "reason": ""}
        self.calls = []
        self.error = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.finished = threading.Event()
        self.cooperative = True
        self.verify_thread = None
        self.verify_result = {"imap_authenticated": True, "smtp_authenticated": True,
                              "verified_at": "2026-09-15T10:00:00+09:00", "reason": ""}

    def status(self):
        self.calls.append(("status",))
        if self.error == "status":
            raise RuntimeError("fixture-private-password")
        return dict(self.current_status)

    def save(self, username, password):
        self.calls.append(("save", username))
        assert password, "UI must submit the newly supplied password"
        if self.error == "save":
            raise RuntimeError(password)
        self.current_status = {"configured": True, "username": username, "verified_at": None,
                               "reason": "fixture-private-password", "password": "fixture-private-password"}
        return dict(self.current_status)

    def verify(self, checkpoint):
        self.calls.append(("verify",))
        self.verify_thread = threading.get_ident()
        self.started.set()
        try:
            while not self.release.wait(0.005):
                if self.cooperative:
                    checkpoint()
            checkpoint()
            if self.error == "verify":
                raise RuntimeError("fixture-private-password")
            return dict(self.verify_result)
        finally:
            self.finished.set()

    def disconnect(self):
        self.calls.append(("disconnect",))
        if self.error == "disconnect":
            raise RuntimeError("fixture-private-password")
        self.current_status = {"configured": False, "username": "", "verified_at": None, "reason": ""}
        return dict(self.current_status)


@pytest.fixture
def make_dialog():
    app()
    created = []

    def create(service=None):
        service = service or FakeService()
        dialog = MailAccountDialog(service)
        created.append((dialog, service))
        dialog.show()
        app().processEvents()
        return dialog, service

    yield create
    for dialog, service in created:
        service.release.set()
        try:
            dialog.reject()
        except RuntimeError:
            pass  # Explicit QObject-destruction lifecycle test.
    assert pump_until(lambda: not _ACTIVE_CHECKS), "Connection-check worker did not finish"
    for dialog, _service in created:
        try:
            dialog.deleteLater()
        except RuntimeError:
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def visible_text(dialog):
    return "\n".join(label.text() for label in dialog.findChildren(QLabel))


def test_saved_account_never_autofills_secret_or_claims_verified(make_dialog):
    service = FakeService(configured=True)
    service.current_status.update(password="fixture-private-password", reason="fixture-private-password")
    dialog, _service = make_dialog(service)
    assert dialog.username_input.text() == "fixture@naver.com"
    assert dialog.password_input.echoMode() == QLineEdit.EchoMode.Password
    assert dialog.password_input.text() == ""
    assert "fixture-private-password" not in visible_text(dialog)
    assert "저장됨" in dialog.status_label.text() and "연결 미확인" in dialog.status_label.text()
    assert dialog.verify_button.isEnabled() and not dialog.save_button.isEnabled()
    assert service.calls == [("status",)]
    assert "캘린더" not in visible_text(dialog)


def test_previous_verification_is_labelled_as_historical(make_dialog):
    service = FakeService(configured=True)
    service.current_status["verified_at"] = "2026-09-15T09:00:00+09:00"
    dialog, _service = make_dialog(service)
    assert "마지막 연결 확인" in dialog.status_label.text()
    assert "현재 접속 가능 여부는 다릅니다" in dialog.status_label.text()


def test_save_clears_password_without_automatic_verification_or_mail_calls(make_dialog, capsys):
    dialog, service = make_dialog()
    dialog.username_input.setText("new@naver.com")
    dialog.password_input.setText("fixture-private-password")
    assert dialog.save_button.isEnabled()
    dialog.save_button.click()
    assert dialog.password_input.text() == ""
    assert "연결 미확인" in dialog.status_label.text()
    assert service.calls == [("status",), ("save", "new@naver.com")]
    assert "fixture-private-password" not in visible_text(dialog)
    captured = capsys.readouterr()
    assert "fixture-private-password" not in captured.out + captured.err


def test_save_failure_does_not_display_exception_or_keep_password(make_dialog):
    dialog, service = make_dialog()
    service.error = "save"
    dialog.username_input.setText("new@naver.com")
    dialog.password_input.setText("fixture-private-password")
    dialog.save_button.click()
    assert dialog.password_input.text() == ""
    assert "완료하지 못했습니다" in dialog.status_label.text()
    assert "fixture-private-password" not in visible_text(dialog)
    assert not dialog.verify_button.isEnabled()


def test_status_failure_and_invalid_timestamp_do_not_leak_service_text(make_dialog):
    service = FakeService(configured=True)
    service.error = "status"
    dialog, _service = make_dialog(service)
    assert "fixture-private-password" not in visible_text(dialog)
    assert not dialog.verify_button.isEnabled()
    service2 = FakeService(configured=True)
    service2.current_status["verified_at"] = "fixture-private-password"
    other, _service = make_dialog(service2)
    assert "fixture-private-password" not in visible_text(other)
    assert "연결 미확인" in other.status_label.text()


def test_unsaved_account_or_password_disables_check_of_old_credentials(make_dialog):
    dialog, service = make_dialog(FakeService(configured=True))
    dialog.username_input.setText("different@naver.com")
    assert not dialog.verify_button.isEnabled()
    dialog._verify()
    assert service.calls == [("status",)]
    dialog.username_input.setText("fixture@naver.com")
    dialog.password_input.setText("new-secret")
    assert not dialog.verify_button.isEnabled()
    dialog.password_input.clear()
    assert dialog.verify_button.isEnabled()


def test_verify_is_background_only_ui_stays_responsive_and_mutations_are_disabled(make_dialog, monkeypatch):
    service = FakeService(configured=True)
    service.release.clear()
    dialog, _service = make_dialog(service)
    gui_thread = threading.get_ident()
    label_threads = []
    set_text = dialog.status_label.setText

    def checked_set_text(text):
        label_threads.append(threading.get_ident())
        assert threading.get_ident() == gui_thread
        set_text(text)

    monkeypatch.setattr(dialog.status_label, "setText", checked_set_text)
    ticks = []
    timer = QTimer(dialog)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    dialog.verify_button.click()
    assert pump_until(lambda: service.started.is_set() and len(ticks) >= 2)
    assert service.verify_thread != gui_thread
    assert not dialog.username_input.isEnabled() and not dialog.password_input.isEnabled()
    assert not dialog.save_button.isEnabled() and not dialog.verify_button.isEnabled()
    assert not dialog.disconnect_button.isEnabled()
    assert dialog.cancel_button.isEnabled()
    dialog._save()
    dialog._disconnect()
    dialog._verify()
    assert service.calls == [("status",), ("verify",)]
    service.release.set()
    assert pump_until(lambda: dialog._worker is None)
    assert "로그인 확인 완료" in dialog.status_label.text()
    assert "메일 조회·전송은 하지 않았습니다" in dialog.status_label.text()
    assert all(value == gui_thread for value in label_threads)
    assert dialog.verify_button.isEnabled() and not dialog.cancel_button.isEnabled()
    timer.stop()


@pytest.mark.parametrize("mode", ["exception", "partial", "missing-timestamp", "truthy-not-bool"])
def test_failed_or_partial_verify_never_claims_full_success_or_leaks_reason(make_dialog, mode, capsys):
    service = FakeService(configured=True)
    service.verify_result["reason"] = "fixture-private-password"
    if mode == "exception":
        service.error = "verify"
    elif mode == "partial":
        service.verify_result["smtp_authenticated"] = False
    elif mode == "missing-timestamp":
        service.verify_result["verified_at"] = "fixture-private-password"
    else:
        service.verify_result["smtp_authenticated"] = "true"
    dialog, _service = make_dialog(service)
    dialog.verify_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert "로그인 확인 완료" not in dialog.status_label.text()
    assert "fixture-private-password" not in visible_text(dialog)
    captured = capsys.readouterr()
    assert "fixture-private-password" not in captured.out + captured.err


def test_cancel_cooperatively_stops_check_and_keeps_dialog_and_saved_state(make_dialog):
    service = FakeService(configured=True)
    service.release.clear()
    dialog, _service = make_dialog(service)
    dialog.verify_button.click()
    assert pump_until(service.started.is_set)
    dialog.cancel_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert dialog.isVisible() and "취소했습니다" in dialog.status_label.text()
    assert service.current_status["configured"]
    assert not any(call[0] == "disconnect" for call in service.calls)


def test_cancel_after_worker_completion_before_queued_gui_result_hides_stale_success(make_dialog):
    dialog, service = make_dialog(FakeService(configured=True))
    dialog.verify_button.click()
    worker = dialog._worker
    # No event pumping: the worker is done but the GUI has not consumed signals.
    assert worker.wait(1000)
    assert service.finished.is_set() and dialog._worker is worker
    dialog._cancel()
    assert pump_until(lambda: dialog._worker is None)
    assert "취소했습니다" in dialog.status_label.text()
    assert "로그인 확인 완료" not in dialog.status_label.text()


def test_save_without_confirmation_never_claims_saved(make_dialog, monkeypatch):
    dialog, service = make_dialog()
    monkeypatch.setattr(service, "save", lambda *_args: {"configured": False, "reason": "fixture-private-password"})
    dialog.username_input.setText("fixture@naver.com")
    dialog.password_input.setText("fixture-private-password")
    dialog.save_button.click()
    assert "완료하지 못했습니다" in dialog.status_label.text()
    assert not dialog.verify_button.isEnabled()
    assert dialog.password_input.text() == ""
    assert "fixture-private-password" not in visible_text(dialog)


@pytest.mark.parametrize("username", [None, ""])
def test_malformed_configured_identity_does_not_enable_connection_check(make_dialog, username):
    service = FakeService(configured=True)
    service.current_status["username"] = username
    dialog, _service = make_dialog(service)
    assert not dialog.verify_button.isEnabled()
    assert "확인하지 못했습니다" in dialog.status_label.text()


@pytest.mark.parametrize("close_method", ["close", "reject", "done"])
def test_close_waits_for_inflight_worker_and_discards_late_success(make_dialog, close_method):
    service = FakeService(configured=True)
    service.release.clear()
    service.cooperative = False  # A bounded SSL call cannot checkpoint mid-call.
    dialog, _service = make_dialog(service)
    dialog.verify_button.click()
    assert pump_until(service.started.is_set)
    worker = dialog._worker
    if close_method == "done":
        dialog.done(0)
    else:
        getattr(dialog, close_method)()
    app().processEvents()
    assert dialog.isVisible() and worker.isRunning()
    assert not dialog.cancel_button.isEnabled()
    assert "취소" in dialog.status_label.text()
    service.release.set()
    assert pump_until(lambda: not dialog.isVisible() and not _ACTIVE_CHECKS)
    assert "로그인 확인 완료" not in dialog.status_label.text()


def test_direct_qobject_destruction_retains_and_cancels_worker(make_dialog):
    service = FakeService(configured=True)
    service.release.clear()
    dialog, _service = make_dialog(service)
    qt_messages = []
    previous_handler = qInstallMessageHandler(lambda _type, _context, text: qt_messages.append(text))
    try:
        dialog.verify_button.click()
        assert pump_until(service.started.is_set)
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert pump_until(lambda: service.finished.is_set() and not _ACTIVE_CHECKS)
        assert not any("Destroyed while thread" in value for value in qt_messages)
    finally:
        qInstallMessageHandler(previous_handler)


@pytest.mark.parametrize("confirmed", [False, True])
def test_disconnect_requires_confirmation_and_explains_separate_naver_revocation(make_dialog, monkeypatch, confirmed):
    dialog, service = make_dialog(FakeService(configured=True))
    prompts = []

    def confirm(_parent, title, text, buttons, default):
        prompts.append((title, text, buttons, default))
        return QMessageBox.StandardButton.Yes if confirmed else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", confirm)
    dialog.disconnect_button.click()
    assert len(prompts) == 1
    assert "로컬" in prompts[0][1] and "네이버 계정 설정에서 별도로 폐기" in prompts[0][1]
    assert prompts[0][3] == QMessageBox.StandardButton.No
    assert any(call[0] == "disconnect" for call in service.calls) is confirmed
    if confirmed:
        assert dialog.username_input.text() == "" and dialog.password_input.text() == ""
        assert not dialog.verify_button.isEnabled() and not dialog.disconnect_button.isEnabled()
        assert "별도로 폐기" in dialog.status_label.text()
    else:
        assert dialog.verify_button.isEnabled()


def test_disconnect_failure_remains_visible_without_secret_details(make_dialog, monkeypatch):
    dialog, service = make_dialog(FakeService(configured=True))
    service.error = "disconnect"
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    dialog.disconnect_button.click()
    assert "확인하지 못했습니다" in dialog.status_label.text()
    assert "fixture-private-password" not in visible_text(dialog)
    assert dialog.disconnect_button.isEnabled()


def test_close_without_worker_erases_unsaved_password(make_dialog):
    dialog, service = make_dialog()
    dialog.password_input.setText("fixture-private-password")
    dialog.close()
    assert dialog.password_input.text() == ""
    assert service.calls == [("status",)]
