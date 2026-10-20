from types import SimpleNamespace

import pytest

from core.research import PlaywrightPageFetcher


def response(url, status=200, location=""):
    return SimpleNamespace(url=url, status=status, headers={"location": location}, dispose=lambda: None)


def test_redirect_private_target_is_rejected_before_network(tmp_path):
    calls = []

    def validate(url):
        if "127.0.0.1" in url:
            raise ValueError("private target")
        return url

    route = SimpleNamespace(
        request=SimpleNamespace(url="https://public.example/document", method="GET"),
        fetch=lambda **kwargs: response("https://public.example/document", 302, "http://127.0.0.1/private"),
    )
    context = SimpleNamespace(request=SimpleNamespace(get=lambda *args, **kwargs: calls.append(args)))
    fetcher = PlaywrightPageFetcher(validate, profiles_dir=str(tmp_path))
    with pytest.raises(ValueError, match="private target"):
        fetcher._fetch_response(route, context)
    assert calls == []


@pytest.mark.parametrize("timeout", [None, 12000])
def test_upstream_request_timeout_keeps_legacy_default_and_allows_public_budget(tmp_path, timeout):
    calls = []
    route = SimpleNamespace(request=SimpleNamespace(url="https://public.example/page", method="GET"),
                            fetch=lambda **kwargs: (calls.append(kwargs), response("https://public.example/page"))[1])
    fetcher = PlaywrightPageFetcher(lambda url: url, profiles_dir=str(tmp_path))
    options = {} if timeout is None else {"timeout": timeout}
    fetcher._fetch_response(route, SimpleNamespace(), **options)
    assert calls == [{"max_redirects": 0, "timeout": timeout or 30000}]


def test_every_redirect_is_validated_and_cycles_are_rejected(tmp_path):
    validated, network = [], []
    first, second = "https://one.example/a", "https://two.example/b"

    def validate(url):
        validated.append(url)
        return url

    def get(url, **kwargs):
        network.append((url, kwargs["max_redirects"]))
        return response(second, 302, first)

    route = SimpleNamespace(request=SimpleNamespace(url=first, method="GET"),
                            fetch=lambda **kwargs: response(first, 302, second))
    context = SimpleNamespace(request=SimpleNamespace(get=get))
    with pytest.raises(RuntimeError, match="반복"):
        PlaywrightPageFetcher(validate, profiles_dir=str(tmp_path))._fetch_response(route, context)
    assert validated == [first, second, first]
    assert network == [(second, 0)]


@pytest.mark.parametrize("redirects", [5, 6])
def test_redirect_limit_includes_five_hops(tmp_path, redirects):
    first = "https://public.example/0"

    def get(url, **kwargs):
        index = int(url.rsplit("/", 1)[-1])
        assert kwargs["max_redirects"] == 0
        return response(url, 302 if index < redirects else 200, f"/{index + 1}")

    route = SimpleNamespace(request=SimpleNamespace(url=first, method="GET"),
                            fetch=lambda **kwargs: response(first, 302, "/1"))
    context = SimpleNamespace(request=SimpleNamespace(get=get))
    fetcher = PlaywrightPageFetcher(lambda url: url, profiles_dir=str(tmp_path))
    if redirects == 5:
        assert fetcher._fetch_response(route, context).url.endswith("/5")
    else:
        with pytest.raises(RuntimeError, match="5회"):
            fetcher._fetch_response(route, context)


def test_public_context_ignores_saved_profile_and_closes_on_failure(monkeypatch, tmp_path):
    state = {}
    def goto(*args, **kwargs):
        state["navigation_timeout"] = kwargs["timeout"]
        raise RuntimeError("navigation failed")
    page = SimpleNamespace(set_default_timeout=lambda value: None,
                           goto=goto)
    context = SimpleNamespace(pages=[], new_page=lambda: page, route=lambda *args: None,
                              close=lambda: state.update(context_closed=True))
    browser = SimpleNamespace(new_context=lambda **kwargs: (state.update(options=kwargs), context)[1],
                              close=lambda: state.update(browser_closed=True))

    class Runtime:
        chromium = SimpleNamespace(launch=lambda **kwargs: browser)
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", Runtime)
    fetcher = PlaywrightPageFetcher(lambda url: url, profiles_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="navigation failed"):
        fetcher.fetch("https://public.example", profile="private-login", public_only=True)
    assert state["options"] == {"accept_downloads": False, "service_workers": "block"}
    assert state["context_closed"] and state["browser_closed"]
    assert state["navigation_timeout"] == 12000
    assert not (tmp_path / "private-login").exists()


def test_publication_time_is_not_invented_as_modification_time():
    class Locator:
        first = property(lambda self: self)
        def __init__(self, time=False): self.time = time
        def count(self): return int(self.time)
        def get_attribute(self, name): return "2026-10-01" if name == "datetime" else None
        def all_text_contents(self): return []

    page = SimpleNamespace(locator=lambda selector: Locator(selector == "time[datetime]"))
    assert PlaywrightPageFetcher._dates(page, {"last-modified": "2026-10-02"}) == (
        "2026-10-01", "2026-10-02")


@pytest.mark.parametrize("title,text,error", [
    ("Just a moment...", "Verify you are human before we show the requested document.", "CAPTCHA"),
    ("Log in | Social", "Please log in to read this post and continue using your account.", "로그인"),
    ("Empty", "   ", "본문"),
])
def test_access_gate_and_empty_content_are_not_evidence(title, text, error):
    page = SimpleNamespace(locator=lambda selector: SimpleNamespace(count=lambda: 0))
    with pytest.raises(RuntimeError, match=error):
        PlaywrightPageFetcher._require_readable(page, "https://public.example/article", title, text)


