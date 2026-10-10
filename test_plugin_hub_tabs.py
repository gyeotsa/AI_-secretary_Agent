import os
import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from core.mcp_bridge import validate_manifest
from core.plugin import PluginRegistry
from ui.main_window import JarvisMainWindow
from ui.plugin_hub import PluginHub, _NoRedirect


def test_https_mcp_validation_and_plugin_tab_reuse():
    app = QApplication.instance() or QApplication([])
    assert validate_manifest({"name": "demo", "url": "https://example.com/mcp"})["url"].startswith("https://")
    with pytest.raises(ValueError):
        validate_manifest({"name": "unsafe", "transport": "stdio", "command": "python"})
    with pytest.raises(ValueError):
        validate_manifest({"name": "unsafe", "url": "http://example.com/mcp"})

    window = JarvisMainWindow()
    window.set_plugin_registry(PluginRegistry())
    hub = window.show_plugin_diagnostics()
    app.processEvents()
    assert window.workspace_tabs.count() == 2
    assert window.workspace_tabs.currentWidget() is hub
    assert window.show_plugin_diagnostics() is hub
    assert window.workspace_tabs.count() == 2
    window.workspace_tabs.tabCloseRequested.emit(1)
    assert window.workspace_tabs.count() == 1
    window.close()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    import ui.plugin_hub as module
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(module, "__file__", str(tmp_path / "ui" / "plugin_hub.py"))
    instance = PluginHub(PluginRegistry())
    yield instance
    instance.close()


def test_standalone_mail_login_settings_opens_existing_login_flow(hub, monkeypatch):
    import ui.browser_login_dialog as login_module
    import core.browser_mail as mail_module
    opener = Mock()
    monkeypatch.setattr(login_module, "open_browser_login_dialog", opener)
    monkeypatch.setattr(mail_module, "BrowserMailService", lambda: Mock())
    dialog = hub.open_mail_inbox()
    try:
        dialog.login_button.click()
        opener.assert_called_once_with(hub.registry, dialog, url="https://accounts.google.com/")
    finally:
        dialog.reject()
        if dialog._worker is not None:
            assert dialog._worker.wait(3000)
        QApplication.instance().processEvents()


@pytest.mark.parametrize("url", ["file:///C:/secret.py", "http://example.org/plugin.py",
                                  "https://user:password@example.org/plugin.py",
                                  "https://example.org/plugin.py?api_key=secret"])
def test_download_rejects_unapproved_addresses_before_network(hub, monkeypatch, url):
    import ui.plugin_hub as module
    opener = Mock()
    monkeypatch.setattr(module.urllib.request, "build_opener", opener)
    with pytest.raises(ValueError):
        hub._download(url)
    opener.assert_not_called()


def test_downloaded_manifest_is_staged_not_connected(hub, monkeypatch, tmp_path):
    import ui.plugin_hub as module
    from plugins.browser import BrowserPlugin
    monkeypatch.setattr(BrowserPlugin, "_validate_url", staticmethod(lambda url: url))
    response = io.BytesIO(b'{"name":"unexpected", "url":"https://other.example/mcp"}')
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda handler: opener)
    connect = Mock(side_effect=AssertionError("untrusted manifest auto-connected"))
    monkeypatch.setattr(module, "connect_mcp", connect)
    message = hub._download("https://example.org/config.json")
    assert "연결은 하지 않았습니다" in message
    assert (tmp_path / "data" / "plugin_inbox" / "config.json").is_file()
    connect.assert_not_called()
    assert opener.open.call_args.kwargs["timeout"] == 20
    with pytest.raises(ValueError, match="리디렉션"):
        _NoRedirect().redirect_request(None, None, None, None, None, None)


