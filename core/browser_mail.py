"""Read visible mail lists and selected bodies in a user-approved Chrome session.

Chrome owns the session; saved sign-in is delegated to BrowserLoginService.
No cookies or private mail APIs are read. Mail stays in memory.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import psutil
from uuid import uuid4

from core.browser_extension import (BrowserExtensionCredentials, BrowserExtensionCredentialsError,
                                    BrowserExtensionSession, BrowserExtensionUnavailableError, CONNECT_TIMEOUT)
from core.browser_login import BrowserLoginService, SITES, open_chrome
from core.plugin import ToolCancelledError
from core.turn_context import check_turn_cancelled


MAIL_SITES = {
    "gmail": "https://mail.google.com/mail/u/0/#inbox",
    "naver": "https://mail.naver.com/",
}
_ORIGINS = {"gmail": "https://mail.google.com", "naver": "https://mail.naver.com"}
_LOGIN_SITES = {"gmail": SITES["google"]["url"], "naver": SITES["naver"]["url"]}
_LOGIN_ORIGINS = {"gmail": "https://accounts.google.com", "naver": "https://nid.naver.com"}
_LANDINGS = {"gmail": "https://myaccount.google.com", "naver": "https://www.naver.com"}
BODY_LIMIT = 200000
_ERRORS = {
    "origin": "선택한 서비스의 로그인된 메일 목록 탭을 연결하세요.",
    "account": "로그인 계정 표시를 확인하지 못했습니다. Chrome에서 계정을 확인하세요.",
    "account_changed": "메일 계정이 변경되었습니다. 연결을 해제한 뒤 다시 조회하세요.",
    "tab_changed": "선택한 메일 탭이 변경되었습니다. 연결을 해제한 뒤 다시 조회하세요.",
    "mail_list": "메일 목록 화면을 확인하지 못했습니다. Chrome에서 메일 목록을 연 뒤 다시 조회하세요.",
    "login_required": "기존 Chrome 로그인 창에 계정을 저장한 뒤 다시 조회하세요.",
    "user_action_required": "Chrome에서 캡차·추가 인증 또는 계정 선택을 완료한 뒤 다시 조회하세요.",
    "ambiguous_tabs": "같은 서비스의 메일·로그인 탭이 여러 개입니다. 사용할 탭 하나만 남긴 뒤 다시 조회하세요.",
    "message_changed": "메일 목록이 변경되었습니다. 목록을 다시 조회한 뒤 메일을 선택하세요.",
    "message_body": "선택한 메일의 본문을 확인하지 못했습니다. 목록을 다시 조회한 뒤 다시 선택하세요.",
    "draft_count": "임시보관함의 전체 초안 건수를 확인하지 못했습니다. 확인 불가로 표시합니다.",
}
_BODY_STAGES = {
    "source_account": "목록 계정 확인",
    "new_tab": "본문 탭 연결",
    "open_mail": "메일 화면 열기",
    "wait_body": "본문 표시 대기",
    "detail_account": "본문 계정 확인",
    "expand": "대화 본문 펼치기",
    "read_body": "본문 읽기",
    "verify_account": "조회 후 계정 확인",
    "restore_list": "목록 화면 복귀",
}


class BrowserMailError(ValueError):
    """Application-authored diagnostics; never include browser/server content."""


# ponytail: current rendered list only; add provider APIs if whole-mailbox sync is requested.
_GMAIL_ACCOUNT_DOM = r"""
    const labels = Array.from(document.querySelectorAll('header [aria-label]'))
      .filter(visible).map(e => e.getAttribute('aria-label') || '')
      .filter(s => /^Google\s+(?:계정|Account)\s*:/i.test(s));
    const accounts = [...new Set(labels.map(s => s.match(/\(([^()\r\n]+)\)\s*$/)?.[1]?.trim().toLowerCase() || '')
      .filter(s => /^[^\s@]+@[^\s@]+$/.test(s)))];
    if (accounts.length !== 1) return {error_code:'account'};
    const account = accounts[0];
"""

_GMAIL_DOM = _GMAIL_ACCOUNT_DOM + r"""
    const rows = Array.from(document.querySelectorAll('tr.zA[role="row"]')).filter(visible);
    const empty = Array.from(document.querySelectorAll('td.TC')).filter(visible)
      .some(e => /^(검색어와 일치하는 메일이 없습니다\.|No conversations|No messages)/.test(text(e)));
    if (!rows.length && !empty) return {error_code:'mail_list'};
    const items = rows.slice(0, limit).map(row => ({
      sender: text(row.querySelector('.yW')),
      subject: text(row.querySelector('.bog')) || '(제목 없음)',
      date: (row.querySelector('.xW span[title]')?.getAttribute('title') || text(row.querySelector('.xW'))).slice(0, 2000),
      unread: row.classList.contains('zE'),
    }));
"""

_NAVER_ACCOUNT_DOM = r"""
    const accounts = [...new Set(Array.from(document.querySelectorAll('#gnb a.gnb_mail_address'))
      .filter(visible).map(e => text(e).toLowerCase()))];
    if (accounts.length !== 1 || !/^[^\s@]+@[^\s@]+$/.test(accounts[0])) return {error_code:'account'};
    const account = accounts[0];
"""

_NAVER_DOM = r"""
    if (!/^\/v2\/folders\/[^/]+\/all\/?$/.test(location.pathname)) return {error_code:'mail_list'};
