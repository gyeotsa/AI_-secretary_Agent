"""Optional Playwright browser tools. Installation remains explicit."""
import json
import re
from datetime import datetime
from typing import Any, Dict, List
from urllib.parse import urlparse
import ipaddress
import socket

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None


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
            ToolSchema("browser_web_search", "웹에서 최신 정보를 검색하고 출처와 함께 반환합니다", {
                "type": "object", "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                }, "required": ["query"]}, ["browser"]),
        ]

    def get_intents(self) -> List[IntentSchema]:
        return [
            IntentSchema(
                "web.search",
                "최신·외부 정보를 실제 웹에서 검색",
                "browser_web_search",
                ["검색", "찾아봐", "알아봐", "확인해", "최신", "신형", "새로 나온", "출시"],
                [SlotSchema("query", "검색할 전체 질문",
                            "웹에서 무엇을 검색할지 알려주세요, 보스.")],
                execution_hints=[
                    "검색", "찾아", "알아봐", "확인", "알려", "무엇", "뭐",
                ],
                follow_up_hints=[
                    "그럼", "그러면", "관련해서", "더 찾아", "다른", "맞아", "응", "어떤",
                ],
                utterance_patterns=[
                    r"(?:20)?\d{2}년.*(?:최신|신형|새로|출시)",
                    r"(?:최신|신형|최근|새로 나온).*(?:이름|무엇|뭐|알려|찾아)",
                    r"(?:검색|찾아봐|알아봐|확인해)",
                ],
            )
        ]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name != "web.search":
            return slots
        query = text.strip()
        if query:
            query = re.sub(
                r"(?<!\d)(\d{2})년",
                lambda match: f"{2000 + int(match.group(1))}년",
                query,
            )
            # 후속 검색도 이전 문맥을 버리지 않고 새 질문과 결합한다.
            previous = str(slots.get("query", "")).strip()
            if previous and query != previous:
                query = f"{previous}\n후속 질문: {query}"
            slots["query"] = query
        return slots

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
        if tool_name == "browser_web_search":
            return self._search_web(tool_input)
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

    @staticmethod
    def _search_web(tool_input: Dict[str, Any]) -> str:
        if DDGS is None:
            return "오류: duckduckgo-search가 설치되지 않았습니다."
        query = str(tool_input.get("query", "")).strip()
        if not query:
            return "오류: 검색어가 비어 있습니다."
        limit = max(1, min(int(tool_input.get("max_results", 5)), 10))
        try:
            with DDGS() as ddgs:
                rows = list(ddgs.text(query, max_results=limit))
                rows.extend(ddgs.text(f"{query} official 공식", max_results=limit))
            candidates = [
                {
                    "title": str(row.get("title", "")).strip(),
                    "url": str(row.get("href", "")).strip(),
                    "snippet": str(row.get("body", "")).strip(),
                }
                for row in rows if row.get("href")
            ]
            deduplicated = {item["url"]: item for item in candidates}
            blocked_hosts = ("tiktok.com", "pinterest.", "facebook.com", "instagram.com")

            def source_score(item):
                host = (urlparse(item["url"]).hostname or "").casefold()
                title = item["title"].casefold()
                score = 0
                if any(term in title for term in ("official", "공식", "newsroom", "media center")):
                    score += 20
                if any(term in host for term in ("newsroom", "media", "press")):
                    score += 10
                if any(term in host for term in blocked_hosts):
                    score -= 100
                if "forum" in host or "reddit.com" in host:
                    score -= 20
                return score

            results = sorted(
                deduplicated.values(), key=source_score, reverse=True
            )[:limit]
            if not results:
                return "오류: 웹 검색 결과를 찾지 못했습니다."
            return json.dumps({
                "query": query,
                "searched_at": datetime.now().astimezone().isoformat(),
                "results": results,
            }, ensure_ascii=False)
        except Exception as exc:
            return f"오류: 웹 검색 실패: {exc}"

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name != "browser_web_search":
            return result
        try:
            payload = json.loads(result)
            results = payload["results"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return result
        from core.llm import get_llm_client

        sources = "\n".join(
            f"[{index}] {item['title']}\nURL: {item['url']}\n요약: {item['snippet']}"
            for index, item in enumerate(results, 1)
        )
        prompt = (
            f"현재 시각: {payload.get('searched_at', '')}\n"
            f"사용자 검색 질문: {payload.get('query', '')}\n\n"
            f"검색 결과:\n{sources}\n\n"
            "검색 결과에 명시된 사실만 사용해 한국어로 답하세요. 결과가 질문의 연도나 대상을 "
            "확실히 뒷받침하지 않으면 확인할 수 없다고 말하세요. 제품명·연도는 추측하지 마세요. "
            "사용자가 단순히 이름이나 정답을 물으면 핵심 답만 한 문장으로 말하세요. 상세 설명·"
            "비교·이유를 요청한 경우에도 최대 세 문장으로 답하세요. URL이나 출처 목록은 출력하지 "
            "마세요."
        )
        answer = get_llm_client("reasoning").chat([
            {"role": "system", "content": "당신은 검색 근거만 사용하는 사실 검증 담당자입니다."},
            {"role": "user", "content": prompt},
        ]).strip()
        if not answer or answer.casefold().startswith(("오류:", "error:")):
            return "웹 검색은 완료했지만 결과를 요약하지 못했습니다."
        answer = re.sub(r"\s*(?:출처|Sources?)\s*:.*$", "", answer, flags=re.I | re.S).strip()
        source_urls = [item["url"] for item in results[:2]]
        return answer + "\n출처: " + " | ".join(source_urls)
