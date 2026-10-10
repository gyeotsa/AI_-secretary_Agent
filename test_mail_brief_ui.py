"""Synthetic shared mail workflow UI checks; no user mail, model or token."""
import os
import threading
import time
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from core.mail_brief import MailBriefError
from core.turn_context import current_turn_context
from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS


@pytest.fixture(scope="module", autouse=True)
def app():
    application = QApplication.instance() or QApplication([])
    yield application


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.instance().processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


class Brief:
    def __init__(self, browser):
        self.browser = browser
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.calls = []
        self.result = {"providers": [], "summary": "네이버: 신규 수신 2건\nGmail: 회신 검토(추정) 1건 <b>문자 그대로</b>"}
        self.error = None

    def collect(self, *, checkpoint, progress):
        if not self.browser.workflow_lock.acquire(blocking=False):
            raise MailBriefError("다른 메일 작업이 진행 중입니다.")
        try:
            self.calls.append(current_turn_context())
            self.entered.set()
            progress({"provider": "naver", "phase": "analysis"})
            self.release.wait(3)
            checkpoint()
            if self.error:
                raise self.error
            return self.result
        finally:
            self.browser.workflow_lock.release()


@pytest.fixture
def inbox():
    credentials = Mock()
    credentials.status.return_value = {"configured": True}
    analysis = Mock()
    analysis.settings.return_value = {"enabled": False, "categories": []}
    browser = Mock(connection_credentials=credentials, analysis=analysis, workflow_lock=threading.Lock())
    brief = Brief(browser)
    dialog = MailInboxDialog(browser, brief_service=brief)
    yield dialog, browser, brief
    brief.release.set()
    if browser.workflow_lock.locked() and not dialog._worker:
        browser.workflow_lock.release()
    dialog.reject()
    assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_manual_brief_uses_core_lock_once_and_displays_exact_plaintext_summary(inbox):
    dialog, browser, brief = inbox
    brief.release.clear()
    dialog.brief_button.click()
    assert pump_until(brief.entered.is_set)
    assert dialog._worker.operation == "brief"
    assert brief.calls[0] is dialog._worker.context and brief.calls[0].session_id == "mail"
    assert not dialog.brief_button.isEnabled() and not dialog.category_button.isEnabled()
    assert not dialog.connection_button.isEnabled() and not dialog.login_button.isEnabled()
    dialog.brief_button.click()
    dialog._start("list")
    assert len(brief.calls) == 1
    brief.release.set()
    assert pump_until(lambda: dialog._worker is None)
    assert dialog.status_label.text() == brief.result["summary"]
    assert dialog.status_label.textFormat() == Qt.TextFormat.PlainText and dialog.status_label.wordWrap()
    assert dialog.brief_button.isEnabled() and not browser.workflow_lock.locked()
    browser.list_current.assert_not_called()
    browser.analysis.collect.assert_not_called()


@pytest.mark.parametrize("action", ["cancel", "close", "provider"])
def test_brief_cancellation_interrupts_context_and_never_displays_late_summary(inbox, action):
    dialog, browser, brief = inbox
    brief.release.clear()
    dialog.brief_button.click()
    assert pump_until(brief.entered.is_set)
    context = brief.calls[0]
    if action == "close":
        dialog.reject()
    elif action == "provider":
        dialog.provider.setCurrentIndex(1)
    else:
        dialog.cancel_button.click()
    assert context.cancelled
    brief.release.set()
    assert pump_until(lambda: dialog._worker is None)
    assert dialog.status_label.text() != brief.result["summary"]
    assert not browser.workflow_lock.locked() and browser.disconnect.called


@pytest.mark.parametrize("operation", ["list", "open", "body", "collect", "switch", "disconnect"])
def test_busy_shared_workflow_never_runs_or_disconnects_another_operation(inbox, operation):
    dialog, browser, brief = inbox
    browser.workflow_lock.acquire()
    dialog._start(operation, "synthetic-message-ref")
    assert pump_until(lambda: dialog._worker is None)
    assert "다른 메일 작업" in dialog.status_label.text()
    assert browser.workflow_lock.locked()
    browser.disconnect.assert_not_called()
    browser.open.assert_not_called()
    browser.list_current.assert_not_called()
    browser.read_message.assert_not_called()
    browser.analysis.collect.assert_not_called()
    browser.switch_provider.assert_not_called()
    browser.workflow_lock.release()


def test_busy_shared_workflow_blocks_category_modal_and_login(inbox, monkeypatch):
    dialog, browser, _brief = inbox
    category = Mock()
    monkeypatch.setattr("ui.mail_inbox_dialog.MailCategoryDialog", category)
    login = Mock()
    dialog.login_requested.connect(login)
    browser.workflow_lock.acquire()
    dialog.category_button.click()
    dialog.login_button.click()
    category.assert_not_called()
    login.assert_not_called()
    assert "다른 메일 작업" in dialog.status_label.text()
    browser.workflow_lock.release()


def test_brief_busy_failure_does_not_disconnect_startup_collection(inbox):
    dialog, browser, brief = inbox
    browser.workflow_lock.acquire()
    dialog.brief_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert "다른 메일 작업" in dialog.status_label.text()
    assert not brief.calls and browser.workflow_lock.locked()
    browser.disconnect.assert_not_called()
    browser.workflow_lock.release()


@pytest.mark.parametrize("result,error", [(None, None), ({"summary": None}, None),
                                          ({"summary": "x" * 6001}, None),
                                          (None, RuntimeError("private-mail-and-token-marker"))])
def test_invalid_or_failed_brief_does_not_display_arbitrary_exception(inbox, result, error):
    dialog, _browser, brief = inbox
    brief.result, brief.error = result, error
    dialog.brief_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert "private-mail-and-token-marker" not in dialog.status_label.text()
    assert "확인하지 못했습니다" in dialog.status_label.text() or "조회하지 못했습니다" in dialog.status_label.text()


def test_late_close_after_brief_worker_completion_discards_queued_result(inbox):
    dialog, browser, brief = inbox
    dialog.brief_button.click()
    assert dialog._worker.wait(3000)
    dialog.reject()
    assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
    assert dialog.status_label.text() != brief.result["summary"]
    assert not browser.workflow_lock.locked()


def test_settings_constructor_failure_releases_shared_workflow(inbox, monkeypatch):
    dialog, browser, _brief = inbox
    monkeypatch.setattr("ui.mail_inbox_dialog.MailCategoryDialog", Mock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        dialog._show_category_settings()
    assert not browser.workflow_lock.locked()
    monkeypatch.setattr("ui.mail_inbox_dialog.MailConnectionDialog", Mock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        dialog._show_connection_settings()
    assert not browser.workflow_lock.locked()
