"""Naver calendar adapter: fixed UI operations on the user-selected Chrome tab."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from uuid import uuid4

from core.browser_extension import BrowserExtensionSession, CONNECT_TIMEOUT
from core.turn_context import check_turn_cancelled


class CalendarScreenError(ValueError):
    """Fixed diagnostic codes only; never expose a login URL or page content."""
    def __init__(self, stage, screen):
        self.stage, self.screen = stage, screen
        advice = {"login":"선택한 탭에서 직접 로그인한 뒤 다시 연결하세요.",
                  "calendar_other":"네이버 캘린더의 일정 메인 화면을 선택하세요.",
                  "other":"Playwright 안내 탭이 아닌 네이버 캘린더 탭을 선택하세요."}
        super().__init__("캘린더 화면 작업을 확인하지 못했습니다: " + stage + ". " + advice.get(screen, "현재 계정·캘린더 화면을 확인하세요."))


class NaverCalendarService:
    def __init__(self, session=None):
        self.session = session or BrowserExtensionSession()
        self._binding = ""
        self._account = ""
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._revision = 0

    @contextmanager
    def _operation(self, checkpoint):
        deadline = time.monotonic() + 90
        with self._state_lock:
            revision = self._revision
        while not self._lock.acquire(timeout=.1):
            checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError("다른 캘린더 작업을 기다리다 제한시간이 지났습니다.")
        try:
            checkpoint()
            with self._state_lock:
                if revision != self._revision:
                    raise ValueError("대기 중 캘린더 연결이 해제되었습니다.")
            yield revision
        finally:
            self._lock.release()

    def _snippet(self, body, *, bind=False):
        binding = self._binding or str(uuid4())
        return "async (page) => {\nlet stage = 'origin', screen = 'unknown';\ntry {\n" + """
          const u = new URL(page.url());
          screen = u.origin === 'https://calendar.naver.com'
            ? (u.pathname === '/main' ? 'calendar' : 'calendar_other')
            : (u.origin === 'https://nid.naver.com' ? 'login' : 'other');
          if (u.origin !== 'https://calendar.naver.com' || u.pathname !== '/main')
            throw new Error('Select the logged-in Naver calendar tab; authentication is manual');
          stage = 'account';
          const account = (await page.locator('#gnb_name1').innerText()).trim();
          if (!account || !(await page.getByRole('link', {name:'일정 쓰기', exact:true}).count()))
            throw new Error('Calendar/account observation unavailable');
          stage = 'tab_binding';
          const key = Symbol.for('anis.naver.calendar.binding');
        """ + f"const binding = {json.dumps(binding)};\n" + (
            "page[key] = binding;\n" if bind else
            "if (page[key] !== binding) throw new Error('Calendar tab changed; reconnect explicitly');\n"
        ) + f"const expectedAccount = {json.dumps(self._account, ensure_ascii=False)};\n" + """
          if (expectedAccount && account !== expectedAccount)
            throw new Error('Calendar account changed; reconnect explicitly');
        """ + "stage = 'operation';\n" + body + "\n} catch (error) { return {error_code:stage, error_type:error.name, screen}; }\n}"

    def _call(self, code, checkpoint, revision, validate, *, timeout=90):
        try:
            result = self.session.call(code, checkpoint=checkpoint, timeout=timeout)
            checkpoint()
            if isinstance(result, dict) and result.get("error_code") in {
                    "origin", "account", "tab_binding", "operation"}:
                screen = result.get("screen")
                if screen not in {"calendar", "calendar_other", "login", "other"}:
                    screen = "unknown"
                raise CalendarScreenError(result["error_code"], screen)
            with self._state_lock:
                if revision != self._revision:
                    raise ValueError("캘린더 연결이 해제되어 늦은 결과를 폐기했습니다.")
                validate(result)
            return result
        except BaseException:
            with self._state_lock:
                if revision == self._revision:
                    self._binding = self._account = ""
            raise

    def connect(self, *, checkpoint=check_turn_cancelled):
        with self._operation(checkpoint) as revision:
            with self._state_lock:
                self._binding = self._account = ""
            def validate(result):
                if (not isinstance(result, dict)
                        or result.get("origin") != "https://calendar.naver.com"
                        or not all(isinstance(result.get(k), str) and result[k].strip() for k in ("binding", "account"))
                        or result["account"].strip() == "내 프로필 이미지"):
                    raise ValueError("캘린더 탭/계정 표시를 확인할 증거가 없습니다.")
                self._binding, self._account = result["binding"], result["account"]
            self._call(self._snippet("return {binding, account, origin:u.origin};", bind=True),
                       checkpoint, revision, validate, timeout=CONNECT_TIMEOUT)
            return self.status()

    def status(self):
        with self._state_lock:
            return {"connected": bool(self._binding), "account": self._account,
                    "account_fingerprint": hashlib.sha256(self._account.encode()).hexdigest() if self._account else "",
                    "provider": "naver", "transport": "official_playwright_extension"}

    def disconnect(self):
        with self._state_lock:
            self._revision += 1
            self._binding = self._account = ""
        self.session.close()

    def observe(self, *, checkpoint=check_turn_cancelled):
        """Read the visible current view; never claim a whole-account sync."""
        with self._operation(checkpoint) as revision:
            if not self._binding:
                raise ValueError("P → 네이버 캘린더 연결에서 캘린더 탭을 먼저 연결하세요.")
            def validate(result):
                self.validate_view(result, self._account)
            result = self._call(self._snippet("""
              const text = await page.locator('body').innerText();
              const list = page.locator('#calendar_list_container');
              if (!(await list.isVisible())) throw new Error('Visible calendar list unavailable');
              const calendars = await list.innerText({timeout:5000});
              if (text.length > 32000) throw new Error('Visible calendar view exceeds result budget');
              return {account, text, calendars, url:page.url(), scope:'visible_view',
                timezone:null, timezone_source:'not_observed', complete_account:false};
            """), checkpoint, revision, validate)
            return result

    @staticmethod
    def validate_view(result, expected_account=""):
        if not isinstance(result, dict):
            raise ValueError("캘린더 조회 결과가 객체가 아닙니다.")
        url = urlsplit(str(result.get("url", "")))
        if (url.scheme != "https" or url.netloc != "calendar.naver.com" or url.path != "/main"
                or not all(isinstance(result.get(k), str) and result[k].strip() for k in ("text", "calendars", "account"))
                or len(result["text"]) > 32000 or result["account"].strip() == "내 프로필 이미지"
                or (expected_account and result["account"] != expected_account)
                or result.get("scope") != "visible_view" or result.get("timezone") is not None
                or result.get("timezone_source") != "not_observed"
                or result.get("complete_account") is not False):
            raise ValueError("현재 캘린더 표시 범위의 검증 증거가 없습니다.")

    def inspect_editor(self, *, checkpoint=check_turn_cancelled, show_timezone=False):
        """Inspect controls; an explicit timezone probe may open a blank editor."""
        with self._operation(checkpoint) as revision:
            if not self._binding:
                raise ValueError("캘린더를 먼저 연결하세요.")
            def validate(result):
                if not isinstance(result, dict) or not isinstance(result.get("controls"), list):
                    raise ValueError("캘린더 입력 컨트롤을 확인하지 못했습니다.")
            body = """
              if (!(await page.locator('input#tx0_0').isVisible())) {
                await page.getByRole('link', {name:'일정 쓰기', exact:true}).click();
                await page.locator('input#tx0_0').waitFor({state:'visible'});
              }
              await page.locator('._set_timezone.change_time').click();
            """ if show_timezone else ""
            return self._call(self._snippet(body + """
              if (new URL(page.url()).origin !== u.origin) throw new Error('Calendar origin changed');
              return {url:page.url(), controls:await page.locator('input,select,textarea,button').evaluateAll(
                els => els.filter(e => e.getClientRects().length).map(e => ({tag:e.tagName,
                  id:e.id, type:e.type, name:e.name, value:e.value, class:e.className,
                  text:e.tagName==='BUTTON'?e.innerText:'', placeholder:e.getAttribute('placeholder'),
                  selected:e.tagName==='SELECT'?[...e.selectedOptions].map(o=>({text:o.text,value:o.value})):undefined,
                  ancestors:e.tagName==='SELECT'?[e.parentElement,e.parentElement?.parentElement,
                    e.parentElement?.parentElement?.parentElement].filter(Boolean).map(p=>({tag:p.tagName,id:p.id,class:p.className})):undefined}))),
                text:(await page.locator('body').innerText()).slice(0,16000)};
            """), checkpoint, revision, validate)
