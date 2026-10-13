"""Synthetic background inbox UI; no credentials, browser, model or network."""
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QLabel

from core.browser_mail import BrowserMailError
from test_oauth_account_dialog import OAuth
from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS, _MailOperation


ACCOUNT = "owner@example.test"
ITEM = {"sender": "Fixture", "subject": "Work", "date": "2026-10-13", "unread": True,
        "message_ref": "fixture-message"}


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.instance().processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


class GmailOAuth(OAuth):
    SPECS = {"google": {**OAuth.SPECS["google"], "scopes": [
        "openid", "email", "https://www.googleapis.com/auth/gmail.readonly"]}}

    def active_account(self):
        return ACCOUNT


@pytest.fixture
def inbox():
    application = QApplication.instance() or QApplication([])
    gmail = GmailOAuth()
    service = SimpleNamespace(
        background=True, workflow_lock=threading.Lock(), gmail=gmail,
        naver=SimpleNamespace(status=lambda: {"configured": False, "username": ""}),
        analysis=Mock(), disconnect=Mock(), switch_provider=Mock(), check_snapshot=Mock(),
        connection_status=Mock(side_effect=lambda provider: {"configured": provider == "gmail",
                                                              "account": ACCOUNT if provider == "gmail" else ""}),
        list_current=Mock(return_value={"provider": "gmail", "account": ACCOUNT, "scope": "inbox",
                                       "items": [dict(ITEM)], "observed_count": 1}),
        read_message=Mock(return_value={"provider": "gmail", "account": ACCOUNT, "message_ref": ITEM["message_ref"],
                                        "subject": ITEM["subject"], "sender": ITEM["sender"], "date": ITEM["date"],
                                        "body": "Fixture plain body", "truncated": False}),
    )
    service.analysis.settings.return_value = {"enabled": False}
    dialog = MailInboxDialog(service)
    dialog.show()
    yield dialog, service
    dialog.reject()
    assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
    assert application is QApplication.instance()


def test_background_list_inline_body_and_cache_clear_without_browser_or_read_change(inbox):
    dialog, service = inbox
    assert dialog.open_button.isHidden() and dialog.login_button.isHidden()
    labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))
    assert "Chrome" in labels  # Explains that no Chrome window is opened.
    assert "Allow & select" not in labels and "자동 연결 토큰" not in labels
    assert "실제 접속은 조회 시 확인" in dialog.connection_label.text()
    dialog.list_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert dialog.table.rowCount() == 1 and "최대 50건" in dialog.count_label.text()
    assert "백그라운드에서 조회" in dialog.status_label.text()
    service.list_current.assert_called_once()
    dialog.table.cellClicked.emit(0, 1)
    assert pump_until(lambda: dialog._worker is None)
    assert dialog._body_view.toPlainText() == "Fixture plain body"
    assert dialog._items[0]["unread"] is True and "읽음 상태는 변경하지 않습니다" in dialog.status_label.text()
    dialog.disconnect_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert not dialog._items and "저장된 계정 연결은 유지" in dialog.status_label.text()
    assert service.disconnect.call_count == 1


def test_scope_validation_zero_inbox_and_provider_status(inbox):
    dialog, service = inbox
    with pytest.raises(ValueError):
        dialog._apply_result({"provider": "gmail", "account": ACCOUNT, "scope": "current_view",
                              "items": [], "observed_count": 0})
    dialog._apply_result({"provider": "gmail", "account": ACCOUNT, "scope": "inbox",
                          "items": [], "observed_count": 0})
    assert dialog.table.rowCount() == 0 and "0건" in dialog.count_label.text()
    dialog.provider.setCurrentIndex(1)
    assert pump_until(lambda: dialog._worker is None)
    assert "미연결" in dialog.connection_label.text()
    service.switch_provider.assert_called_once_with()
    service.disconnect.assert_not_called()


@pytest.mark.parametrize("provider_index", [0, 1])
def test_settings_reuses_provider_dialog_and_holds_shared_workflow_lock(inbox, monkeypatch, provider_index):
    from ui.oauth_account_dialog import OAuthAccountDialog
    from ui.mail_account_dialog import MailAccountDialog
    dialog, service = inbox
    dialog.provider.setCurrentIndex(provider_index)
    assert pump_until(lambda: dialog._worker is None)
    opened = []

    def execute(child):
        assert service.workflow_lock.locked()
        assert not dialog.connection_button.isEnabled() and not dialog.provider.isEnabled()
        if provider_index == 0:
            assert child.oauth is service.gmail and child.gmail_only
            assert child.provider.count() == 1 and child.account_input.text() == ACCOUNT
        else:
            assert child.service is service.naver
        opened.append(child.windowTitle())
        return 0

    monkeypatch.setattr(OAuthAccountDialog, "exec", execute)
    monkeypatch.setattr(MailAccountDialog, "exec", execute)
    dialog.connection_button.click()
    assert len(opened) == 1 and not service.workflow_lock.locked()
    service.disconnect.assert_not_called()
    assert dialog.connection_button.isEnabled() and dialog.provider.isEnabled()
    service.workflow_lock.acquire()
    try:
        dialog.connection_button.click()
        assert len(opened) == 1 and "다른 메일 작업" in dialog.status_label.text()
    finally:
        service.workflow_lock.release()


def test_background_failure_hides_arbitrary_server_text_and_guides_account_settings(inbox):
    dialog, service = inbox
    service.list_current.side_effect = RuntimeError("fixture-secret-server-content")
    dialog.list_button.click()
    assert pump_until(lambda: dialog._worker is None)
    assert "백그라운드 계정 설정" in dialog.status_label.text()
    assert "fixture-secret" not in dialog.status_label.text()
    assert "Chrome 로그인" not in dialog.status_label.text() and not dialog._items


@pytest.mark.parametrize("when", ["before", "after"])
def test_cached_analysis_still_checks_active_account_before_and_after(inbox, when):
    _dialog, service = inbox
    checks = []

    def guard(provider, account):
        assert (provider, account) == ("gmail", ACCOUNT)
        checks.append(True)
        if when == "before" or len(checks) > 1:
            raise BrowserMailError("메일 계정이 변경되었습니다. 다시 조회하세요.")

    service.check_snapshot.side_effect = guard
    service.analysis.collect.return_value = {"processed": 0, "partial": 0, "failed": 0, "skipped": 1}
    worker = _MailOperation(service, "collect", "gmail", account=ACCOUNT, items=[dict(ITEM)])
    outcomes = []
    worker.outcome.connect(lambda kind, payload: outcomes.append((kind, payload)))
    worker.run()
    assert outcomes[0][0] == "failed" and "계정이 변경" in outcomes[0][1]["message"]
    assert service.analysis.collect.call_count == (0 if when == "before" else 1)
    assert not service.workflow_lock.locked()
