"""Optional Playwright browser tools. Installation remains explicit."""
from typing import Any, Dict, List
from urllib.parse import urlparse
import ipaddress

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
        return url

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "오류: playwright가 설치되지 않았습니다. requirements.txt와 브라우저 설치 단계를 확인하세요."
        try:
            url = self._validate_url(str(tool_input["url"]))
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if tool_name == "browser_get_text":
                    result = page.locator("body").inner_text()[:50000]
                elif tool_name == "browser_screenshot":
                    from core.harness import SafetyLayer
                    path = str(tool_input["path"])
                    ok, message = SafetyLayer.validate_path(path)
                    if not ok:
                        browser.close()
                        return f"오류: {message}"
                    page.screenshot(path=path, full_page=True)
                    result = f"스크린샷 저장 성공: {path}"
                else:
                    result = f"오류: 알 수 없는 툴 '{tool_name}'"
                browser.close()
                return result
        except Exception as exc:
            return f"오류: 브라우저 실행 실패: {exc}"