""" + _NAVER_ACCOUNT_DOM + r"""
    const lists = Array.from(document.querySelectorAll('ul.mail_list')).filter(visible);
    if (lists.length !== 1) return {error_code:'mail_list'};
    const rows = Array.from(lists[0].querySelectorAll('li.mail_item')).filter(visible);
    if (!rows.length) return {error_code:'mail_list'};
    const items = rows.slice(0, limit).map(row => {
      const read = row.classList.contains('read'), toggle = row.querySelector('input.toggle_read');
      if (!toggle || toggle.checked !== read) return null;
      const sender = row.querySelector('.button_sender')?.cloneNode(true);
      sender?.querySelectorAll('.blind').forEach(label => label.remove());
      return {
        sender: (sender?.textContent || '').trim().slice(0, 2000),
        subject: text(row.querySelector('.mail_title_link .text')) || '(제목 없음)',
        date: text(Array.from(row.querySelectorAll('.mail_date_wrap .mail_date')).find(visible)),
        unread: !read,
      };
    });
    if (items.some(item => !item)) return {error_code:'mail_list'};
"""


class BrowserMailService:
    def __init__(self, session=None, connection_credentials=None, analysis=None):
        self.connection_credentials = connection_credentials or BrowserExtensionCredentials()
        self.session = session or BrowserExtensionSession(
            environment=self.connection_credentials.environment, client_name="ANIS Mail")
        self.login_service = BrowserLoginService(session=self.session)
        self._lock = threading.Lock()
        self.workflow_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._revision = 0
        self._provider = self._binding = self._fingerprint = ""
        self._fingerprints = {}
        self._messages = {}
        self._analysis = analysis

    @property
    def analysis(self):
        if self._analysis is None:
            from core.mail_analysis import MailAnalysisService
            self._analysis = MailAnalysisService()
        return self._analysis

    @staticmethod
    def _provider_name(provider):
        if not isinstance(provider, str) or provider not in MAIL_SITES:
            raise BrowserMailError("Gmail 또는 네이버 메일을 선택하세요.")
        return provider

    def open(self, provider):
        provider = self._provider_name(provider)
        if not self._lock.acquire(blocking=False):
            raise BrowserMailError("이전 메일 조회가 진행 중입니다.")
        try:
            self.disconnect()
            return open_chrome(MAIL_SITES[provider])
        except BrowserMailError:
            raise
        except Exception:
            raise BrowserMailError("Chrome에서 메일을 열지 못했습니다. Chrome 설치·연결 상태를 확인하세요.") from None
        finally:
            self._lock.release()

    def disconnect(self):
        self._disconnect(reset_accounts=True)

    def _disconnect(self, *, reset_accounts):
        with self._state_lock:
            self._revision += 1
            self._provider = self._binding = self._fingerprint = ""
            self._messages.clear()
            if reset_accounts:
                self._fingerprints.clear()
        self.login_service.disconnect()

    def _automatic(self):
        try:
            return self.connection_credentials.status().get("configured") is True
        except BrowserExtensionCredentialsError as exc:
            raise BrowserMailError(str(exc)) from None

    def switch_provider(self):
        if not self._automatic():
            self.disconnect()
            return
        with self._state_lock:
            self._revision += 1
            self._provider = self._binding = self._fingerprint = ""
            self._messages.clear()

    @staticmethod
    def _tabs_snippet(provider, nonce, *, create=False):
        config = json.dumps({"origin": _ORIGINS[provider], "loginOrigin": _LOGIN_ORIGINS[provider],
                             "loginPath": "/nidlogin.login" if provider == "naver" else "",
                             "url": MAIL_SITES[provider], "nonce": nonce})
        return "async (page) => { const config = " + config + ";\n" + r"""
          const key = Symbol.for('anis.browser.mail.route');
          const context = page.context();
          const candidates = context.pages().flatMap((tab, index) => {
            try {
              const u = new URL(tab.url());
              if (u.username || u.password) return [];
              const kind = u.origin === config.origin ? 'mail' :
                u.origin === config.loginOrigin && (!config.loginPath || u.pathname === config.loginPath) ? 'login' : '';
              if (!kind) return [];
              const route = config.nonce + ':' + index;
              tab[key] = route;
              return [{index, kind, route}];
            } catch (_) { return []; }
          });
        """ + (r"""
          if (candidates.length) return {error_code:'tab_changed'};
          const created = await context.newPage();
          try {
            await created.goto(config.url, {waitUntil:'domcontentloaded', timeout:15000});
            const index = context.pages().indexOf(created);
            if (index < 0) return {error_code:'tab_changed'};
            const route = config.nonce + ':' + index;
            created[key] = route;
            return {tabs:[{index, kind:'mail', route}]};
          } catch (_) {
            await created.close().catch(() => {});
            return {error_code:'mail_list'};
          }
        """ if create else "return {tabs:candidates};") + " }"

    def _select_provider_tab(self, provider, checkpoint):
        """Only automatic connections route tabs; manual selection stays explicit."""
        nonce = str(uuid4())
        result = self.session.call(self._tabs_snippet(provider, nonce), checkpoint=checkpoint, timeout=90)
        checkpoint()
        for create in (False, True):
            if isinstance(result, dict) and result.get("error_code") in _ERRORS:
                raise BrowserMailError(_ERRORS[result["error_code"]])
            if not isinstance(result, dict) or not isinstance(result.get("tabs"), list):
                raise BrowserMailError(_ERRORS["tab_changed"])
            tabs = result["tabs"]
            if any(not isinstance(tab, dict) or type(tab.get("index")) is not int or tab["index"] < 0
                   or tab.get("kind") not in ("mail", "login")
                   or tab.get("route") != f"{nonce}:{tab['index']}" for tab in tabs):
                raise BrowserMailError(_ERRORS["tab_changed"])
            candidates = [tab for tab in tabs if tab["kind"] == "mail"] or tabs
            if len(candidates) > 1:
                raise BrowserMailError(_ERRORS["ambiguous_tabs"])
            if candidates:
                chosen = candidates[0]
                selected = self.session.select_tab(chosen["index"], checkpoint=checkpoint, timeout=90)
                checkpoint()
                if selected != {"selected": True, "index": chosen["index"]}:
                    raise BrowserMailError(_ERRORS["tab_changed"])
                return chosen["route"]
            if create:
                raise BrowserMailError(_ERRORS["tab_changed"])
            result = self.session.call(self._tabs_snippet(provider, nonce, create=True),
                                       checkpoint=checkpoint, timeout=90)
            checkpoint()
        raise BrowserMailError(_ERRORS["tab_changed"])

    @staticmethod
    def _snippet(provider, limit, binding, expected_fingerprint, *, bind, route="", inbox=False):
        config = json.dumps({"provider": provider, "origin": _ORIGINS[provider],
                             "limit": limit, "binding": binding,
                             "expectedFingerprint": expected_fingerprint,
                             "route": route,
                             "inbox": inbox,
                             "url": MAIL_SITES[provider],
                             "references": str(uuid4()),
                             "loginOrigin": _LOGIN_ORIGINS[provider], "landingOrigin": _LANDINGS[provider]})
        return "async (page) => { let closeProfile = false; let mailBinding = ''; try {\nconst config = " + config + ";\n" + r"""
          const key = Symbol.for('anis.browser.mail.binding');
          const routeKey = Symbol.for('anis.browser.mail.route');
          const messagesKey = Symbol.for('anis.browser.mail.messages');
          mailBinding = config.binding;
          const guard = () => {
            if (new URL(page.url()).origin !== config.origin) throw new Error('origin');
            if (page[key] !== config.binding) throw new Error('tab_changed');
            if (config.route && page[routeKey] !== config.route) throw new Error('tab_changed');
          };
          const selected = new URL(page.url());
          if (config.route && page[routeKey] !== config.route) return {error_code:'tab_changed'};
          if (selected.origin !== config.origin && selected.origin !== config.loginOrigin &&
              !(selected.origin === config.landingOrigin && selected.pathname === '/')) return {error_code:'origin'};
        """ + ("page[key] = config.binding;\n" if bind else "if (page[key] !== config.binding) return {error_code:'tab_changed'};\n") + r"""
          page[messagesKey] = {routes:new Map(), url:page.url()};
          if (selected.origin !== config.origin) return {error_code:'login_required'};
          if (config.inbox) {
            const inbox = config.provider === 'gmail' && /^\/mail\/u\/\d+\/$/.test(selected.pathname)
              ? config.origin + selected.pathname + '#inbox' : config.url;
            const destination = config.provider === 'naver' ? config.origin + '/v2/folders/0/all' : inbox;
            if (page.url() !== destination) {
              guard();
              await page.goto(destination, {waitUntil:'domcontentloaded', timeout:15000});
              guard();
            }
            const current = new URL(page.url());
            if (config.provider === 'gmail' ? !/^\/mail\/u\/\d+\/$/.test(current.pathname) || current.hash !== '#inbox'
                : current.pathname !== '/v2/folders/0/all' || current.search || current.hash) return {error_code:'mail_list'};
          }
          if (config.provider === 'gmail') {
            try { await page.locator('tr.zA[role="row"]:visible').first().waitFor({state:'visible', timeout:5000}); } catch (_) {}
          } else {
            try { await page.locator('ul.mail_list:visible').first().waitFor({state:'visible', timeout:5000}); }
            catch (_) { return {error_code:'mail_list'}; }
            guard();
            const profile = page.locator('#gnb a.gnb_mail_address:visible');
            if (!(await profile.count())) {
              const toggle = page.locator('#gnb_my'), layer = page.locator('#gnb_my_layer');
              if ((await toggle.count()) !== 1 || (await layer.count()) !== 1) return {error_code:'account'};
              if ((await layer.getAttribute('class') || '').split(/\s+/).includes('gnb_lyr_opened')) return {error_code:'account'};
              closeProfile = true;
              await toggle.click({timeout:5000});
              await profile.first().waitFor({state:'visible', timeout:5000});
            }
          }
          guard();
          const data = await page.evaluate(async ({provider, limit, expectedFingerprint, origin, references}) => {
            if (location.origin !== origin) return {error_code:'origin'};
            const visible = e => !!e.getClientRects().length &&
              getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
            const text = e => e ? (e.innerText || '').trim().slice(0, 2000) : '';
        """ + (_GMAIL_DOM if provider == "gmail" else _NAVER_DOM) + r"""
            if (items.some(item => !item.sender || !item.subject || !item.date)) return {error_code:'mail_list'};
            const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(account));
            const fingerprint = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
            if (expectedFingerprint && fingerprint !== expectedFingerprint) return {error_code:'account_changed'};
            if (location.origin !== origin) return {error_code:'origin'};
            const messageRoutes = [];
            rows.slice(0, limit).forEach((row, index) => {
              let route = '';
              if (provider === 'gmail' && /^\/mail\/u\/\d+\/$/.test(location.pathname)) {
                const ids = [...new Set(Array.from(row.querySelectorAll('[data-legacy-thread-id]'))
                  .map(e => e.getAttribute('data-legacy-thread-id')))];
                if (ids.length === 1 && /^[a-f0-9]{16}$/.test(ids[0])) route = location.pathname + '#inbox/' + ids[0];
              } else if (provider === 'naver') {
                const href = row.querySelector('.mail_title_link')?.getAttribute('href') || '';
                if (/^\/v2\/popup\/read\/\d+\/\d+$/.test(href)) route = href;
              }
              if (route) {
                const ref = references + ':' + index;
                items[index].message_ref = ref;
                messageRoutes.push([ref, {route, subject:items[index].subject}]);
              }
            });
            await Promise.all(messageRoutes.map(async ([ref, message]) => {
              const identity = provider === 'gmail' ? message.route.split('/').pop() : message.route;
              const digest = await crypto.subtle.digest('SHA-256',
                new TextEncoder().encode(JSON.stringify([provider, fingerprint, identity])));
              items[Number(ref.split(':').pop())].message_key = Array.from(new Uint8Array(digest),
                b => b.toString(16).padStart(2, '0')).join('');
            }));
            return {account, fingerprint, items, messageRoutes, observed_count:rows.length};
          }, config);
          guard();
          if (data.error_code) return {error_code:data.error_code};
          const {messageRoutes, ...metadata} = data;
          page[messagesKey] = {routes:new Map(messageRoutes), url:page.url()};
          return {provider:config.provider, origin:config.origin, binding:config.binding,
                  scope:'current_view', ...metadata};
        } catch (_) { return {error_code:'mail_list'}; }
        finally {
          if (closeProfile) {
            try {
              if (new URL(page.url()).origin === 'https://mail.naver.com' &&
                  page[Symbol.for('anis.browser.mail.binding')] === mailBinding &&
                  (await page.locator('#gnb_my_layer').getAttribute('class') || '').split(/\s+/).includes('gnb_lyr_opened'))
                await page.locator('#gnb_my').click({timeout:5000});
            } catch (_) {}
          }
        } }
        """

    @staticmethod
    def _body_snippet(provider, message_ref, binding, fingerprint):
        config = json.dumps({"provider": provider, "origin": _ORIGINS[provider], "message_ref": message_ref,
                             "binding": binding, "expectedFingerprint": fingerprint, "bodyLimit": BODY_LIMIT})
        return "async (page) => { const config = " + config + ";\n" + r"""
          const detail = page, sourceUrl = page.url();
          let stage = 'source_account';
          const key = Symbol.for('anis.browser.mail.binding');
          const messagesKey = Symbol.for('anis.browser.mail.messages');
          const sourceMessages = page[messagesKey];
          const fail = code => {throw new Error(code);};
          const sourceGuard = (expectedUrl = sourceUrl) => {
            if (new URL(page.url()).origin !== config.origin) fail('origin');
            if (page[key] !== config.binding) fail('tab_changed');
            const messages = page[messagesKey];
            if (!messages || messages !== sourceMessages || messages.url !== sourceUrl ||
                page.url() !== expectedUrl || !messages.routes.has(config.message_ref)) fail('message_changed');
            return messages.routes.get(config.message_ref);
          };
          const accountOf = async target => {
            let closeProfile = false;
            try {
              if (new URL(target.url()).origin !== config.origin) fail('origin');
              if (config.provider === 'naver' && !(await target.locator('#gnb a.gnb_mail_address:visible').count())) {
                const toggle = target.locator('#gnb_my'), layer = target.locator('#gnb_my_layer');
                if ((await toggle.count()) !== 1 || (await layer.count()) !== 1) fail('account');
                if ((await layer.getAttribute('class') || '').split(/\s+/).includes('gnb_lyr_opened')) fail('account');
                closeProfile = true;
                await toggle.click({timeout:5000});
                await target.locator('#gnb a.gnb_mail_address:visible').first().waitFor({state:'visible', timeout:5000});
              }
              const data = await target.evaluate(async ({origin, expectedFingerprint}) => {
                if (location.origin !== origin) return {error_code:'origin'};
                const visible = e => !!e.getClientRects().length &&
                  getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
                const text = e => e ? (e.innerText || '').trim().slice(0, 2000) : '';
        """ + (_GMAIL_ACCOUNT_DOM if provider == "gmail" else _NAVER_ACCOUNT_DOM) + r"""
                const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(account));
                const fingerprint = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
                if (fingerprint !== expectedFingerprint) return {error_code:'account_changed'};
                return {account, fingerprint};
              }, config);
              if (data.error_code) fail(data.error_code);
              return data;
            } finally {
              if (closeProfile) {
                try {
                  if (new URL(target.url()).origin === config.origin &&
                      (await target.locator('#gnb_my_layer').getAttribute('class') || '').split(/\s+/).includes('gnb_lyr_opened'))
                    await target.locator('#gnb_my').click({timeout:5000});
                } catch (_) {}
              }
            }
          };
          try {
            const message = sourceGuard();
            let route = message.route, messageId = '';
            if (config.provider === 'gmail') {
              if (!/^\/mail\/u\/\d+\/#inbox\/[a-f0-9]{16}$/.test(route)) fail('message_changed');
              messageId = route.split('/').pop();
            } else {
              const match = route.match(/^\/v2\/popup\/read\/(\d+)\/(\d+)$/);
              if (!match) fail('message_changed');
              route = '/v2/read/' + match[1] + '/' + match[2];
              messageId = match[2];
            }
            await accountOf(page);
            sourceGuard();
            stage = 'open_mail';
            await detail.goto(config.origin + route, {waitUntil:'domcontentloaded', timeout:15000});
            const selector = config.provider === 'gmail' ? '.a3s.aiL:visible' : '.mail_view_inner.selected .mail_view_contents_inner:visible';
            stage = 'wait_body';
            await detail.locator(selector).first().waitFor({state:'visible', timeout:10000});
            const detailUrl = detail.url();
            const detailGuard = () => {
              sourceGuard(detailUrl);
              const u = new URL(detail.url());
              if (u.origin !== config.origin || detail.url() !== detailUrl) fail('message_changed');
              if (config.provider === 'gmail') {
                if (u.pathname !== route.split('#')[0] || !/^#inbox\/[a-zA-Z0-9_-]+$/.test(u.hash)) fail('message_changed');
              } else if (u.pathname !== route || u.search || u.hash) fail('message_changed');
            };
            detailGuard();
            stage = 'detail_account';
            const identity = await accountOf(detail);
            detailGuard();
            if (config.provider === 'gmail') {
              const heading = detail.locator('h2.hP:visible');
              if ((await heading.count()) !== 1 || (await heading.getAttribute('data-legacy-thread-id')) !== messageId)
                fail('message_changed');
              const expand = detail.getByRole('button', {name:/^(모두 펼치기|Expand all)$/});
              const count = await expand.count();
              if (count > 1) fail('message_body');
              if (count === 1) {
                stage = 'expand';
                await expand.click({timeout:5000});
                await detail.getByRole('button', {name:/^(모두 접기|Collapse all)$/})
                  .waitFor({state:'visible', timeout:10000});
                await detail.waitForFunction(() => {
                  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
                  const messages = Array.from(document.querySelectorAll('.adn.ads[data-message-id]')).filter(visible);
                  return messages.length > 0 && messages.every(e => Array.from(e.querySelectorAll('.a3s.aiL')).some(visible));
                }, null, {timeout:10000});
              }
              detailGuard();
            }
            stage = 'read_body';
            const data = await detail.evaluate(({provider, bodyLimit, messageId, subject}) => {
              const visible = e => !!e.getClientRects().length &&
                getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
              let bodies;
              if (provider === 'gmail') {
                const titles = Array.from(document.querySelectorAll('h2.hP')).filter(visible);
                if (titles.length !== 1 || titles[0].getAttribute('data-legacy-thread-id') !== messageId ||
                    ((titles[0].innerText || '').trim().slice(0, 2000) || '(제목 없음)') !== subject) return {error_code:'message_changed'};
                bodies = Array.from(document.querySelectorAll('.a3s.aiL')).filter(visible);
              } else {
                const views = Array.from(document.querySelectorAll('.mail_view_inner.selected')).filter(visible);
                if (views.length !== 1 || !views[0].querySelector('input.toggle_bookmark[id="bookmark-' + messageId + '"]'))
                  return {error_code:'message_changed'};
                bodies = Array.from(views[0].querySelectorAll('.mail_view_contents_inner')).filter(visible);
                if (bodies.length !== 1) return {error_code:'message_body'};
              }
              if (!bodies.length) return {error_code:'message_body'};
              const text = bodies.map(e => (e.innerText || '').trim()).join('\n\n──────────\n\n');
              return {body:text.slice(0, bodyLimit), truncated:text.length > bodyLimit};
            }, {...config, messageId, subject:message.subject});
            detailGuard();
            stage = 'verify_account';
            await accountOf(detail);
            detailGuard();
            if (data.error_code) fail(data.error_code);
            stage = 'restore_list';
            // Check and navigate in one browser task so user navigation wins.
            const restoring = await page.evaluate(({from, to}) => {
              if (location.href !== from) return false;
              location.assign(to);
              return true;
            }, {from:detailUrl, to:sourceUrl});
            if (!restoring) fail('message_changed');
            await page.waitForURL(sourceUrl, {waitUntil:'domcontentloaded', timeout:15000});
            const listSelector = config.provider === 'gmail' ? 'tr.zA[role="row"]:visible, td.TC:visible' : 'ul.mail_list:visible';
            await page.locator(listSelector).first().waitFor({state:'visible', timeout:10000});
            sourceGuard();
            await accountOf(page);
            sourceGuard();
            return {provider:config.provider, binding:config.binding, message_ref:config.message_ref, ...identity, ...data};
          } catch (error) {
            if (['origin','account','account_changed','tab_changed','message_changed'].includes(error.message))
              return {error_code:error.message};
            return {error_code:'message_body', stage};
          }
        }"""

    @staticmethod
    def _draft_snippet(provider, binding, fingerprint):
        config = json.dumps({"provider": provider, "origin": _ORIGINS[provider],
                             "binding": binding, "expectedFingerprint": fingerprint})
        return "async (page) => { const config = " + config + ";\n" + r"""
          const source = page.url(), key = Symbol.for('anis.browser.mail.binding');
          const fail = code => {throw new Error(code);};
          const guard = expected => {
            if (new URL(page.url()).origin !== config.origin) fail('origin');
            if (page[key] !== config.binding) fail('tab_changed');
            if (page.url() !== expected) fail('tab_changed');
          };
          const accountOf = async () => {
            let closeProfile = false;
            try {
              if (config.provider === 'naver' && !(await page.locator('#gnb a.gnb_mail_address:visible').count())) {
                const toggle = page.locator('#gnb_my'), layer = page.locator('#gnb_my_layer');
                if ((await toggle.count()) !== 1 || (await layer.count()) !== 1 ||
                    (await layer.getAttribute('class') || '').split(/\s+/).includes('gnb_lyr_opened')) fail('account');
                closeProfile = true;
                await toggle.click({timeout:5000});
                await page.locator('#gnb a.gnb_mail_address:visible').first().waitFor({state:'visible', timeout:5000});
              }
              const result = await page.evaluate(async ({origin, expectedFingerprint}) => {
                if (location.origin !== origin) return {error_code:'origin'};
                const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
                const text = e => (e?.innerText || '').trim();
        """ + (_GMAIL_ACCOUNT_DOM if provider == "gmail" else _NAVER_ACCOUNT_DOM) + r"""
                const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(account));
                const fingerprint = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2,'0')).join('');
                return fingerprint === expectedFingerprint ? {account, fingerprint} : {error_code:'account_changed'};
              }, config);
              if (result.error_code) fail(result.error_code);
              return result;
            } finally {
              if (closeProfile && new URL(page.url()).origin === config.origin && page[key] === config.binding &&
                  (await page.locator('#gnb_my_layer').getAttribute('class') || '').split(/\s+/).includes('gnb_lyr_opened'))
                await page.locator('#gnb_my').click({timeout:5000});
            }
          };
          try {
            guard(source);
            const original = new URL(source);
            if (config.provider === 'gmail' ? !/^\/mail\/u\/\d+\/$/.test(original.pathname) || original.hash !== '#inbox'
                : original.pathname !== '/v2/folders/0/all' || original.search || original.hash) fail('mail_list');
            await accountOf(); guard(source);
            let target;
            if (config.provider === 'gmail') {
              target = config.origin + original.pathname + '#drafts';
              guard(source);
              await page.goto(target, {waitUntil:'domcontentloaded', timeout:15000});
              await page.waitForURL(target, {waitUntil:'domcontentloaded', timeout:10000});
              await page.waitForFunction(() => {
                const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
                const rows = Array.from(document.querySelectorAll('tr.zA[role="row"]')).filter(visible);
                const empty = Array.from(document.querySelectorAll('td.TC')).filter(visible)
                  .some(e => /^(?:임시보관함에 (?:저장된 )?메일이 없습니다\.?|임시보관함이 비어 있습니다\.?|No drafts\.?|No conversations\.?)/i.test((e.innerText || '').trim()));
                return !rows.length && empty || rows.length > 0 && rows.every(row =>
                  /(?:^|\s)(?:초안|임시보관(?:함)?|Draft)(?:\s|$|,)/i.test((row.querySelector('.yW')?.innerText || '').trim()));
              }, null, {timeout:10000});
            } else {
              const folder = page.locator('a.mailbox_label.svg_temporary[title="임시보관함"]:visible');
              if ((await folder.count()) !== 1 || (await folder.innerText()).trim() !== '임시보관함') fail('draft_count');
              guard(source);
              await folder.click({timeout:5000});
              await page.waitForURL(url => url.origin === config.origin && /^\/v2\/folders\/\d+(?:\/all)?\/?$/.test(url.pathname)
                && url.href !== source && !url.search && !url.hash, {timeout:10000});
              target = page.url();
              await page.waitForFunction(() => Array.from(document.querySelectorAll('h2.mailbox_title .text'))
                .filter(e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .map(e => (e.innerText || '').trim()).join('') === '임시보관함', null, {timeout:10000});
            }
            await page.locator(config.provider === 'gmail' ? '[role="main"]:visible' : 'h2.mailbox_title:visible')
              .first().waitFor({state:'visible', timeout:10000});
            guard(target);
            const identity = await accountOf(); guard(target);
            const data = await page.evaluate(({provider}) => {
              const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
              if (provider === 'naver' && Array.from(document.querySelectorAll('h2.mailbox_title .text')).filter(visible)
                  .map(e => (e.innerText || '').trim()).join('') !== '임시보관함') return {error_code:'draft_count'};
              const values = [...new Set(Array.from(document.querySelectorAll(provider === 'gmail'
                ? '.Dj' : 'h2.mailbox_title a.total'))
                .filter(visible).map(e => (e.textContent || '').trim()))];
              const totals = [];
              const number = s => Number(s.replace(/,/g,''));
              for (const value of values) {
                for (const match of value.matchAll(/([\d,]+)\s*[–-]\s*([\d,]+)\s*(?:\/|of)\s*([\d,]+)/gi))
                  if (number(match[1]) > 0 && number(match[1]) <= number(match[2]) && number(match[2]) <= number(match[3])) totals.push(number(match[3]));
                for (const match of value.matchAll(/([\d,]+)\s*(?:개\s*)?중\s*([\d,]+)\s*[–-]\s*([\d,]+)/g))
                  if (number(match[2]) > 0 && number(match[2]) <= number(match[3]) && number(match[3]) <= number(match[1])) totals.push(number(match[1]));
                if (provider === 'naver') {
                  const match = value.match(/^(?:전체\s*메일|전체|총)\s*([\d,]+)\s*(?:통|개|건)?$/);
                  if (match) totals.push(number(match[1]));
                }
              }
              const unique = [...new Set(totals)];
              if (unique.length === 1 && Number.isSafeInteger(unique[0]) && unique[0] >= 0 && unique[0] <= 10000000)
                return {count:unique[0]};
              const rows = Array.from(document.querySelectorAll(provider === 'gmail' ? 'tr.zA[role="row"]' : 'li.mail_item')).filter(visible);
              const empty = Array.from(document.querySelectorAll(provider === 'gmail' ? 'td.TC' : '.mail_list_empty, .empty_mail, .mail_empty'))
                .filter(visible).some(e => /^(?:임시보관함에 (?:저장된 )?메일이 없습니다\.?|임시보관함이 비어 있습니다\.?|메일이 없습니다\.?|No drafts\.?|No conversations\.?)/i.test((e.innerText || '').trim()));
              return !rows.length && empty ? {count:0} : {error_code:'draft_count'};
            }, config);
            guard(target); await accountOf(); guard(target);
            const restoring = await page.evaluate(({from,to}) => {
              if (location.href !== from) return false;
              location.assign(to); return true;
            }, {from:target,to:source});
            if (!restoring) fail('tab_changed');
            await page.waitForURL(source, {waitUntil:'domcontentloaded', timeout:15000});
            await page.locator(config.provider === 'gmail' ? 'tr.zA[role="row"]:visible, td.TC:visible' : 'ul.mail_list:visible')
              .first().waitFor({state:'visible', timeout:10000});
            guard(source); await accountOf(); guard(source);
            if (data.error_code) fail(data.error_code);
            return {provider:config.provider,binding:config.binding,...identity,...data};
          } catch (error) {
            return {error_code:['origin','account','account_changed','tab_changed','mail_list'].includes(error.message)
              ? error.message : 'draft_count'};
          }
        }"""

    def draft_count(self, provider, *, checkpoint=check_turn_cancelled):
        provider = self._provider_name(provider)
        if not self._lock.acquire(blocking=False):
            raise BrowserMailError("이전 메일 조회가 진행 중입니다.")
        try:
            checkpoint()
            with self._state_lock:
                if self._provider != provider or not self._binding:
                    raise BrowserMailError(_ERRORS["mail_list"])
                revision, binding, fingerprint = self._revision, self._binding, self._fingerprint

            def check():
                checkpoint()
                with self._state_lock:
                    if revision != self._revision:
                        raise BrowserMailError(_ERRORS["tab_changed"])

            result = self.session.call(self._draft_snippet(provider, binding, fingerprint), checkpoint=check, timeout=90)
            check()
            if isinstance(result, dict) and result.get("error_code") in _ERRORS:
                raise BrowserMailError(_ERRORS[result["error_code"]])
            if (not isinstance(result, dict) or result.get("provider") != provider or result.get("binding") != binding
                    or not isinstance(result.get("account"), str)
                    or hashlib.sha256(result["account"].encode()).hexdigest() != fingerprint
                    or result.get("fingerprint") != fingerprint or type(result.get("count")) is not int
                    or not 0 <= result["count"] <= 10000000):
                raise BrowserMailError(_ERRORS["draft_count"])
            return result["count"]
        except ToolCancelledError:
            self.disconnect()
            raise
        except (BrowserMailError, BrowserExtensionUnavailableError):
            raise
        except Exception:
            raise BrowserMailError(_ERRORS["draft_count"]) from None
        finally:
            self._lock.release()

    def _reuse_login(self, provider, binding, checkpoint, *, route=""):
        """Use the existing saved-login flow; a mail-list DOM proves the result."""
        checkpoint()
        url = _LOGIN_SITES[provider]
        screen = self.login_service.inspect(url, checkpoint=checkpoint,
                                            expected_mail_binding=binding, expected_mail_route=route)
        for step in range(4):
            checkpoint()
            if screen.get("origin") == _ORIGINS[provider]:
                return
            state = screen.get("state")
            if state == "user_action_required":
                raise BrowserMailError(_ERRORS[state])
            if state == "authenticated" and screen.get("origin") == _LANDINGS[provider]:
                # One fixed navigation after the login service verified the landing screen.
                code = "async (page) => { try { const config = " + json.dumps({
                    "binding": binding, "origin": _LANDINGS[provider], "url": MAIL_SITES[provider]}) + r""";
                  const key = Symbol.for('anis.browser.mail.binding');
                  const u = new URL(page.url());
                  if (page[key] !== config.binding || u.origin !== config.origin || u.pathname !== '/') return {error_code:'origin'};
                  await page.goto(config.url, {waitUntil:'domcontentloaded', timeout:15000});
                  if (page[key] !== config.binding) return {error_code:'tab_changed'};
                  return {opened:true};
                } catch (_) { return {error_code:'origin'}; } }"""
                result = self.session.call(code, checkpoint=checkpoint, timeout=90)
                checkpoint()
                if result.get("opened") is not True:
                    raise BrowserMailError(_ERRORS["origin"])
                return
            if state != "login_required":
                raise BrowserMailError(_ERRORS["user_action_required"])
            if step == 3:
                raise BrowserMailError(_ERRORS["user_action_required"])
            if not self.login_service.account_status(url).get("configured"):
                raise BrowserMailError(_ERRORS["login_required"])
            before = (screen.get("origin"), screen.get("username_field"), screen.get("password_field"))
            screen = self.login_service.fill_saved(url, submit=True, checkpoint=checkpoint)
            after = (screen.get("origin"), screen.get("username_field"), screen.get("password_field"))
            if screen.get("state") == "login_required" and before == after:
                raise BrowserMailError("저장된 계정의 로그인을 확인하지 못했습니다. Chrome에서 로그인 정보를 확인하세요.")

    @staticmethod
    def _validate(result, provider, limit, binding, expected_fingerprint):
        if isinstance(result, dict) and result.get("error_code") in _ERRORS:
            raise BrowserMailError(_ERRORS[result["error_code"]])
        if (not isinstance(result, dict) or result.get("provider") != provider
                or result.get("origin") != _ORIGINS[provider] or result.get("binding") != binding
                or result.get("scope") != "current_view"
                or not isinstance(result.get("account"), str) or not 1 <= len(result["account"]) <= 320
                or not re.fullmatch(r"[^\s@]+@[^\s@]+", result["account"])
                or any(ord(c) < 32 or ord(c) == 127 for c in result["account"])
                or not isinstance(result.get("items"), list) or len(result["items"]) > limit
                or type(result.get("observed_count")) is not int
                or not len(result["items"]) <= result["observed_count"] <= 10000):
            raise BrowserMailError("메일 목록 조회 결과를 확인하지 못했습니다.")
        fingerprint = hashlib.sha256(result["account"].encode()).hexdigest()
        if fingerprint != result.get("fingerprint") or (expected_fingerprint and fingerprint != expected_fingerprint):
            raise BrowserMailError(_ERRORS["account_changed"])
        items = []
        for item in result["items"]:
            if (not isinstance(item, dict) or type(item.get("unread")) is not bool
                    or not all(isinstance(item.get(k), str) and len(item[k]) <= 2000 for k in ("sender", "subject", "date"))):
                raise BrowserMailError("메일 목록 조회 결과를 확인하지 못했습니다.")
            items.append({k: item[k] for k in ("sender", "subject", "date", "unread")})
            if "message_ref" in item:
                if (not isinstance(item["message_ref"], str)
                        or not re.fullmatch(r"[a-f0-9-]{36}:[0-9]{1,2}", item["message_ref"])
                        or any(previous.get("message_ref") == item["message_ref"] for previous in items[:-1])):
                    raise BrowserMailError("메일 목록 조회 결과를 확인하지 못했습니다.")
                items[-1]["message_ref"] = item["message_ref"]
            if "message_key" in item:
                if ("message_ref" not in item or not isinstance(item["message_key"], str)
                        or not re.fullmatch(r"[a-f0-9]{64}", item["message_key"])
                        or any(previous.get("message_key") == item["message_key"] for previous in items[:-1])):
                    raise BrowserMailError("메일 목록 조회 결과를 확인하지 못했습니다.")
                items[-1]["message_key"] = item["message_key"]
        return {"provider": provider, "account": result["account"], "items": items,
                "scope": "current_view", "observed_count": result["observed_count"]}

    def list_inbox(self, provider, limit=50, *, checkpoint=check_turn_cancelled):
        """Automatic workflows reuse Chrome and saved login, then verify the inbox."""
        provider = self._provider_name(provider)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise BrowserMailError("메일 조회 개수는 1~50 사이의 정수여야 합니다.")
        checkpoint()
        if not self._automatic():
            raise BrowserMailError("메일 자동 연결 설정에서 Chrome 연결 토큰을 먼저 등록하세요.")
        if not any((process.info.get("name") or "").casefold() in {"chrome.exe", "chrome", "google-chrome"}
                   for process in psutil.process_iter(["name"])):
            open_chrome(MAIL_SITES[provider])
        checkpoint()
        return self.list_current(provider, limit, checkpoint=checkpoint, _inbox=True)

    def list_current(self, provider, limit=50, *, checkpoint=check_turn_cancelled, _inbox=False):
        provider = self._provider_name(provider)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise BrowserMailError("메일 조회 개수는 1~50 사이의 정수여야 합니다.")
        if not self._lock.acquire(blocking=False):
            raise BrowserMailError("이전 메일 조회가 진행 중입니다.")
        try:
            checkpoint()
            automatic = self._automatic()
            with self._state_lock:
                previous_provider = self._provider
            if previous_provider and previous_provider != provider:
                self.switch_provider()
            with self._state_lock:
                revision, binding, fingerprint = self._revision, self._binding, self._fingerprint
                self._messages.clear()
                if automatic:
                    fingerprint = self._fingerprints.get(provider, "")
            bind = automatic or not binding
            binding = str(uuid4()) if automatic else binding or str(uuid4())
            def check():
                checkpoint()
                with self._state_lock:
                    if revision != self._revision:
                        raise BrowserMailError("메일 연결이 해제되어 이전 조회 결과를 폐기했습니다.")
            route = self._select_provider_tab(provider, check) if automatic else ""
            result = self.session.call(self._snippet(provider, limit, binding, fingerprint, bind=bind, route=route, inbox=_inbox),
                                       checkpoint=check, timeout=CONNECT_TIMEOUT if bind and not automatic else 90)
            check()
            if isinstance(result, dict) and result.get("error_code") == "login_required":
                self._reuse_login(provider, binding, check, route=route)
                result = self.session.call(self._snippet(provider, limit, binding, fingerprint, bind=False, route=route, inbox=_inbox),
                                           checkpoint=check, timeout=90)
                check()
            data = self._validate(result, provider, limit, binding, fingerprint)
            with self._state_lock:
                if revision != self._revision:
                    raise BrowserMailError("메일 연결이 해제되어 이전 조회 결과를 폐기했습니다.")
                self._provider, self._binding, self._fingerprint = provider, binding, result["fingerprint"]
                self._messages = {item["message_ref"]: dict(item) for item in data["items"] if "message_ref" in item}
                if automatic:
                    self._fingerprints[provider] = result["fingerprint"]
            return data
        except ToolCancelledError:
            self.disconnect()
            raise
        except (BrowserMailError, BrowserExtensionUnavailableError):
            self._disconnect(reset_accounts=False)
            raise
        except Exception:
            self._disconnect(reset_accounts=False)
            raise BrowserMailError("메일 조회를 확인하지 못했습니다. Chrome의 계정·메일 목록 탭을 확인한 뒤 다시 연결하세요.") from None
        except BaseException:
            self._disconnect(reset_accounts=False)
            raise
        finally:
            self._lock.release()

    def read_message(self, provider, message_ref, *, checkpoint=check_turn_cancelled):
        provider = self._provider_name(provider)
        if not isinstance(message_ref, str) or not re.fullmatch(r"[a-f0-9-]{36}:[0-9]{1,2}", message_ref):
            raise BrowserMailError(_ERRORS["message_changed"])
        if not self._lock.acquire(blocking=False):
            raise BrowserMailError("이전 메일 조회가 진행 중입니다.")
        try:
            checkpoint()
            with self._state_lock:
                if provider != self._provider or message_ref not in self._messages or not self._binding:
                    raise BrowserMailError(_ERRORS["message_changed"])
                revision, binding, fingerprint = self._revision, self._binding, self._fingerprint
                item = dict(self._messages[message_ref])

            def check():
                checkpoint()
                with self._state_lock:
                    if revision != self._revision or message_ref not in self._messages:
                        raise BrowserMailError("메일 연결이 해제되어 이전 본문 조회 결과를 폐기했습니다.")

            result = self.session.call(self._body_snippet(provider, message_ref, binding, fingerprint),
                                       checkpoint=check, timeout=90)
            check()
            if isinstance(result, dict) and result.get("error_code") in _ERRORS:
                message = _ERRORS[result["error_code"]]
                stage = result.get("stage")
                if result["error_code"] == "message_body" and isinstance(stage, str) and stage in _BODY_STAGES:
                    message += f" ({_BODY_STAGES[stage]} 단계)"
                raise BrowserMailError(message)
            if (not isinstance(result, dict) or result.get("provider") != provider
                    or result.get("binding") != binding or result.get("message_ref") != message_ref
                    or not isinstance(result.get("account"), str)
                    or hashlib.sha256(result["account"].encode()).hexdigest() != fingerprint
                    or result.get("fingerprint") != fingerprint
                    or not isinstance(result.get("body"), str) or len(result["body"]) > BODY_LIMIT
                    or type(result.get("truncated")) is not bool):
                raise BrowserMailError(_ERRORS["message_body"])
            return {"provider": provider, "account": result["account"], "message_ref": message_ref,
                    **{key: item[key] for key in ("subject", "sender", "date")},
                    **({"message_key": item["message_key"]} if "message_key" in item else {}),
                    "body": result["body"], "truncated": result["truncated"]}
        except ToolCancelledError:
            self.disconnect()
            raise
        except (BrowserMailError, BrowserExtensionUnavailableError):
            self._disconnect(reset_accounts=False)
            raise
        except Exception:
            self._disconnect(reset_accounts=False)
            raise BrowserMailError(_ERRORS["message_body"]) from None
        except BaseException:
            self._disconnect(reset_accounts=False)
            raise
        finally:
            self._lock.release()


_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def get_browser_mail_service():
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BrowserMailService()
        return _SERVICE
