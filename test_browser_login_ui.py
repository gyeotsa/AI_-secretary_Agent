"""Offscreen Chrome login settings checks; no real vault, browser or account."""
import os
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QWidget

from ui.browser_login_dialog import BrowserLoginDialog, _ACTIVE_OPERATIONS, open_browser_login_dialog


_APP = None


def pump_until(predicate, timeout=3):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


class Service:
    def __init__(self):
        self.configured = False
        self.started, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.calls = []
        self.error = False

    def account_status(self, url):
        return {"configured": self.configured, "username": "fixture-id" if self.configured else "",
                "origin": "https://nid.naver.com", "password": "fixture-secret"}

    def save_account(self, url, username, password):
        assert username == "fixture-id" and password == "fixture-secret"
        self.calls.append(("save", threading.get_ident()))
        self.configured = True
        return self.account_status(url)

    def delete_account(self, url):
        self.configured = False
        return self.account_status(url)

    def open(self, url):
        return {"state": "opened", "origin": "https://nid.naver.com"}

    def inspect(self, url, *, checkpoint):
        self.started.set()
        while not self.release.wait(.005):
            checkpoint()
        checkpoint()
        if self.error:
            raise RuntimeError("fixture-secret")
        return {"state": "login_required", "origin": "https://nid.naver.com",
                "username_field": True, "password_field": True, "submit": True}

    def fill_saved(self, url, *, submit, checkpoint):
        checkpoint()
        self.calls.append(("fill_saved", submit))
        return {"state": "unknown", "origin": "https://nid.naver.com"}

    def disconnect(self):
        pass


def test_encrypted_save_and_reuse_do_not_reload_password_or_claim_login():
    assert pump_until(lambda: True)
    service = Service()
    dialog = BrowserLoginDialog(service)
    try:
        assert dialog.password_input.echoMode() == QLineEdit.EchoMode.Password
        assert not dialog.saved_submit_button.isEnabled()
        dialog.username_input.setText("fixture-id")
        dialog.password_input.setText("fixture-secret")
        dialog.save_button.click()
        assert dialog.password_input.text() == ""
        assert pump_until(lambda: dialog._worker is None)
        assert service.calls[0][0] == "save"
        assert service.calls[0][1] != threading.get_ident()
        assert dialog.username_input.text() == "fixture-id"
        assert "암호화 저장됨" in dialog.account_label.text()
        assert not dialog.saved_submit_button.isEnabled()
        dialog.inspect_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert dialog.saved_submit_button.isEnabled()
        dialog.saved_submit_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert ("fill_saved", True) in service.calls
        assert "확인하지 못했습니다" in dialog.status_label.text()
        dialog.open_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert "로그인은 아직 미확인" in dialog.status_label.text()
        assert "fixture-secret" not in "\n".join(label.text() for label in dialog.findChildren(QLabel))
        assert dialog.password_input.text() == ""
        dialog.delete_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert not service.configured and not dialog.saved_fill_button.isEnabled()
    finally:
        dialog.reject()
        dialog.deleteLater()
        assert pump_until(lambda: not _ACTIVE_OPERATIONS)


def test_cancel_close_waits_for_worker_and_suppresses_private_errors():
    assert pump_until(lambda: True)
    service = Service()
    dialog = BrowserLoginDialog(service)
    dialog.show()
    try:
        service.error = True
        dialog.inspect_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert "fixture-secret" not in dialog.status_label.text()
        service.error = False
        service.started.clear()
        service.release.clear()
        dialog.inspect_button.click()
        assert service.started.wait(2)
        worker = dialog._worker
        dialog.reject()
        assert worker.cancelled.is_set()
        assert dialog.isVisible()
        assert pump_until(lambda: dialog._worker is None and not dialog.isVisible())
        assert not dialog.saved_submit_button.isEnabled()
        dialog.show()
        assert dialog.inspect_button.isEnabled()
    finally:
        service.release.set()
        assert pump_until(lambda: not _ACTIVE_OPERATIONS)
        dialog.reject()
        dialog.deleteLater()


