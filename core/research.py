"""Evidence-first web research runtime with cache and untrusted-content isolation."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from config import Config


INJECTION_PATTERNS = [
    re.compile(pattern, re.I) for pattern in (
        r"ignore (?:all |the )?(?:previous|prior) instructions",
        r"disregard (?:the )?(?:system|developer|previous)",
        r"system prompt", r"developer message", r"reveal .*?(?:prompt|secret|token)",
        r"(?:이전|위의) (?:지시|명령).*?(?:무시|따르지)", r"시스템 프롬프트", r"개발자 메시지",
        r"도구를 실행", r"파일을 삭제", r"비밀번호.*?출력",
    )
]


@dataclass
class ResearchSource:
    source_id: str
    url: str
    title: str
    content: str
    retrieved_at: float
    published_at: str = ""
    modified_at: str = ""
    official: bool = False
    status_code: int = 200
    content_type: str = "text/html"
    tables: List[List[List[str]]] = field(default_factory=list)
    injection_warnings: List[str] = field(default_factory=list)
    content_sha256: str = ""

    def __post_init__(self):
        if not self.content_sha256:
            self.content_sha256 = hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class ResearchClaim:
    text: str
    citations: List[str]
    support_count: int
    conflicting_citations: List[str] = field(default_factory=list)


@dataclass
class ResearchReport:
    query: str
    created_at: float
    expires_at: float
    sources: List[ResearchSource]
    claims: List[ResearchClaim]
    conflicts: List[Dict[str, Any]]
    cache_hit: bool = False

    def to_dict(self):
        return {
            "query": self.query, "created_at": self.created_at, "expires_at": self.expires_at,
            "cache_hit": self.cache_hit,
            "sources": [asdict(item) for item in self.sources],
            "claims": [asdict(item) for item in self.claims], "conflicts": self.conflicts,
        }


class ResearchCache:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or Path(Config.DB_PATH).with_name("research_cache.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._session() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS research_cache (
                cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL,
                expires_at REAL NOT NULL)""")

    @contextmanager
    def _session(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._session() as conn:
            row = conn.execute("SELECT payload,expires_at FROM research_cache WHERE cache_key=?", (key,)).fetchone()
            if not row: return None
            if row[1] <= time.time():
                conn.execute("DELETE FROM research_cache WHERE cache_key=?", (key,))
                return None
            return json.loads(row[0])

    def put(self, key: str, payload: Dict[str, Any], ttl_seconds: float):
        now = time.time()
        with self._session() as conn:
            conn.execute("INSERT OR REPLACE INTO research_cache VALUES (?,?,?,?)",
                         (key, json.dumps(payload, ensure_ascii=False), now, now + ttl_seconds))


class PromptInjectionGuard:
    @staticmethod
    def isolate(content: str) -> tuple[str, List[str]]:
        warnings, safe_lines = [], []
        for line in content.splitlines():
            matched = [pattern.pattern for pattern in INJECTION_PATTERNS if pattern.search(line)]
            if matched:
                warnings.extend(matched)
                safe_lines.append("[격리된 웹 지시문]")
            else:
                safe_lines.append(line)
        return "\n".join(safe_lines), list(dict.fromkeys(warnings))


class PlaywrightPageFetcher:
    """Fetches rendered pages with an optional persistent, user-selected browser profile."""
    def __init__(self, url_validator: Callable[[str], str], route_guard: Optional[Callable] = None,
                 profiles_dir: Optional[str] = None):
        self.url_validator = url_validator
        self.route_guard = route_guard
        self.profiles_dir = Path(profiles_dir or Path(Config.DB_PATH).with_name("browser_profiles"))
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _profile_name(name: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name or "default")[:60].strip("._")
        return cleaned or "default"

    def fetch(self, url: str, *, profile: str = "default", dynamic: bool = True) -> ResearchSource:
        from playwright.sync_api import sync_playwright
        validated = self.url_validator(url)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profiles_dir / self._profile_name(profile)), headless=True,
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            if self.route_guard: page.route("**/*", self.route_guard)
            response = page.goto(validated, wait_until="domcontentloaded", timeout=30000)
            if dynamic:
                try: page.wait_for_load_state("networkidle", timeout=5000)
                except Exception: pass
            self.url_validator(page.url)
            if response is None or not response.ok:
                raise RuntimeError(f"페이지 응답 실패: {response.status if response else '응답 없음'}")
            content_type = (response.headers.get("content-type") or "text/html").split(";", 1)[0]
            if content_type == "application/pdf" or page.url.casefold().endswith(".pdf"):
                content = self._pdf_text(response.body())
                tables = []
            else:
                content = page.locator("article, main, body").first.inner_text()[:150000]
                tables = page.locator("table").evaluate_all("""tables => tables.slice(0,20).map(t =>
                    Array.from(t.rows).map(r => Array.from(r.cells).map(c => c.innerText.trim())))""")
            safe_content, warnings = PromptInjectionGuard.isolate(content)
            published_at, modified_at = self._dates(page, response.headers)
            source = ResearchSource(
                source_id="", url=page.url, title=page.title(), content=safe_content,
                retrieved_at=time.time(), published_at=published_at, modified_at=modified_at,
                status_code=response.status, content_type=content_type, tables=tables,
                injection_warnings=warnings,
            )
            context.close()
            return source

    @staticmethod
    def _meta(page, names: Iterable[str]) -> str:
        for name in names:
            locator = page.locator(f'meta[property="{name}"], meta[name="{name}"], time[datetime]').first
            if locator.count():
                value = locator.get_attribute("content") or locator.get_attribute("datetime")
                if value: return value
        return ""

    @classmethod
    def _dates(cls, page, headers: Dict[str, str]) -> tuple[str, str]:
        published = cls._meta(page, ["article:published_time", "datePublished", "date", "pubdate"])
        modified = cls._meta(page, ["article:modified_time", "dateModified", "last-modified"])
        try:
            scripts = page.locator('script[type="application/ld+json"]').all_text_contents()
            values = []
            for script in scripts:
                try:
                    payload = json.loads(script)
                    values.extend(payload if isinstance(payload, list) else [payload])
                except json.JSONDecodeError:
                    continue
            queue = list(values)
            while queue:
                value = queue.pop(0)
                if isinstance(value, dict):
                    published = published or str(value.get("datePublished", ""))
                    modified = modified or str(value.get("dateModified", ""))
                    queue.extend(item for item in value.values() if isinstance(item, (dict, list)))
                elif isinstance(value, list):
                    queue.extend(value)
        except Exception:
            pass
        modified = modified or str(headers.get("last-modified", ""))
        return published, modified

    @staticmethod
    def _pdf_text(data: bytes) -> str:
        import fitz
        document = fitz.open(stream=data, filetype="pdf")
        try: return "\n".join(page.get_text("text") for page in document)[:150000]
        finally: document.close()

    def download(self, url: str, destination: str, *, profile: str = "default") -> Dict[str, Any]:
        from playwright.sync_api import sync_playwright
        validated = self.url_validator(url)
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profiles_dir / self._profile_name(profile)), headless=True, accept_downloads=True)
            page = context.pages[0] if context.pages else context.new_page()
            if self.route_guard: page.route("**/*", self.route_guard)
            with page.expect_download(timeout=30000) as pending:
                page.goto(validated, wait_until="commit", timeout=30000)
            download = pending.value
            download.save_as(str(target))
            context.close()
        return {"path": str(target), "size": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "suggested_filename": download.suggested_filename}


