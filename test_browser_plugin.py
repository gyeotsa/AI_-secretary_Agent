"""Browser plugin security and Playwright integration tests."""
import socket
from pathlib import Path

import pytest

from plugins.browser import BrowserPlugin
from core.tools import ToolExecutor
from core.tool_result import ToolRunResult
import core.permission as permission_module


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "http://localhost/",
    "http://127.0.0.1/",
    "http://10.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://user:password@example.com/",
    "https://example.com:8443/",
])
def test_unsafe_urls_are_blocked(url):
    with pytest.raises(ValueError):
        BrowserPlugin._validate_url(url)


def test_dns_name_resolving_to_private_ip_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 443))
    ])
    with pytest.raises(ValueError):
        BrowserPlugin._validate_url("https://internal.example/")


def test_public_dns_name_is_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ])
    assert BrowserPlugin._validate_url("https://example.com/") == "https://example.com/"


def test_unknown_tool_is_rejected_before_browser_launch():
    result = BrowserPlugin().execute_tool("browser_unknown", {})
    assert isinstance(result, ToolRunResult)
    assert result.raw_output.startswith("오류: 알 수 없는 툴")


def test_invalid_screenshot_path_is_rejected_before_browser_launch():
    result = BrowserPlugin().execute_tool(
        "browser_screenshot", {"url": "https://example.com/", "path": "C:/Windows/test.png"}
    )
    assert isinstance(result, ToolRunResult)
    assert result.raw_output.startswith("오류:")
    assert "허용되지 않습니다" in result.raw_output


def test_tool_executor_checks_browser_and_file_write_permissions(monkeypatch):
    class RecordingPermissionManager:
        def __init__(self):
            self.requested = []

        def request_permission(self, permission_id):
            self.requested.append(permission_id)
            return permission_id != "filesystem_write"

    manager = RecordingPermissionManager()
    monkeypatch.setattr(permission_module, "get_permission_manager", lambda: manager)
    result = ToolExecutor().execute_tool(
        "browser_screenshot", {"url": "https://example.com/", "path": str(Path.cwd() / "denied.png")}
    )
    assert isinstance(result, ToolRunResult)
    assert not result.succeeded
    assert result.raw_output == "오류: 권한이 거부되었습니다: filesystem_write"
    assert manager.requested == ["browser", "filesystem_write"]


@pytest.mark.integration
def test_playwright_get_text_from_public_page():
    result = BrowserPlugin().execute_tool("browser_get_text", {"url": "https://example.com/"})
    assert isinstance(result, ToolRunResult)
    assert result.succeeded
    assert result.evidence[0].kind == "http_page"
    assert "Example Domain" in result.raw_output


@pytest.mark.integration
def test_playwright_screenshot_public_page():
    target = Path.cwd() / ".browser-test-output.png"
    try:
        result = BrowserPlugin().execute_tool(
            "browser_screenshot", {"url": "https://example.com/", "path": str(target)}
        )
        assert isinstance(result, ToolRunResult)
        assert result.succeeded
        assert result.raw_output.startswith("스크린샷 저장 성공:")
        assert target.exists() and target.stat().st_size > 0
    finally:
        target.unlink(missing_ok=True)
