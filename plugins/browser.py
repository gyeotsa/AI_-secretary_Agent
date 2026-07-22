"""Optional Playwright browser tools. Installation remains explicit."""
from typing import Any, Dict, List
from urllib.parse import urlparse
import ipaddress
import socket

from core.plugin import BasePlugin, ToolSchema


class BrowserPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "browser"
        self.description = "Playwright 기반 웹 페이지 조회 및 스크린샷"

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("browser_get_text", "웹 페이지의 본문 텍스트를 가져옵니다", {
                "type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, ["browser"]),
            ToolSchema("browser_screenshot", "웹 페이지 스크린샷을 저장합니다", {
                "type": "object", "properties": {"url": {"type": "string"}, "path": {"type": "string"}},
                "required": ["url", "path"]}, ["browser", "filesystem_write"]),
        ]

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("http/https URL만 허용됩니다.")
        if parsed.username or parsed.password:
            raise ValueError("URL 자격 증명은 허용되지 않습니다.")
        if parsed.port and parsed.port not in {80, 443}:
            raise ValueError("80/443 포트만 허용됩니다.")
        hostname = (parsed.hostname or "").casefold()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise ValueError("로컬 주소는 허용되지 않습니다.")
        try:
            address = ipaddress.ip_address(hostname)
            if not address.is_global:
                raise ValueError("사설·루프백·예약 주소는 허용되지 않습니다.")
        except ValueError as exc:
            if "허용되지" in str(exc):
                raise
            try:
                resolved = {
                    item[4][0]
                    for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
                }
            except socket.gaierror as dns_error:
                raise ValueError(f"호스트를 확인할 수 없습니다: {hostname}") from dns_error
            if not resolved or any(not ipaddress.ip_address(ip).is_global for ip in resolved):
                raise ValueError("공개 인터넷 주소로 확인되지 않는 호스트는 허용되지 않습니다.")
        return url

    def _guard_route(self, route) -> None:
        """Redirect와 하위 리소스가 사설망으로 우회하지 못하도록 모든 요청 검사."""
        request_url = route.request.url
        scheme = urlparse(request_url).scheme
        if scheme in {"data", "blob"}:
            route.continue_()
            return
        try:
            self._validate_url(request_url)
            route.continue_()
        except ValueError:
            route.abort()

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        if tool_name not in {"browser_get_text", "browser_screenshot"}:
            return f"오류: 알 수 없는 툴 '{tool_name}'"
        screenshot_path = None
        if tool_name == "browser_screenshot":
            from core.harness import SafetyLayer
            screenshot_path = str(tool_input.get("path", ""))
            ok, message = SafetyLayer.validate_path(screenshot_path)
            if not ok:
                return f"오류: {message}"
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "오류: playwright가 설치되지 않았습니다. requirements.txt와 브라우저 설치 단계를 확인하세요."
        try:
            url = self._validate_url(str(tool_input["url"]))
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.route("**/*", self._guard_route)
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                self._validate_url(page.url)
                if response is None or not response.ok:
                    status = response.status if response else "응답 없음"
                    browser.close()
                    return f"오류: 페이지 응답 실패: {status}"
                if tool_name == "browser_get_text":
                    result = page.locator("body").inner_text()[:50000]
                else:
                    page.screenshot(path=screenshot_path, full_page=True)
                    result = f"스크린샷 저장 성공: {screenshot_path}"
                browser.close()
                return result
        except Exception as exc:
            return f"오류: 브라우저 실행 실패: {exc}"
