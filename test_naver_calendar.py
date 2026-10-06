import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.browser_extension import decode_result
from core.naver_calendar import CalendarScreenError, NaverCalendarService
from core.plugin import PluginRegistry
from core.plugin import ToolCancelledError
from core.verifier import ToolVerifier
from plugins.naver_calendar import NaverCalendarPlugin


def response(text, error=False):
    return SimpleNamespace(is_error=error, content=[SimpleNamespace(type="text", text=text)])


def connected():
    return {"binding":"nonce", "account":"QA", "origin":"https://calendar.naver.com"}


def visible_view():
    return {"text":"2026.10", "calendars":"내 캘린더", "account":"QA",
            "scope":"visible_view", "complete_account":False, "timezone":None,
            "timezone_source":"not_observed",
            "url":"https://calendar.naver.com/main#view"}


def test_mcp_result_must_be_result_not_echoed_code():
    assert decode_result(response('### Result\n{"ok":true}\n### Ran Playwright code\nignored')) == {"ok":True}
    with pytest.raises(ValueError):
        decode_result(response('### Ran Playwright code\n{"ok":true}'))
    with pytest.raises(RuntimeError):
        decode_result(response('### Result\n{"ok":true}', True))
    with pytest.raises(ValueError):
        decode_result(response('### Result\n["ok"]'))


def test_missing_connection_performs_no_browser_calls():
    session = Mock()
    service = NaverCalendarService(session)
    with pytest.raises(ValueError, match="먼저"):
        service.observe()
    session.call.assert_not_called()


def test_fixed_code_binds_origin_tab_and_account_without_cookies():
    session = Mock()
    session.call.side_effect = [connected(), visible_view()]
    service = NaverCalendarService(session)
    assert service.connect()["connected"]
    assert service.observe()["text"] == "2026.10"
    connect_code, read_code = [call.args[0] for call in session.call.call_args_list]
    assert "u.origin !== 'https://calendar.naver.com'" in connect_code
    assert "u.pathname !== '/main'" in connect_code
    assert "page[key] !== binding" in read_code
    assert 'const expectedAccount = "QA"' in read_code
    assert 'const binding = "nonce"' in read_code
    assert "complete_account:false" in read_code
    assert "page.locator('#calendar_list_container')" in read_code
    assert "list.isVisible()" in read_code and "timeout:5000" in read_code
    assert "timezone:null, timezone_source:'not_observed'" in read_code
    assert session.call.call_args_list[0].kwargs["timeout"] == 300
    assert session.call.call_args_list[1].kwargs["timeout"] == 90
    for forbidden in ("storageState", "cookies(", "page.goto", "page.context", "request.", "getByRole('button', {name:'저장'"):
        assert forbidden not in connect_code + read_code


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError(), ValueError()])
def test_failed_observation_revokes_old_connection_evidence(failure):
    session = Mock()
    session.call.side_effect = [connected(), failure]
    service = NaverCalendarService(session)
    service.connect()
    with pytest.raises(type(failure)):
        service.observe()
    assert service.status()["connected"] is False
    assert service.status()["account"] == ""


@pytest.mark.parametrize("invalid", [{"text":""}, {"account":"different"}, {"complete_account":True},
                                      {"url":"https://calendar.naver.com.evil/main"}, {"scope":"all"},
                                      {"timezone":"Asia/Seoul"}, {"timezone_source":"verified"},
                                      {"url":"https://calendar.naver.com/main-other"}])
def test_invalid_observation_revokes_connection(invalid):
    session = Mock()
    session.call.side_effect = [connected(), {**visible_view(), **invalid}]
    service = NaverCalendarService(session)
    service.connect()
    with pytest.raises(ValueError):
        service.observe()
    assert not service.status()["connected"]


def test_generic_profile_alt_is_not_account_identity():
    session = Mock()
    session.call.return_value = {**connected(), "account":"내 프로필 이미지"}
    service = NaverCalendarService(session)
    with pytest.raises(ValueError):
        service.connect()
    assert not service.status()["connected"]


def test_disconnect_cannot_be_undone_by_late_connect_result():
    session = Mock()
    entered, release = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return connected()
    session.call.side_effect = delayed
    service = NaverCalendarService(session)
    errors = []
    def run():
        try:
            service.connect()
        except ValueError as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2)
        service.disconnect()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and errors
    assert not service.status()["connected"]


def test_queued_service_operation_checks_cancellation_without_browser_call():
    session = Mock()
    service = NaverCalendarService(session)
    service._lock.acquire()
    def cancelled():
        raise ToolCancelledError("cancelled")
    try:
        with pytest.raises(ToolCancelledError):
            service.connect(checkpoint=cancelled)
    finally:
        service._lock.release()
    session.call.assert_not_called()


def test_plugin_read_result_marks_visible_scope_not_remote_sync():
    registry = PluginRegistry()
    plugin = NaverCalendarPlugin()
    plugin.service = Mock()
    plugin.service.status.return_value = {"connected":True, "account_fingerprint":"qa"}
    plugin.service.observe.return_value = visible_view()
    registry.register_plugin(plugin)
    result = registry.execute_tool("naver_calendar_read_view", {})
    assert result.succeeded
    assert json.loads(result.raw_output)["complete_account"] is False
    assert result.evidence[0].data["scope"] == "visible_view"
    tool = plugin.get_tools()[0]
    assert tool.side_effect == "read" and tool.max_retries == 0
    assert tool.cancellable
    assert ToolVerifier().verify(tool.name, {}, result).success
    result.raw_output = json.dumps({**visible_view(), "text":"forged"}, ensure_ascii=False)
    assert not ToolVerifier().verify(tool.name, {}, result).success
    plugin.on_unload()
    plugin.service.disconnect.assert_called_once()
    registry.shutdown()


def test_editor_inspection_never_clicks_or_saves_anything():
    session = Mock()
    session.call.side_effect = [connected(), {"controls":[], "text":"2026.10"}]
    service = NaverCalendarService(session)
    service.connect()
    service.inspect_editor()
    code = session.call.call_args.args[0]
    assert ".click(" not in code and ".fill(" not in code and ".goto(" not in code


@pytest.mark.parametrize("screen", ["login", "calendar_other", "other", "calendar", "unknown", "https://secret?token=abc"])
def test_screen_failure_has_safe_actionable_diagnostic_and_revokes_binding(screen):
    session = Mock()
    session.call.side_effect = [connected(), {"error_code":"origin", "screen":screen}]
    service = NaverCalendarService(session)
    service.connect()
    with pytest.raises(CalendarScreenError) as caught:
        service.observe()
    assert caught.value.stage == "origin"
    assert caught.value.screen == (screen if screen in {"login", "calendar_other", "other", "calendar"} else "unknown")
    assert "secret" not in str(caught.value) and "token" not in str(caught.value)
    assert not service.status()["connected"]
    assert "page.goto" not in session.call.call_args.args[0]
