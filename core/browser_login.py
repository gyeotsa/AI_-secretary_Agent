"""One-step login on an explicitly selected, existing Chrome tab.

Credentials come only from the local dialog, never a model tool argument.
Chrome owns persistence; this adapter never reads cookies or field values.
"""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from core.browser_extension import BrowserExtensionSession, CONNECT_TIMEOUT
from core.turn_context import check_turn_cancelled


SITES = {
    "naver": {"name": "네이버", "url": "https://nid.naver.com/nidlogin.login"},
    "google": {"name": "구글", "url": "https://accounts.google.com/"},
    "kakao": {"name": "카카오", "url": "https://accounts.kakao.com/login"},
    "instagram": {"name": "인스타그램", "url": "https://www.instagram.com/accounts/login/"},
    "daangn": {"name": "당근", "url": "https://www.daangn.com/"},
}


def validate_login_url(url, *, resolve=True):
    """Use the existing public-address gate, with HTTPS for credentials."""
    from plugins.browser import BrowserPlugin
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "\\" in url
                or any(ord(c) < 33 or ord(c) == 127 for c in url)):
            raise ValueError()
        login_origin(url)  # Includes port/IDNA validation; no DNS for local settings.
        if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith(".localhost"):
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if resolve:
            BrowserPlugin._validate_url(url)
        return url
    except (ValueError, TypeError, OSError):
        raise ValueError("공개 HTTPS 로그인 주소를 입력하세요. 계정 정보가 들어간 주소는 사용할 수 없습니다.") from None


def login_origin(url):
    parsed = urlsplit(url)
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = "[" + host + "]"
    return "https://" + host + (":" + str(parsed.port) if parsed.port not in (None, 443) else "")