def test_missing_extension_has_actionable_safe_message():
    from core.browser_extension import BrowserExtensionUnavailableError

    class MissingExtension(Service):
        def inspect(self, url, *, checkpoint):
            raise BrowserExtensionUnavailableError("fixture-private-detail")

    assert pump_until(lambda: True)
    dialog = BrowserLoginDialog(MissingExtension())
    try:
        dialog.inspect_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert "Playwright 확장을 찾지 못했습니다" in dialog.status_label.text()
        assert "fixture-private-detail" not in dialog.status_label.text()
        assert not dialog.saved_submit_button.isEnabled()
    finally:
        dialog.reject()
        dialog.deleteLater()
        assert pump_until(lambda: not _ACTIVE_OPERATIONS)


def test_singleton_preserves_literal_url_and_main_window_surface(monkeypatch):
    assert pump_until(lambda: True)
    service = Service()
    registry = SimpleNamespace(get_plugin=lambda _name: SimpleNamespace(service=service))
    parent = QWidget()
    url = "https://example.com/CaseSensitive?Continue=XYZ"
    first = open_browser_login_dialog(registry, parent, url=url)
    try:
        assert open_browser_login_dialog(registry, parent, url=url) is first
        assert first.url_input.text() == url
        assert not first.isModal()
        import ui.browser_login_dialog as module
        from ui.main_window import JarvisMainWindow
        calls = []
        monkeypatch.setattr(module, "open_browser_login_dialog", lambda registry, parent, url: calls.append(url))
        window = SimpleNamespace(plugin_registry=registry)
        JarvisMainWindow.open_interface_surface(window, "browser_login:" + url)
        assert calls == [url]
    finally:
        first.reject()
        parent.deleteLater()


def test_site_change_clears_old_account_and_invalid_url_never_persists_credentials():
    from core.browser_login import BrowserLoginService, SITES

    class Vault:
        def __init__(self):
            self.records = {}
            self.saves = 0

        def load(self, provider, account):
            record = self.records.get((provider, account))
            return dict(record) if record else None

        def save(self, provider, account, record):
            self.saves += 1
            self.records[provider, account] = dict(record)

    assert pump_until(lambda: True)
    vault = Vault()
    service = BrowserLoginService(vault=vault)
    service.save_account(SITES["naver"]["url"], "naver-fixture", "fixture-secret")
    dialog = BrowserLoginDialog(service)
    try:
        assert dialog.username_input.text() == "naver-fixture"
        assert dialog.password_input.text() == ""
        dialog.site.setCurrentIndex(dialog.site.findData(SITES["google"]["url"]))
        assert dialog.username_input.text() == ""
        assert not dialog.delete_button.isEnabled()
        dialog.site.setCurrentIndex(dialog.site.count() - 1)
        dialog.url_input.setText("https://user:fixture-secret@example.com/")
        dialog.username_input.setText("invalid-fixture")
        dialog.password_input.setText("fixture-invalid-secret")
        dialog.save_button.click()
        assert dialog.password_input.text() == ""
        assert pump_until(lambda: dialog._worker is None)
        assert vault.saves == 1
        assert "fixture-secret" not in dialog.status_label.text()
        assert "fixture-invalid-secret" not in dialog.status_label.text()
    finally:
        dialog.reject()
        dialog.deleteLater()
        service.disconnect()
        assert pump_until(lambda: not _ACTIVE_OPERATIONS)


def test_redirect_requires_inspection_before_another_saved_input():
    class RedirectService(Service):
        def fill_saved(self, url, *, submit, checkpoint):
            checkpoint()
            return {"state": "login_required", "origin": "https://www.naver.com",
                    "username_field": True, "password_field": True, "submit": True}

    assert pump_until(lambda: True)
    service = RedirectService()
    service.configured = True
    dialog = BrowserLoginDialog(service)
    try:
        dialog.inspect_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert dialog.saved_submit_button.isEnabled()
        dialog.saved_submit_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert not dialog.fill_button.isEnabled()
        assert not dialog.saved_fill_button.isEnabled()
        assert not dialog.saved_submit_button.isEnabled()
        dialog.inspect_button.click()
        assert pump_until(lambda: dialog._worker is None)
        assert dialog.saved_submit_button.isEnabled()
    finally:
        dialog.reject()
        dialog.deleteLater()
        assert pump_until(lambda: not _ACTIVE_OPERATIONS)
