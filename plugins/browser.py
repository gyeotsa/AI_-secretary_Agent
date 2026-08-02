"""Optional Playwright browser tools. Installation remains explicit."""
import json
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse
import ipaddress
import socket
import time

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult
from core.research import PlaywrightPageFetcher, PromptInjectionGuard, ResearchAgent, ResearchCache

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
        self.dependencies = ["playwright"]
        self._research_agent = None

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("browser_get_text", "웹 페이지의 본문 텍스트를 가져옵니다", {
                "type": "object", "properties": {"url": {"type": "string"},
                    "profile": {"type": "string", "default": "default"},
                    "dynamic": {"type": "boolean", "default": True}},
                "required": ["url"], "additionalProperties": False}, ["browser"]),
            ToolSchema("browser_screenshot", "웹 페이지 스크린샷을 저장합니다", {
                "type": "object", "properties": {"url": {"type": "string"}, "path": {"type": "string"},
                    "profile": {"type": "string", "default": "default"}},
                "required": ["url", "path"], "additionalProperties": False}, ["browser", "filesystem_write"]),
            ToolSchema("browser_web_search", "웹에서 최신 정보를 검색하고 출처와 함께 반환합니다", {
                "type": "object", "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                }, "required": ["query"]}, ["browser"]),
            ToolSchema("browser_research", "검색 결과 페이지를 실제 방문해 교차검증 보고서를 만듭니다", {
                "type": "object", "properties": {
                    "query": {"type": "string"}, "max_sources": {"type": "integer", "minimum": 2, "maximum": 8, "default": 5},
                    "profile": {"type": "string", "default": "default"},
                    "ttl_seconds": {"type": "number", "minimum": 60, "maximum": 86400, "default": 3600},
                    "force_refresh": {"type": "boolean", "default": False},
                }, "required": ["query"], "additionalProperties": False,
            }, ["browser"], timeout_seconds=180, max_retries=1, cancellable=True),
            ToolSchema("browser_research_and_apply_preference", "웹 조사 근거를 요약해 사용자 설정과 RAG에 검증 저장합니다", {
                "type": "object", "properties": {
                    "query": {"type": "string"},
                    "setting": {"type": "string", "enum": ["response_style"]},
                    "max_sources": {"type": "integer", "minimum": 2, "maximum": 8, "default": 5},
                }, "required": ["query", "setting"], "additionalProperties": False,
            }, ["browser"], side_effect="change", timeout_seconds=240, max_retries=1, cancellable=True),
            ToolSchema("browser_learning_status", "웹 조사 학습이 설정과 RAG에 반영됐는지 확인합니다", {
                "type": "object", "properties": {
                    "setting": {"type": "string", "enum": ["response_style"]},
                }, "required": ["setting"], "additionalProperties": False,
            }, [], side_effect="read"),
            ToolSchema("browser_download", "로그인 세션을 유지한 브라우저에서 파일을 안전한 경로로 다운로드합니다", {
                "type": "object", "properties": {
                    "url": {"type": "string"}, "path": {"type": "string"},
                    "profile": {"type": "string", "default": "default"},
                }, "required": ["url", "path"], "additionalProperties": False,
            }, ["browser", "filesystem_write"], timeout_seconds=120, cancellable=True),
            ToolSchema("browser_profile_status", "브라우저 로그인 세션 Profile의 로컬 저장 상태를 확인합니다", {
                "type": "object", "properties": {"profile": {"type": "string", "default": "default"}},
                "additionalProperties": False,
            }, ["browser"], side_effect="read"),
        ]

    def get_intents(self) -> List[IntentSchema]:
        return [
            IntentSchema(
                "web.learning_status",
                "이전 웹 조사 학습의 설정 및 RAG 반영 상태 확인",
                "browser_learning_status",
                ["조사를 통해 학습했어", "웹 학습 상태", "조사 결과 반영됐어", "말투를 학습했어"],
                [SlotSchema("setting", "확인할 설정", "어떤 학습 설정을 확인할지 알려주세요.")],
                execution_hints=["학습했어", "반영됐어", "저장됐어", "기억했어"],
                utterance_patterns=[r"(?:조사|검색|웹).{0,30}(?:학습|반영|저장|기억).{0,8}(?:했|됐|되었)"],
                request_type="query",
            ),
            IntentSchema(
                "web.research_and_apply_preference",
                "외부 자료를 조사하고 검증된 요약을 설정과 RAG에 반영",
                "browser_research_and_apply_preference",
                ["웹 검색을 통해", "웹에서 조사", "검색해서 학습", "조사해서 반영", "조사하고 분석해서"],
                [SlotSchema("query", "조사할 대상", "웹에서 조사할 대상을 알려주세요."),
                 SlotSchema("setting", "반영할 설정", "조사 결과를 어디에 반영할지 알려주세요.")],
                execution_hints=["학습", "반영", "기억", "적용"],
                utterance_patterns=[r"(?:웹|검색|조사).{0,80}(?:학습|반영|기억|적용)"],
                request_type="change", freshness="live", requires_sources=True,
            ),
            IntentSchema(
                "web.search",
                "최신·외부 정보를 실제 웹에서 검색",
                "browser_research",
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
                freshness="live",
                requires_sources=True,
            )
        ]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name == "web.learning_status":
            if re.search(r"(?:말투|응답\s*스타일|대답\s*방식)", text):
                slots["setting"] = "response_style"
            return slots
        if intent_name == "web.research_and_apply_preference":
            normalized = text.strip()
            if re.search(r"(?:말투|응답\s*스타일|대답\s*방식)", normalized):
                slots["setting"] = "response_style"
            game = re.search(r"(.{1,50}?)라는\s*게임에서", normalized)
            character = re.search(
                r"(?:나오는|등장하는)\s*[\"'“”‘’]?([^\"'“”‘’]{1,20}?)[\"'“”‘’]?\s*라는\s*캐릭터",
                normalized,
            )
            if game and character:
                game_name = re.sub(r"^.*?(?:통해|에서)\s*", "", game.group(1)).strip(" ,:：")
                character_name = character.group(1).strip()
                slots["query"] = f"{game_name} {character_name} 캐릭터 성격 대사 말투 공식 자료"
            else:
                query = re.sub(r"^(?:웹\s*(?:검색)?을?\s*통해|웹에서|검색해서)\s*", "", normalized)
                query = re.sub(r"(?:꼼꼼하게\s*)?(?:조사|분석).*$", "", query).strip(" ,.!?") or normalized
                slots["query"] = query
            return slots
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

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if tool_name == "browser_research":
            return self._research_web(tool_input)
        if tool_name == "browser_research_and_apply_preference":
            return self._research_and_apply_preference(tool_input)
        if tool_name == "browser_learning_status":
            return self._learning_status(tool_input)
        if tool_name == "browser_download":
            return self._download(tool_input)
        if tool_name == "browser_profile_status":
            return self._profile_status(tool_input)
        if tool_name == "browser_web_search":
            return self._search_web(tool_input)
        if tool_name not in {"browser_get_text", "browser_screenshot"}:
            return ToolRunResult.failed(
                tool_name=tool_name, error=f"알 수 없는 툴 '{tool_name}'"
            )
        screenshot_path = None
        if tool_name == "browser_screenshot":
            from core.harness import SafetyLayer
            screenshot_path = str(tool_input.get("path", ""))
            ok, message = SafetyLayer.validate_path(screenshot_path)
            if not ok:
                return ToolRunResult.failed(tool_name=tool_name, error=message)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return ToolRunResult.failed(
                tool_name=tool_name,
                error="playwright가 설치되지 않았습니다. requirements.txt와 브라우저 설치 단계를 확인하세요.",
            )
        try:
            url = self._validate_url(str(tool_input["url"]))
            with sync_playwright() as playwright:
                fetcher = self._get_research_agent().fetcher
                profile = fetcher._profile_name(str(tool_input.get("profile", "default")))
                context = playwright.chromium.launch_persistent_context(
                    str(fetcher.profiles_dir / profile), headless=True, accept_downloads=True)
                page = context.pages[0] if context.pages else context.new_page()
                page.route("**/*", self._guard_route)
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if tool_name == "browser_get_text" and tool_input.get("dynamic", True):
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                self._validate_url(page.url)
                if response is None or not response.ok:
                    status = response.status if response else "응답 없음"
                    context.close()
                    return ToolRunResult.failed(
                        tool_name=tool_name, error=f"페이지 응답 실패: {status}"
                    )
                retrieved_at = datetime.now().astimezone().isoformat()
                final_url = page.url
                status_code = response.status
                if tool_name == "browser_get_text":
                    raw_text = page.locator("body").inner_text()[:50000]
                    result, injection_warnings = PromptInjectionGuard.isolate(raw_text)
                    tool_result = ToolRunResult.successful(
                        tool_name=tool_name,
                        raw_output=result,
                        evidence=[Evidence(
                            "http_page",
                            "공개 웹 페이지의 HTTP 응답과 본문을 확인했습니다.",
                            {
                                "requested_url": url,
                                "final_url": final_url,
                                "status_code": status_code,
                                "retrieved_at": retrieved_at,
                                "characters": len(result),
                                "sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
                                "prompt_injection_warnings": injection_warnings,
                            },
                        )],
                        artifacts=[Artifact("url", final_url)],
                    )
                else:
                    page.screenshot(path=screenshot_path, full_page=True)
                    screenshot = Path(screenshot_path).resolve()
                    if not screenshot.is_file() or screenshot.stat().st_size <= 0:
                        raise ValueError("스크린샷 파일이 생성되지 않았습니다.")
                    tool_result = ToolRunResult.successful(
                        tool_name=tool_name,
                        raw_output=f"스크린샷 저장 성공: {screenshot}",
                        evidence=[Evidence(
                            "browser_screenshot",
                            "페이지 응답과 저장된 스크린샷 파일을 확인했습니다.",
                            {
                                "requested_url": url,
                                "final_url": final_url,
                                "status_code": status_code,
                                "retrieved_at": retrieved_at,
                                "size": screenshot.stat().st_size,
                            },
                        )],
                        artifacts=[
                            Artifact("image", str(screenshot), {"format": "png"}),
                            Artifact("url", final_url),
                        ],
                    )
                context.close()
                return tool_result
        except Exception as exc:
            return ToolRunResult.failed(
                tool_name=tool_name, error=f"브라우저 실행 실패: {exc}"
            )

    @staticmethod
    def _search_candidates(query: str, limit: int):
        if DDGS is None: raise RuntimeError("DDGS 검색 라이브러리가 설치되지 않았습니다.")
        with DDGS() as ddgs:
            rows = list(ddgs.text(query, max_results=limit))
            rows.extend(ddgs.text(f"{query} official 공식", max_results=min(limit, 5)))
        seen, results = set(), []
        for row in rows:
            url = str(row.get("href", "")).strip()
            if url and url not in seen and urlparse(url).scheme in {"http", "https"}:
                seen.add(url)
                results.append({"url": url, "title": str(row.get("title", "")),
                                "snippet": str(row.get("body", ""))})
        return results

    def _get_research_agent(self):
        if self._research_agent is None:
            fetcher = PlaywrightPageFetcher(self._validate_url, self._guard_route)
            self._research_agent = ResearchAgent(self._search_candidates, fetcher, ResearchCache())
        return self._research_agent

    def _research_web(self, data):
        query = str(data.get("query", "")).strip()
        if not query: return ToolRunResult.failed(tool_name="browser_research", error="검색어가 비어 있습니다.")
        try:
            report = self._get_research_agent().research(
                query, max_sources=int(data.get("max_sources", 5)), profile=str(data.get("profile", "default")),
                ttl_seconds=float(data.get("ttl_seconds", 3600)), force_refresh=bool(data.get("force_refresh", False)))
            if not report.sources:
                return ToolRunResult.failed(tool_name="browser_research", error="실제로 방문해 검증할 수 있는 검색 결과가 없습니다.")
            payload = report.to_dict()
            return ToolRunResult.successful(tool_name="browser_research", raw_output=json.dumps(payload, ensure_ascii=False),
                evidence=[Evidence("research_report", "검색 결과를 실제 방문하고 본문·날짜·출처·상충 여부를 분석했습니다.", {
                    "source_count": len(report.sources), "claim_count": len(report.claims),
                    "conflict_count": len(report.conflicts), "cache_hit": report.cache_hit,
                    "expires_at": report.expires_at,
                })], artifacts=[Artifact("url", source.url, {"source_id": source.source_id,
                    "official": source.official, "published_at": source.published_at,
                    "modified_at": source.modified_at}) for source in report.sources])
        except Exception as exc:
            return ToolRunResult.failed(tool_name="browser_research", error=f"Research 실행 실패: {exc}")

    def _research_and_apply_preference(self, data):
        query = str(data.get("query", "")).strip()
        setting = str(data.get("setting", "")).strip()
        research = self._research_web({"query": query, "max_sources": data.get("max_sources", 5)})
        if not research.succeeded:
            return ToolRunResult.failed(tool_name="browser_research_and_apply_preference", error=research.error)
        try:
            payload = json.loads(research.raw_output)
            sources = payload.get("sources", [])
            evidence_text = "\n\n".join(
                f"[{item.get('source_id')}] {item.get('title')}\n{str(item.get('content', ''))[:1200]}"
                for item in sources[:6]
            )
            from core.llm import get_llm_client
            llm = get_llm_client("reasoning")
            response = llm.chat([
                {"role": "system", "content": (
                    "웹 근거에서 확인되는 고수준 대화 특성만 추출하세요. 인물의 대사를 복제하거나 "
                    "근거 없는 성격을 만들지 마세요. JSON 객체만 출력하세요: "
                    '{"summary":"근거 요약","preference":"160자 이내의 재사용 가능한 한국어 응답 지침"}'
                )},
                {"role": "user", "content": f"조사 질문: {query}\n\n검증 방문 본문:\n{evidence_text}"},
            ]).strip()
            try:
                match = re.search(r"\{.*\}", response, re.S)
                learned = json.loads(match.group(0) if match else response)
                summary = str(learned.get("summary", "")).strip()
                preference = " ".join(str(learned.get("preference", "")).split())
            except (TypeError, ValueError, json.JSONDecodeError):
                compact_evidence = "\n".join(evidence_text.splitlines()[:18])[:5000]
                fallback = llm.chat([
                    {"role": "system", "content": (
                        "제공된 웹 근거에서 확인되는 말투 특성만 120자 이내 한국어 한 문장으로 요약하세요. "
                        "대사를 복제하거나 근거 없는 성격을 만들지 말고 설명 없이 문장만 출력하세요."
                    )},
                    {"role": "user", "content": f"조사 질문: {query}\n웹 근거:\n{compact_evidence}"},
                ]).strip()
                if not fallback or fallback.startswith(("오류", "Error", "HTTP")):
                    raise ValueError("요약 모델이 유효한 응답을 반환하지 않았습니다.")
                summary = fallback[:160]
                preference = fallback[:160]
            if not summary or not preference:
                raise ValueError("웹 근거에서 적용 가능한 설정을 추출하지 못했습니다.")
            self._validate_learned_preference(query, preference)
            from core.assistant_settings import get_assistant_settings
            saved = get_assistant_settings().set(setting, preference)
            from core.rag import get_rag_manager
            doc_id = "web-learning-" + hashlib.sha256((query + saved).encode("utf-8")).hexdigest()[:16]
            source_lines = "\n".join(f"- {item.get('url')}" for item in sources)
            rag_text = f"조사 주제: {query}\n검증 요약: {summary}\n적용 설정: {saved}\n출처:\n{source_lines}"
            rag_manager = get_rag_manager()
            rag_manager.add_text_document(
                rag_text, doc_id=doc_id, namespace="global",
                source_uri=f"web-learning://{doc_id}",
                metadata={"source_type": "web", "kind": "learned_preference",
                          "query": query, "expires_at": payload.get("expires_at", 0)},
            )
            recalled = rag_manager.search_docs(
                query, top_k=10, metadata_filter={"source_type": "web"}, include_stale=True,
            )
            if not any(str(item.get("doc_id", "")) == doc_id for item in recalled):
                raise RuntimeError("저장 직후 RAG 재조회 검증에 실패했습니다.")
            return ToolRunResult.successful(
                tool_name="browser_research_and_apply_preference",
                raw_output=f"웹 출처 {len(sources)}개를 검증하고 응답 스타일에 반영했어. RAG 문서 ID: {doc_id}",
                evidence=[Evidence("web_learning", "웹 조사 요약을 설정 저장소와 RAG에서 다시 확인했습니다.", {
                    "query": query, "setting": setting, "saved": saved, "rag_doc_id": doc_id,
                    "source_count": len(sources), "source_urls": [item.get("url") for item in sources],
                    "rag_recalled": True,
                })],
                artifacts=[Artifact("knowledge", f"web-learning://{doc_id}", {"rag_doc_id": doc_id})],
            )
        except Exception as exc:
            return ToolRunResult.failed(
                tool_name="browser_research_and_apply_preference",
                error=f"웹 조사 결과를 설정과 RAG에 반영하지 못했습니다: {exc}",
            )

    @staticmethod
    def _validate_learned_preference(query: str, preference: str) -> None:
        """Reject identity leakage and vague prose before it can overwrite runtime behavior."""
        value = " ".join(str(preference or "").split())
        if len(value) < 20 or len(value) > 180:
            raise ValueError("추출된 말투 지침의 길이가 품질 기준을 충족하지 않습니다.")
        identity = re.match(r"^(.+?)\s+([^\s]+)\s+캐릭터(?:\s|$)", str(query or "").strip())
        if identity:
            game_name, character_name = identity.groups()
            if game_name in value or character_name in value:
                raise ValueError("말투 지침에 게임·캐릭터 정체성 문장이 섞여 저장을 중단했습니다.")
            if len(character_name) > 1 and re.search(
                rf"(?:^|\s){re.escape(character_name[1:])}(?:은|는|이|가)", value,
            ):
                raise ValueError("캐릭터명이 잘린 저품질 문장을 감지해 저장을 중단했습니다.")
        style_cues = ("말투", "반말", "존댓말", "어조", "표현", "대답", "답변", "밝", "솔직", "장난", "진지", "차분")
        if sum(cue in value for cue in style_cues) < 2:
            raise ValueError("응답 방식으로 사용할 만큼 구체적인 말투 특성이 부족합니다.")

    def _learning_status(self, data):
        setting = str(data.get("setting", "")).strip()
        from core.assistant_settings import get_assistant_settings
        from core.rag import get_rag_manager
        current = get_assistant_settings().get(setting)
        recalled = get_rag_manager().search_docs(
            "조사 주제 검증 요약 적용 설정", top_k=10,
            metadata_filter={"source_type": "web"}, include_stale=True,
        )
        learned = [item for item in recalled if str(item.get("source", "")).startswith("web-learning://")]
        if not learned:
            return ToolRunResult.successful(
                tool_name="browser_learning_status",
                raw_output="웹 조사 결과가 RAG에 저장된 기록은 아직 없어.",
                evidence=[Evidence("web_learning_status", "웹 출처 RAG를 재조회했습니다.", {
                    "setting": setting, "current": current, "rag_recalled": False,
                })],
            )
        latest = learned[0]
        return ToolRunResult.successful(
            tool_name="browser_learning_status",
            raw_output=f"웹 조사 학습이 반영되어 있어. 현재 설정은 '{current}'이고 RAG에서도 다시 조회됐어.",
            evidence=[Evidence("web_learning_status", "설정 저장소와 웹 출처 RAG를 함께 재조회했습니다.", {
                "setting": setting, "current": current, "rag_recalled": True,
                "rag_doc_id": latest.get("doc_id"), "source": latest.get("source"),
            })],
        )

    def _download(self, data):
        from core.harness import SafetyLayer
        ok, error = SafetyLayer.validate_path(str(data["path"]))
        if not ok: return ToolRunResult.failed(tool_name="browser_download", error=error)
        try:
            fetcher = self._get_research_agent().fetcher
            result = fetcher.download(str(data["url"]), str(data["path"]), profile=str(data.get("profile", "default")))
            return ToolRunResult.successful(tool_name="browser_download", raw_output="브라우저 다운로드를 완료했습니다.",
                evidence=[Evidence("browser_download", "저장 파일의 크기와 해시를 확인했습니다.", result)],
                artifacts=[Artifact("download", result["path"], {"sha256": result["sha256"], "size": result["size"]})])
        except Exception as exc:
            return ToolRunResult.failed(tool_name="browser_download", error=f"다운로드 실패: {exc}")

    def _profile_status(self, data):
        fetcher = self._get_research_agent().fetcher
        profile = fetcher._profile_name(str(data.get("profile", "default")))
        path = (fetcher.profiles_dir / profile).resolve()
        files = sum(1 for item in path.rglob("*") if item.is_file()) if path.exists() else 0
        return ToolRunResult.successful(tool_name="browser_profile_status", raw_output=("Profile 저장됨" if path.exists() else "Profile 미생성"),
            evidence=[Evidence("browser_profile", "Cookie 값을 노출하지 않고 영속 Profile 존재 여부만 확인했습니다.", {
                "profile": profile, "path": str(path), "exists": path.exists(), "file_count": files,
            })])

    @staticmethod
    def _search_web(tool_input: Dict[str, Any]):
        if DDGS is None:
            return ToolRunResult.failed(
                tool_name="browser_web_search",
                error="duckduckgo-search가 설치되지 않았습니다.",
            )
        query = str(tool_input.get("query", "")).strip()
        if not query:
            return ToolRunResult.failed(
                tool_name="browser_web_search", error="검색어가 비어 있습니다."
            )
        limit = max(1, min(int(tool_input.get("max_results", 5)), 10))
        try:
            with DDGS() as ddgs:
                rows = list(ddgs.text(query, max_results=limit))
                rows.extend(ddgs.text(f"{query} official 공식", max_results=limit))
            candidates = [
                {
                    "title": str(row.get("title", "")).strip(),
                    "url": str(row.get("href", "")).strip(),
                    "snippet": PromptInjectionGuard.isolate(str(row.get("body", "")).strip())[0],
                }
                for row in rows
                if row.get("href")
                and urlparse(str(row.get("href"))).scheme in {"http", "https"}
                and urlparse(str(row.get("href"))).netloc
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
                return ToolRunResult.failed(
                    tool_name="browser_web_search",
                    error="웹 검색 결과를 찾지 못했습니다.",
                )
            searched_at = datetime.now().astimezone().isoformat()
            output = json.dumps({
                "query": query,
                "searched_at": searched_at,
                "results": results,
            }, ensure_ascii=False)
            return ToolRunResult.successful(
                tool_name="browser_web_search",
                raw_output=output,
                evidence=[Evidence(
                    "web_search_sources",
                    f"웹 검색 결과 {len(results)}건의 출처 URL을 확인했습니다.",
                    {
                        "provider": "DDGS",
                        "query": query,
                        "searched_at": searched_at,
                        "source_count": len(results),
                    },
                )],
                artifacts=[
                    Artifact("url", item["url"], {"title": item["title"]})
                    for item in results
                ],
            )
        except Exception as exc:
            return ToolRunResult.failed(
                tool_name="browser_web_search", error=f"웹 검색 실패: {exc}"
            )

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name == "browser_research":
            try:
                payload = json.loads(result)
                claims = payload.get("claims", [])[:5]
                lines = [f"- {claim['text']} [{', '.join(claim['citations'])}]" for claim in claims]
                if payload.get("conflicts"):
                    lines.append(f"상충하는 정보 {len(payload['conflicts'])}건이 있어 출처별 값을 함께 확인해야 합니다.")
                lines.extend(f"[{source['source_id']}] {source['url']}" for source in payload.get("sources", []))
                return "\n".join(lines) if lines else "검증 가능한 주장을 찾지 못했습니다."
            except (json.JSONDecodeError, KeyError, TypeError):
                return result
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