def open_chrome(url):
    url = validate_login_url(url)
    # Normal Chrome, with its current profile and password manager.
    candidates = []
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                    candidates.append(Path(winreg.QueryValue(key, None)))
            except OSError:
                pass
    candidates.extend(Path(os.environ[root]) / suffix for root, suffix in (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ) if os.environ.get(root))
    chrome = next((path for path in candidates if path.is_file()), None)
    if chrome is None:
        raise RuntimeError("Chrome을 찾지 못했습니다. Chrome 설치 상태를 확인하세요.")
    subprocess.Popen([str(chrome), "--new-tab", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"state": "opened", "origin": login_origin(url)}


# ponytail: visible, unambiguous HTML login controls only; add site adapters for custom/iframe forms.
_CONTROLS = r"""
  const user = page.locator('input:is([autocomplete="username"],[autocomplete="email"],[type="email"],[name="username"],[name="email"],[name="loginId"],[name="id"],[id="id"],[id="identifierId"]):visible');
  const pass = page.locator('input[type="password"]:visible');
  const submit = page.getByRole('button', {name:/^(로그인|로그인하기|로그인 하기|다음|계속|Sign in|Log in|Login|Next|Continue)$/i}).filter({visible:true});
  const logout = page.getByRole('link', {name:/^(로그아웃|로그아웃하기|Sign out|Log out|Logout)$/i})
    .or(page.getByRole('button', {name:/^(로그아웃|로그아웃하기|Sign out|Log out|Logout)$/i})).filter({visible:true});
  const blocked = page.locator('iframe[src*="recaptcha"]:visible,iframe[src*="hcaptcha"]:visible,input[autocomplete="one-time-code"]:visible,input[name*="captcha" i]:visible,input[id*="captcha" i]:visible');
  const summary = async () => {
    const currentOrigin = new URL(page.url()).origin;
    const username_field = (await user.count()) === 1, password_field = (await pass.count()) === 1;
    const challenge = (await blocked.count()) > 0;
    const trustedOrigin = currentOrigin === origin || readOnlyRedirect();
    const googleMain = origin === 'https://accounts.google.com' && currentOrigin === 'https://myaccount.google.com';
    let googleSignedIn = false;
    if (googleMain && trustedOrigin) {
      const account = page.getByRole('button', {name:/^Google 계정:/}).filter({visible:true});
      if ((await account.count()) === 1) {
        const label = await account.getAttribute('aria-label');
        const email = label?.match(/\(([^()\r\n]+)\)\s*$/)?.[1]?.trim();
        googleSignedIn = !!email && /^[^\s@]+@[^\s@]+$/.test(email) &&
          (!expectedUsername || email.toLowerCase() === expectedUsername.toLowerCase());
      }
    }
    const authenticated = trustedOrigin && !challenge && !(await user.count()) && !(await pass.count()) &&
      (googleMain ? googleSignedIn : (await logout.count()) > 0);
    const state = !trustedOrigin ? 'unknown' : challenge ? 'user_action_required' : authenticated ? 'authenticated' :
      ((await user.count()) || (await pass.count()) ? 'login_required' : 'unknown');
    const hasSubmit = (await submit.count()) === 1;
    if (new URL(page.url()).origin !== currentOrigin) throw new Error('navigation');
    return {origin:currentOrigin, state, username_field, password_field, submit:hasSubmit};
  };
"""


class BrowserLoginService:
    def __init__(self, session=None, vault=None):
        self.session = session or BrowserExtensionSession()
        self._vault = vault
        self._origin = self._binding = ""
        self._screen = {}
        self._revision = 0
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()

    @property
    def vault(self):
        if self._vault is None:
            from core.remote_runtime import SecureTokenVault
            self._vault = SecureTokenVault()
        return self._vault

    def _account(self, origin):
        try:
            record = self.vault.load("anis-browser-login", origin)
            if record is None:
                return None
            if (not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1
                    or set(record) != {"version", "username", "password"}
                    or not isinstance(record["username"], str) or not record["username"].strip()
                    or len(record["username"]) > 320 or not isinstance(record["password"], str)
                    or not 1 <= len(record["password"]) <= 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in record["username"] + record["password"])):
                raise ValueError()
            return record
        except Exception:
            raise ValueError("암호화된 웹 로그인 정보를 확인하지 못했습니다. 계정을 다시 저장하세요.") from None

    def account_status(self, url):
        origin = login_origin(validate_login_url(url, resolve=False))
        record = self._account(origin)
        return {"configured": record is not None, "username": record["username"] if record else "", "origin": origin}

    def save_account(self, url, username, password):
        origin = login_origin(validate_login_url(url, resolve=False))
        if (not isinstance(username, str) or not username.strip() or len(username) > 320
                or not isinstance(password, str) or not 1 <= len(password) <= 4096
                or any(ord(c) < 32 or ord(c) == 127 for c in username + password)):
            raise ValueError("아이디와 비밀번호를 모두 입력하세요. 제어 문자는 사용할 수 없습니다.")
        try:
            self.vault.save("anis-browser-login", origin,
                            {"version": 1, "username": username.strip(), "password": password})
        except Exception:
            raise ValueError("웹 로그인 정보를 Windows 암호화 저장소에 저장하지 못했습니다.") from None
        return {"configured": True, "username": username.strip(), "origin": origin}

    def delete_account(self, url):
        origin = login_origin(validate_login_url(url, resolve=False))
        try:
            self.vault.delete("anis-browser-login", origin)
        except Exception:
            raise ValueError("저장된 웹 로그인 정보를 삭제하지 못했습니다.") from None
        return {"configured": False, "username": "", "origin": origin}

    def fill_saved(self, url, submit=False, *, checkpoint=check_turn_cancelled):
        origin = login_origin(validate_login_url(url))
        with self._state_lock:
            if self._origin != origin or not self._binding:
                raise ValueError("저장한 계정과 같은 주소의 Chrome 로그인 탭을 먼저 연결하세요.")
            screen = dict(self._screen)
        record = self._account(origin)
        if record is None:
            raise ValueError("이 사이트의 아이디와 비밀번호를 먼저 암호화 저장하세요.")
        try:
            # A two-step password screen must display the saved username.
            return self.fill(record["username"], record["password"] if screen.get("password_field") else "",
                             submit, url=url, checkpoint=checkpoint)
        finally:
            record.clear()

    def open(self, url):
        if not self._lock.acquire(blocking=False):
            raise ValueError("이전 로그인 작업이 진행 중입니다.")
        try:
            self.disconnect()
            return open_chrome(url)
        finally:
            self._lock.release()

    @staticmethod
    def _validate(result):
        if (not isinstance(result, dict) or result.get("state") not in {
                "authenticated", "login_required", "user_action_required", "unknown"}
                or not isinstance(result.get("origin"), str)
                or any(type(result.get(k)) is not bool for k in ("username_field", "password_field", "submit"))):
            raise ValueError("로그인 화면 상태를 확인하지 못했습니다. Chrome에서 직접 확인하세요.")
        # Whitelist the result: no page text, field values, account IDs, or URLs.
        return {k: result[k] for k in ("origin", "state", "username_field", "password_field", "submit")}

    def _call(self, body, origin, binding, revision, checkpoint, *, bind=False,
              expected_mail_binding="", expected_mail_route=""):
        expected_username = self.account_status(SITES["google"]["url"])["username"] if origin == "https://accounts.google.com" else ""
        code = "async (page) => { try {\n" + f"const origin={json.dumps(origin)}, binding={json.dumps(binding)}, expectedUsername={json.dumps(expected_username)};\n" + f"const expectedMailBinding={json.dumps(expected_mail_binding)}, expectedMailRoute={json.dumps(expected_mail_route)};\n" + r"""
          const key = Symbol.for('anis.browser.login.binding');
          const mailGuard = () => {
            if ((expectedMailBinding && page[Symbol.for('anis.browser.mail.binding')] !== expectedMailBinding) ||
                (expectedMailRoute && page[Symbol.for('anis.browser.mail.route')] !== expectedMailRoute))
              throw new Error('mail_tab_changed');
          };
          mailGuard();
          const readOnlyRedirect = () => {
            const current = new URL(page.url());
            return current.pathname === '/' && (
              (origin === 'https://nid.naver.com' && current.origin === 'https://www.naver.com') ||
              (origin === 'https://accounts.google.com' && current.origin === 'https://myaccount.google.com'));
          };
          const guard = () => {
            mailGuard();
            if (new URL(page.url()).origin !== origin || page[key] !== binding)
              throw new Error('tab_or_origin_changed');
          };
        """ + (r"""
          const selected = new URL(page.url());
          if (selected.origin !== origin && !readOnlyRedirect()) throw new Error('origin');
          page[key]=binding;
        """ if bind else "guard();\n") + _CONTROLS + body + r"""
        } catch (_) { return {error_code:'login_screen'}; } }
        """
        try:
            result = self.session.call(code, checkpoint=checkpoint, timeout=CONNECT_TIMEOUT if bind else 90)
            checkpoint()
            result = self._validate(result)
            with self._state_lock:
                if self._revision != revision:
                    raise ValueError("로그인 연결이 취소되어 이전 결과를 폐기했습니다.")
                if result["origin"] != origin:
                    self._origin = self._binding = ""
                self._screen = dict(result)
            return result
        except BaseException:
            with self._state_lock:
                if self._revision == revision:
                    self._origin = self._binding = ""
            raise

    def inspect(self, url, *, checkpoint=check_turn_cancelled,
                expected_mail_binding="", expected_mail_route=""):
        if any(not isinstance(value, str) or len(value) > 128
               for value in (expected_mail_binding, expected_mail_route)):
            raise ValueError("Chrome 메일 탭 연결 정보를 확인하지 못했습니다.")
        url = validate_login_url(url)
        origin = login_origin(url)
        if not self._lock.acquire(blocking=False):
            raise ValueError("이전 로그인 작업이 진행 중입니다.")
        try:
            checkpoint()
            with self._state_lock:
                revision = self._revision
                binding = self._binding if self._origin == origin else ""
            bind = not binding
            binding = binding or str(uuid4())
            body = r"""
              const current = new URL(page.url());
              if (current.origin === origin) guard();
              else if (page[key] !== binding || !readOnlyRedirect()) throw new Error('origin');
              return await summary();
            """ if bind else "guard(); return await summary();"
            result = self._call(body, origin, binding, revision, checkpoint, bind=bind,
                                expected_mail_binding=expected_mail_binding, expected_mail_route=expected_mail_route)
            with self._state_lock:
                if self._revision != revision:
                    raise ValueError("로그인 연결이 해제되었습니다.")
                if result["origin"] == origin:
                    self._origin, self._binding = origin, binding
            return result
        finally:
            self._lock.release()

    def fill(self, username="", password="", submit=False, *, url, checkpoint=check_turn_cancelled):
        if (not isinstance(username, str) or not isinstance(password, str) or type(submit) is not bool
                or len(username) > 320 or len(password) > 4096):
            raise ValueError("아이디 또는 비밀번호 입력 형식이 올바르지 않습니다.")
        expected_origin = login_origin(validate_login_url(url, resolve=False))
        if not self._lock.acquire(blocking=False):
            raise ValueError("이전 로그인 작업이 진행 중입니다.")
        try:
            checkpoint()
            with self._state_lock:
                origin, binding, revision = self._origin, self._binding, self._revision
            if not binding or origin != expected_origin:
                raise ValueError("선택한 주소의 Chrome 로그인 탭을 먼저 연결하세요.")
            body = f"const username={json.dumps(username)}, password={json.dumps(password)}, doSubmit={json.dumps(submit)};\n" + r"""
              // Validate every requested field before changing either one.
              const passwordStage = username && password && !(await user.count()) &&
                (await page.getByText(username, {exact:true}).filter({visible:true}).count()) === 1;
              if ((await blocked.count()) || (username && (await user.count()) !== 1 && !passwordStage)
                  || (password && (await pass.count()) !== 1)
                  || (doSubmit && (await submit.count()) !== 1)) throw new Error('ambiguous_or_challenge');
              if (!username && !password && !(await user.count()) && !(await pass.count()))
                throw new Error('no_login_form');
              const fields = [];
              if (username && !passwordStage) fields.push(user);
              if (password) fields.push(pass);
              if (!fields.length) fields.push((await user.count()) === 1 ? user : pass);
              const button = doSubmit ? await submit.elementHandle() : null;
              for (const field of fields) {
                const safe = await field.evaluate((el, button) => {
                  const buttonForm = button && ('form' in button ? button.form : button.closest('form'));
                  const target = new URL(button?.getAttribute('formaction') || el.form?.action || location.href, location.href);
                  return target.origin === location.origin && target.protocol === 'https:' &&
                    (!button || (!el.form && !buttonForm) || el.form === buttonForm);
                }, button);
                if (!safe) throw new Error('form_destination');
              }
              if (username && !passwordStage) { guard(); await user.fill(username, {timeout:5000}); }
              if (password) { guard(); await pass.fill(password, {timeout:5000}); }
              if (doSubmit) {
                const before = await summary();
                guard();
                await submit.click({timeout:5000});
                const deadline = Date.now() + 10000;
                let observed = null;
                do {
                  if (page[key] !== binding) throw new Error('tab_changed');
                  try { observed = await summary(); } catch (_) { observed = null; }
                  if (page[key] !== binding) throw new Error('tab_changed');
                  if (observed && (
                      (observed.origin !== origin && !readOnlyRedirect()) ||
                      observed.state === 'authenticated' || observed.state === 'user_action_required' ||
                      (observed.state === 'login_required' && (observed.username_field !== before.username_field || observed.password_field !== before.password_field))))
                    return observed;
                  await new Promise(resolve => setTimeout(resolve, 100));
                } while (Date.now() < deadline);
                if (page[key] !== binding) throw new Error('tab_changed');
                if (observed) return observed;
                throw new Error('screen_not_settled');
              }
              if (page[Symbol.for('anis.browser.login.binding')] !== binding) throw new Error('tab_changed');
              return await summary();
            """
            return self._call(body, origin, binding, revision, checkpoint)
        finally:
            self._lock.release()

    def disconnect(self):
        with self._state_lock:
            self._revision += 1
            self._origin = self._binding = ""
            self._screen = {}
        self.session.close()