def test_review_inbox_never_overwrites_or_accepts_oversized_bytes(hub, tmp_path):
    hub._stage_python("example.py", b"original")
    target = tmp_path / "data" / "plugin_inbox" / "example.py"
    with pytest.raises(FileExistsError):
        hub._stage_python("example.py", b"replacement")
    assert target.read_bytes() == b"original"
    with pytest.raises(ValueError):
        hub._stage_python("large.py", b"x" * 2_000_001)
    assert not target.with_name("large.py").exists()


@pytest.mark.parametrize("connected,failed", [(2, 0), (1, 1), (0, 2)])
def test_saved_reconnect_ui_distinguishes_success_partial_and_failure(hub, monkeypatch, connected, failed):
    import ui.plugin_hub as module
    monkeypatch.setattr(module, "saved_mcp_count", lambda: 2)
    reconnect = Mock(return_value={"connected": connected, "failed": failed})
    monkeypatch.setattr(module, "load_saved_mcp_plugins", reconnect)
    monkeypatch.setattr(module.QMessageBox, "question", lambda *_args: module.QMessageBox.StandardButton.Yes)
    information, warning = Mock(), Mock()
    monkeypatch.setattr(module.QMessageBox, "information", information)
    monkeypatch.setattr(module.QMessageBox, "warning", warning)
    monkeypatch.setattr(hub, "_run", lambda operation: hub._done(operation()))
    hub.reconnect_saved()
    reconnect.assert_called_once_with(hub.registry)
    shown, absent = (warning, information) if failed else (information, warning)
    shown.assert_called_once()
    absent.assert_not_called()
    assert f"성공 {connected}개" in shown.call_args.args[2]
    assert f"실패 {failed}개" in shown.call_args.args[2]
    if failed:
        assert "실패" in shown.call_args.args[1]


def test_saved_reconnect_does_not_run_without_approval(hub, monkeypatch):
    import ui.plugin_hub as module
    monkeypatch.setattr(module, "saved_mcp_count", lambda: 2)
    monkeypatch.setattr(module.QMessageBox, "question", lambda *_args: module.QMessageBox.StandardButton.No)
    operation = Mock()
    monkeypatch.setattr(hub, "_run", operation)
    hub.reconnect_saved()
    operation.assert_not_called()


def test_busy_connection_keeps_tab_and_parent_alive_until_result(monkeypatch):
    import ui.plugin_hub as module
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(module.QMessageBox, "information", lambda *_a: None)
    window = JarvisMainWindow()
    window.set_plugin_registry(PluginRegistry())
    hub = window.show_plugin_diagnostics()
    entered, release = threading.Event(), threading.Event()
    def operation():
        entered.set()
        assert release.wait(3)
        return "finished"
    closed = []
    window.close_requested.connect(lambda: closed.append(True))
    try:
        hub._run(operation)
        assert entered.wait(2)
        hub.reject()
        window.workspace_tabs.tabCloseRequested.emit(1)
        window._request_close()
        assert window.workspace_tabs.count() == 2 and not closed
        assert not hub.can_close_workspace_tab()
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while not hub.can_close_workspace_tab() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
    assert hub.can_close_workspace_tab()
    window.workspace_tabs.tabCloseRequested.emit(1)
    assert window.workspace_tabs.count() == 1
    window._request_close()
    assert closed == [True]
    window.close()