def test_article_about_login_is_readable():
    page = SimpleNamespace(locator=lambda selector: SimpleNamespace(count=lambda: 0))
    PlaywrightPageFetcher._require_readable(page, "https://public.example/article", "로그인 보안 가이드",
                                          "이 문서는 로그인 방법과 계정 보안을 설명합니다. 비밀번호를 안전하게 관리하고 인증 화면을 확인하세요.")


@pytest.fixture
def public_test_server():
    import fitz
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    document = fitz.open()
    document.new_page().insert_text((72, 72), "Public PDF evidence for the research fetcher.")
    pdf = document.tobytes()
    document.close()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/article")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf" if self.path == "/paper" else "text/html; charset=utf-8")
            self.send_header("Last-Modified", "Mon, 12 Oct 2026 00:00:00 GMT")
            self.end_headers()
            if self.path == "/paper":
                self.wfile.write(pdf)
            elif self.path in {"/long-menu", "/long-menu-main", "/long-menu-article"}:
                content = (
                    '<header><h1>Company</h1><nav>' + 'Product menu header item. ' * 900 + '</nav></header>'
                    '<h1>Concrete content methods</h1>'
                    '<div style="display:none">Invisible menu chatter.</div>'
                    '<p>Film the making process in clear steps. Compare two examples and record the observed results.</p>'
                )
                if self.path == "/long-menu-main": content = '<main>' + content + '</main>'
                if self.path == "/long-menu-article":
                    content = '<main>' + content.replace('<h1>Concrete', '<article><h1>Concrete')
                    content += '</article><aside>Promotional footer outside the article.</aside></main>'
                self.wfile.write((
                    '<html><head><title>Concrete content methods | Company</title>'
                    '<meta property="article:published_time" content="2026-10-01"></head><body>'
                    + content + '</body></html>'
                ).encode())
            else:
                cookie = self.headers.get("Cookie", "none")
                self.wfile.write((
                    '<html><head><title>Research article</title></head><body><nav>Navigation only</nav>'
                    '<main><time datetime="2026-10-01">Publication</time>'
                    'Public evidence about recent trends with sufficient readable article text. '
                    f'Cookie received: {cookie}.</main></body></html>'
                ).encode())

        def log_message(self, *args): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.integration
def test_real_chromium_pdf_and_redirect_extract_final_public_source(public_test_server, tmp_path):
    origin = public_test_server

    def validate(url):
        if not url.startswith(origin + "/"):
            raise ValueError("Only this isolated fixture origin is permitted")
        return url

    fetcher = PlaywrightPageFetcher(validate, profiles_dir=str(tmp_path))
    pdf = fetcher.fetch(origin + "/paper", public_only=True)
    assert pdf.url == origin + "/paper" and pdf.content_type == "application/pdf"
    assert "Public PDF evidence" in pdf.content
    article = fetcher.fetch(origin + "/redirect", public_only=True)
    assert article.url == origin + "/article"
    assert "Public evidence" in article.content and "Navigation only" not in article.content
    assert article.published_at == "2026-10-01" and article.modified_at.startswith("Mon, 12 Oct")


@pytest.mark.integration
@pytest.mark.parametrize("path", ["/long-menu", "/long-menu-main", "/long-menu-article"])
def test_real_chromium_matching_headline_removes_long_menu_before_evidence(public_test_server, tmp_path, path):
    fetcher = PlaywrightPageFetcher(lambda url: url, profiles_dir=str(tmp_path))
    source = fetcher.fetch(public_test_server + path, public_only=True)
    assert source.content.startswith("Concrete content methods")
    assert "Film the making process in clear steps." in source.content[:6000]
    assert "Product menu header item" not in source.content
    assert "Invisible menu chatter" not in source.content
    assert "Promotional footer outside the article" not in source.content
    assert source.published_at == "2026-10-01"


@pytest.mark.integration
def test_response_cleanup_failure_does_not_abort_fulfilled_page(public_test_server, tmp_path, monkeypatch):
    from playwright.sync_api import APIResponse, Route

    disposed, aborted = [], []
    original_dispose, original_abort = APIResponse.dispose, Route.abort

    def closed_response(response):
        original_dispose(response)
        disposed.append(True)
        raise RuntimeError("Target page, context or browser has been closed")

    def abort(route, *args, **kwargs):
        aborted.append(True)
        return original_abort(route, *args, **kwargs)

    monkeypatch.setattr(APIResponse, "dispose", closed_response)
    monkeypatch.setattr(Route, "abort", abort)
    fetcher = PlaywrightPageFetcher(lambda url: url, profiles_dir=str(tmp_path))
    source = fetcher.fetch(public_test_server + "/article", dynamic=False, public_only=True)
    assert "Public evidence" in source.content
    assert disposed and not aborted


@pytest.mark.integration
def test_real_chromium_public_research_does_not_read_profile_cookies(public_test_server, tmp_path):
    import time
    from playwright.sync_api import sync_playwright

    origin = public_test_server
    fetcher = PlaywrightPageFetcher(lambda url: url, profiles_dir=str(tmp_path))
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(tmp_path / "saved"), headless=True)
        context.add_cookies([{"name": "fixture-session", "value": "private-placeholder", "url": origin,
                             "expires": time.time() + 3600}])
        context.close()
    private = fetcher.fetch(origin + "/article", profile="saved")
    assert "private-placeholder" in private.content
    source = fetcher.fetch(origin + "/article", profile="saved", public_only=True)
    assert "Cookie received: none" in source.content
    assert "private-placeholder" not in source.content