class ResearchAgent:
    def __init__(self, search_provider: Callable[[str, int], List[Dict[str, str]]],
                 fetcher: Any, cache: Optional[ResearchCache] = None):
        self.search_provider, self.fetcher = search_provider, fetcher
        self.cache = cache or ResearchCache()

    def research(self, query: str, *, max_sources: int = 5, profile: str = "default",
                 ttl_seconds: float = 3600, force_refresh: bool = False) -> ResearchReport:
        key = hashlib.sha256(f"{query}|{max_sources}|{profile}".encode()).hexdigest()
        if not force_refresh and (cached := self.cache.get(key)):
            report = self._report_from_dict(cached)
            report.cache_hit = True
            return report
        candidates = self.search_provider(query, max(max_sources * 2, 5))
        sources = []
        for candidate in candidates:
            if len(sources) >= max_sources: break
            try:
                source = self.fetcher.fetch(candidate["url"], profile=profile, dynamic=True)
                source.source_id = f"S{len(sources) + 1}"
                source.title = source.title or candidate.get("title", "")
                source.official = self._official_score(source, query) >= 20
                sources.append(source)
            except Exception:
                continue
        sources.sort(key=lambda source: (source.official, bool(source.modified_at or source.published_at)), reverse=True)
        for index, source in enumerate(sources, 1): source.source_id = f"S{index}"
        claims, conflicts = self._cross_validate(sources)
        now = time.time()
        report = ResearchReport(query, now, now + ttl_seconds, sources, claims, conflicts)
        self.cache.put(key, report.to_dict(), ttl_seconds)
        return report

    @staticmethod
    def _official_score(source: ResearchSource, query: str) -> int:
        host = (urlparse(source.url).hostname or "").casefold()
        score = 30 if host.endswith((".go.kr", ".gov", ".gov.uk", ".edu", ".ac.kr")) else 0
        text = f"{source.title} {source.content[:500]}".casefold()
        if any(word in text for word in ("official", "공식", "보도자료", "newsroom")): score += 20
        query_tokens = {token for token in re.findall(r"[a-z0-9가-힣]+", query.casefold()) if len(token) > 2}
        if query_tokens & set(host.replace("-", ".").split(".")): score += 10
        return score

    @staticmethod
    def _sentences(content: str) -> List[str]:
        return [part.strip() for part in re.split(r"(?<=[.!?。])\s+|\n+", content) if 10 <= len(part.strip()) <= 500]

    def _cross_validate(self, sources: List[ResearchSource]):
        groups: List[Dict[str, Any]] = []
        for source in sources:
            for sentence in self._sentences(source.content)[:30]:
                tokens = {t for t in re.findall(r"[a-z0-9가-힣]+", sentence.casefold()) if len(t) > 1}
                if not tokens: continue
                match = next((group for group in groups if len(tokens & group["tokens"]) / max(1, min(len(tokens), len(group["tokens"]))) >= 0.65), None)
                if match:
                    match["items"].append((source.source_id, sentence))
                    match["tokens"] |= tokens
                else:
                    groups.append({"tokens": tokens, "items": [(source.source_id, sentence)]})
        claims, conflicts = [], []
        for group in groups:
            citations = list(dict.fromkeys(item[0] for item in group["items"]))
            variants = list(dict.fromkeys(item[1] for item in group["items"]))
            numeric_sets = {tuple(re.findall(r"\d+(?:\.\d+)?%?", text)) for text in variants}
            conflicting = citations if len(numeric_sets) > 1 and any(numeric_sets) else []
            claims.append(ResearchClaim(variants[0], citations, len(citations), conflicting))
            if conflicting:
                conflicts.append({"claim": variants[0], "citations": citations, "variants": variants})
        claims.sort(key=lambda claim: (claim.support_count, len(claim.text)), reverse=True)
        return claims[:30], conflicts

    @staticmethod
    def _report_from_dict(payload: Dict[str, Any]) -> ResearchReport:
        return ResearchReport(
            query=payload["query"], created_at=payload["created_at"], expires_at=payload["expires_at"],
            sources=[ResearchSource(**item) for item in payload.get("sources", [])],
            claims=[ResearchClaim(**item) for item in payload.get("claims", [])],
            conflicts=payload.get("conflicts", []), cache_hit=payload.get("cache_hit", False),
        )