@pytest.mark.parametrize("cancelled", [False, True])
def test_calendar_tab_selection_wait_can_cancel_without_releasing_busy_tab(monkeypatch, cancelled):
    import ui.plugin_hub as module
    app = QApplication.instance() or QApplication([])
    question = Mock(return_value=module.QMessageBox.StandardButton.Yes)
    information, warning = Mock(), Mock()
    monkeypatch.setattr(module.QMessageBox, "question", question)
    monkeypatch.setattr(module.QMessageBox, "information", information)
    monkeypatch.setattr(module.QMessageBox, "warning", warning)
    entered, release, disconnected = threading.Event(), threading.Event(), threading.Event()
    def connect():
        entered.set()
        assert release.wait(3)
        if disconnected.is_set():
            raise RuntimeError("QA 연결 취소")
        return {"account": "QA 계정"}
    service = SimpleNamespace(connect=Mock(side_effect=connect), observe=Mock(),
                              disconnect=Mock(side_effect=disconnected.set))
    registry = PluginRegistry()
    monkeypatch.setattr(registry, "get_plugin", lambda name: (
        SimpleNamespace(service=service) if name == "naver_calendar" else None))
    window = JarvisMainWindow()
    window.set_plugin_registry(registry)
    hub = window.show_plugin_diagnostics()
    closed = []
    window.close_requested.connect(lambda: closed.append(True))
    try:
        hub.calendar_button.click()
        assert entered.wait(2)
        service.connect.assert_called_once_with()
        assert "5분 안에 Allow & select" in question.call_args.args[2]
        assert "시간 초과 후 남은 요청은 무효" in question.call_args.args[2]
        assert "최대 5분" in hub.busy.text()
        assert hub._running and hub._calendar_connecting
        if cancelled:
            hub.calendar_disconnect_button.click()
            assert disconnected.wait(2)
            service.disconnect.assert_called_once_with()
            assert "연결 해제 중" in hub.busy.text()
        # Even an already-requested cancellation must await the worker result.
        hub.reject()
        window.workspace_tabs.tabCloseRequested.emit(1)
        window._request_close()
        assert window.workspace_tabs.count() == 2 and not closed
        assert not hub.can_close_workspace_tab()
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while not hub.can_close_workspace_tab() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
    assert not hub._running and not hub._calendar_connecting and not hub.busy.text()
    # Closing the parent while busy emits its own information dialog, not a
    # calendar success. Keep that guard checked separately from the hub result.
    parent_notices = [call for call in information.call_args_list if call.args[0] is window]
    calendar_notices = [call for call in information.call_args_list if call.args[0] is hub]
    assert len(parent_notices) == 1
    assert "연결 작업 진행 중" == parent_notices[0].args[1]
    if cancelled:
        service.observe.assert_not_called()
        warning.assert_called_once()
        assert not calendar_notices
        assert "QA 연결 취소" in warning.call_args.args[2]
    else:
        service.observe.assert_called_once_with()
        service.disconnect.assert_not_called()
        assert len(calendar_notices) == 1
        warning.assert_not_called()
        assert "별도 승인" in calendar_notices[0].args[2]
    window.workspace_tabs.tabCloseRequested.emit(1)
    assert window.workspace_tabs.count() == 1
    window._request_close()
    assert closed == [True]
    window.close()


def test_calendar_disconnect_cannot_cancel_another_mcp_operation(hub, monkeypatch):
    import ui.plugin_hub as module
    app = QApplication.instance() or QApplication([])
    service = SimpleNamespace(connect=Mock(), observe=Mock(), disconnect=Mock())
    monkeypatch.setattr(hub.registry, "get_plugin", lambda name: SimpleNamespace(service=service))
    question = Mock(return_value=module.QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(module.QMessageBox, "question", question)
    monkeypatch.setattr(module.QMessageBox, "information", lambda *_args: None)
    entered, release = threading.Event(), threading.Event()
    def another_mcp():
        entered.set()
        assert release.wait(3)
        return "다른 MCP 연결 완료"
    try:
        hub._run(another_mcp)
        assert entered.wait(2)
        busy = hub.busy.text()
        hub.calendar_disconnect_button.click()
        hub.calendar_button.click()
        service.disconnect.assert_not_called()
        service.connect.assert_not_called()
        service.observe.assert_not_called()
        question.assert_not_called()
        assert hub._running and not hub._calendar_connecting
        assert hub.busy.text() == busy and not hub.can_close_workspace_tab()
    finally:
        release.set()
        deadline = time.monotonic() + 3
        while not hub.can_close_workspace_tab() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
    assert hub.can_close_workspace_tab() and not hub._calendar_connecting
