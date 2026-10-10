"""Chrome login contracts; only synthetic data and isolated browser contexts."""
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest

import core.browser_login as login
from core.intent_router import IntentRouter
from core.plugin import PluginRegistry, ToolCancelledError
from core.tool_result import ToolRunStatus
from plugins.browser import BrowserPlugin
from plugins.browser_login import BrowserLoginPlugin


class Vault:
    def __init__(self):
        self.records = {}

    def save(self, provider, account, record):
        self.records[provider, account] = dict(record)

    def load(self, provider, account):
        record = self.records.get((provider, account))
        return dict(record) if record is not None else None

    def delete(self, provider, account):
        return self.records.pop((provider, account), None) is not None


def screen(origin="https://nid.naver.com", **changes):
    return {"origin": origin, "state": "login_required", "username_field": True,
            "password_field": True, "submit": True, **changes}


@pytest.fixture(autouse=True)
def offline_urls(monkeypatch):
    monkeypatch.setattr(BrowserPlugin, "_validate_url", lambda url: url)


def test_account_reuses_lazy_dpapi_vault_and_isolates_canonical_origin(monkeypatch):
    vault, created = Vault(), []
    monkeypatch.setattr("core.remote_runtime.SecureTokenVault", lambda: created.append(True) or vault)
    service = login.BrowserLoginService(Mock())
    assert not created
    result = service.save_account("https://NID.NAVER.COM:443/login", " owner ", "fixture-password")
    assert created == [True]
    assert result == {"configured": True, "username": "owner", "origin": "https://nid.naver.com"}
    assert vault.records["anis-browser-login", "https://nid.naver.com"]["password"] == "fixture-password"
    assert service.account_status("https://nid.naver.com/another?query=secret") == result
    for other in ("https://www.naver.com/", "https://nid.naver.com.evil.example/", "https://nid.naver.com:8443/"):
        assert service.account_status(other)["configured"] is False
    service.save_account("https://accounts.google.com/", "google-owner", "other-password")
    assert service.delete_account("https://nid.naver.com/")["configured"] is False
    assert service.account_status("https://accounts.google.com/")["configured"] is True
    assert "password" not in json.dumps(result)


@pytest.mark.parametrize("url", [
    "http://example.com/login", "https://owner:secret@example.com/", "https://example.com\\@evil.example/",
    "https://example.com:99999/", "https://example.com/a\n", "https://example.com/a b",
    "https://localhost/", "https://service.localhost/", "https://127.0.0.1/", "https://[::1]/",
])
def test_invalid_url_cannot_store_credentials(url):
    vault = Vault()
    service = login.BrowserLoginService(Mock(), vault)
    with pytest.raises(ValueError):
        service.save_account(url, "owner", "fixture-password")
    assert not vault.records


@pytest.mark.parametrize("username,password", [("", "password"), ("owner", ""), ("owner\n", "password"), ("owner", "pw\0")])
def test_invalid_credentials_never_reach_vault(username, password):
    vault = Vault()
    service = login.BrowserLoginService(Mock(), vault)
    with pytest.raises(ValueError):
        service.save_account(login.SITES["naver"]["url"], username, password)
    assert not vault.records


@pytest.mark.parametrize("changes", [{"extra": "secret"}, {"version": True}, {"username": "owner\n"}, {"password": "fixture-password\0"}])
def test_corrupt_account_fails_closed_without_leaking_payload(changes):
    vault = Mock()
    vault.load.return_value = {"version": 1, "username": "owner", "password": "fixture-password", **changes}
    service = login.BrowserLoginService(Mock(), vault)
    with pytest.raises(ValueError) as error:
        service.account_status(login.SITES["naver"]["url"])
    assert "fixture-password" not in str(error.value) and "extra" not in str(error.value)


def test_bind_reuse_fill_guards_and_results_never_return_secrets():
    session = Mock()
    session.call.return_value = screen(password="fixture-password", value="private-field", text="private-page")
    service = login.BrowserLoginService(session, Vault())
    first = service.inspect(login.SITES["naver"]["url"])
    binding = service._binding
    assert first == screen()
    service.inspect(login.SITES["naver"]["url"])
    result = service.fill("owner", "fixture-password", True, url=login.SITES["naver"]["url"])
    codes = [call.args[0] for call in session.call.call_args_list]
    assert "page[key]=binding;" in codes[0]
    assert "page[key]=binding;" not in codes[1] + codes[2]
    assert service._binding == binding
    assert "new URL(page.url()).origin !== origin || page[key] !== binding" in codes[2]
    assert "guard(); await user.fill" in codes[2] and "guard(); await pass.fill" in codes[2]
    assert "guard();\n                await submit.click" in codes[2]
    assert result == screen()
    assert "fixture-password" not in json.dumps(result)


def test_real_isolated_chrome_role_button_preserves_form_and_destination_guards():
    node = shutil.which("node")
    playwright = Path(__file__).parent / "data/tools/playwright-mcp/node_modules/playwright"
    if not node or not playwright.is_dir():
        pytest.skip("Installed Chrome extension test runtime requires Node.js and Playwright")
    origin = "https://login.example.test"
    session = Mock()
    session.call.return_value = screen(origin=origin)
    service = login.BrowserLoginService(session, Vault())
    service.inspect(origin)
    inspect_code = session.call.call_args.args[0]
    service.fill("qa-user", "qa-password", True, url=origin)
    fill_code = session.call.call_args.args[0]
    guarded_service = login.BrowserLoginService(session, Vault())
    guarded_service.inspect(origin, expected_mail_binding="fixture-mail-binding", expected_mail_route="fixture-mail-route")
    guarded_code = session.call.call_args.args[0]
    script = f"const {{chromium}}=require({json.dumps(str(playwright))});\n"
    script += f"const inspect=({inspect_code}), fill=({fill_code}), guardedInspect=({guarded_code}), origin={json.dumps(origin)};\n"
    script += r"""
      const assert = require('node:assert/strict');
      const fields = '<input name="email" autocomplete="username webauthn"><input name="pass" type="password">';
      const done = "window.fixtureFilled=document.querySelector('[name=email]').value==='qa-user'&&document.querySelector('[name=pass]').value==='qa-password';document.querySelector('form')?.remove();document.querySelector('input')?.remove();document.querySelector('input')?.remove();document.body.insertAdjacentHTML('beforeend','<button>로그아웃</button>')";
      const div = `<div role="button" onclick="${done}">로그인</div>`;
      const native = `<button type="button" onclick="${done}">로그인</button>`;
      const cases = [
        [`<form id="login_form">${fields}${div}</form>`, true],
        [`<form id="login_form">${fields}</form>${div}`, false],
        [`<form id="login_form">${fields}</form><form id="other">${div}</form>`, false],
        [`<form action="https://other.example.test/collect">${fields}${div}</form>`, false],
        [`<form>${fields}${div.replace('role="button"','role="button" formaction="https://other.example.test/collect"')}</form>`, false],
        [`<form id="login_form">${fields}${native.replace('type="button"','type="button" form="missing"')}</form>`, false],
        [`<form id="login_form">${fields}${native}</form>`, true],
        [`${fields}${native}`, true],
      ];
      (async () => {
        const browser = await chromium.launch({channel:'chrome',headless:true});
        try {
          const context = await browser.newContext();
          let html;
          await context.route('**/*', route => route.fulfill({contentType:'text/html; charset=utf-8',body:html}));
          const page = await context.newPage();
          for (const [index, [fixture, allowed]] of cases.entries()) {
            html = fixture;
            await page.goto(origin);
            assert.equal((await inspect(page)).state, 'login_required', `inspect ${index}`);
            const result = await fill(page);
            if (allowed) {
              assert.equal(result.state, 'authenticated', `allowed ${index}`);
              assert.equal(await page.evaluate(() => window.fixtureFilled), true, `filled ${index}`);
            }
            else {
              assert.equal(result.error_code, 'login_screen', `denied ${index}`);
              assert.equal(await page.locator('input[name=email]').inputValue(), '', `username untouched ${index}`);
              assert.equal(await page.locator('input[name=pass]').inputValue(), '', `password untouched ${index}`);
            }
          }
          html=cases[0][0];
          await page.goto(origin);
          const beforeBinding=page[Symbol.for('anis.browser.login.binding')];
          page[Symbol.for('anis.browser.mail.binding')]='another-mail-page';
          page[Symbol.for('anis.browser.mail.route')]='fixture-mail-route';
          assert.equal((await guardedInspect(page)).error_code,'login_screen');
          assert.equal(page[Symbol.for('anis.browser.login.binding')],beforeBinding);
          page[Symbol.for('anis.browser.mail.binding')]='fixture-mail-binding';
          page[Symbol.for('anis.browser.mail.route')]='another-mail-route';
          assert.equal((await guardedInspect(page)).error_code,'login_screen');
          assert.equal(page[Symbol.for('anis.browser.login.binding')],beforeBinding);
          page[Symbol.for('anis.browser.mail.route')]='fixture-mail-route';
          assert.equal((await guardedInspect(page)).state,'login_required');
          assert.equal(await page.locator('input[name=email]').inputValue(),'');
          assert.equal(await page.locator('input[name=pass]').inputValue(),'');
          console.log(JSON.stringify({checked:cases.length}));
        } finally { await browser.close(); }
      })().catch(error => { console.error(error.stack); process.exitCode=1; });
    """
    result = subprocess.run([node, "-"], input=script, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"checked": 8}


def test_delayed_post_submit_redirect_is_observed_without_replaying_click():
    node = shutil.which("node")
    if not node:
        pytest.skip("Chrome extension runtime requires Node.js")
    session = Mock()
    session.call.return_value = screen()
    service = login.BrowserLoginService(session, Vault())
    service.inspect(login.SITES["naver"]["url"])
    service.fill("owner", "fixture-password", True, url=login.SITES["naver"]["url"])
    code = session.call.call_args.args[0]
    binding = service._binding
    service.disconnect()
    session.call.return_value = screen(origin="https://www.naver.com", state="authenticated",
                                       username_field=False, password_field=False, submit=False)
    service.inspect(login.SITES["naver"]["url"])
    inspect_code = session.call.call_args.args[0]
    with pytest.raises(ValueError, match="먼저 연결"):
        service.fill("owner", "fixture-password", True, url=login.SITES["naver"]["url"])
    script = r"""
      let moved = false, clicks = 0;
      function control(kind) {
        return {
          count: async () => kind === 'blocked' ? 0 : kind === 'logout' ? Number(moved) : Number(!moved),
          filter() { return this; }, or() { return this; },
          fill: async () => {}, evaluate: async () => true, elementHandle: async () => ({}),
          click: async () => { clicks++; setTimeout(() => { moved = true; }, 200); },
        };
      }
      const page = {
        url: () => moved ? 'https://www.naver.com/' : 'https://nid.naver.com/nidlogin.login',
        locator: selector => control(selector.startsWith('input:is') ? 'user' : selector.startsWith('input[type="password"]') ? 'pass' : 'blocked'),
        getByRole: (role, options) => control(options.name.test('Logout') ? 'logout' : 'submit'),
      };
    """
    script += f"page[Symbol.for('anis.browser.login.binding')]={json.dumps(binding)};\n"
    script += f"({code})(page).then(async result => {{const inspected=await ({inspect_code})(page);console.log(JSON.stringify({{result, inspected, clicks}}));}});"
    result = subprocess.run([node, "-"], input=script, text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    authenticated = screen(origin="https://www.naver.com", state="authenticated",
                           username_field=False, password_field=False, submit=False)
    assert observed == {"result": authenticated, "inspected": authenticated, "clicks": 1}


@pytest.mark.parametrize("url,label,count,state", [
    ("https://myaccount.google.com/?pli=1", "Google 계정: QA \n(qa@example.test)", 1, "authenticated"),
    ("https://myaccount.google.com/", "Google 계정: Other (other@example.test)", 1, "unknown"),
    ("https://myaccount.google.com/", "Google 계정: QA (qa@example.test)", 2, "unknown"),
    ("https://myaccount.google.com/security", "Google 계정: QA (qa@example.test)", 1, None),
    ("https://myaccount.google.com.evil.example/", "Google 계정: QA (qa@example.test)", 1, None),
])
def test_google_redirect_checks_visible_saved_account_without_allowing_credentials(url, label, count, state):
    node = shutil.which("node")
    if not node:
        pytest.skip("Chrome extension runtime requires Node.js")
    setup = f"const url={json.dumps(url)}, label={json.dumps(label)}, accountCount={count};\n" + r"""
      function control(count) {
        return {count:async()=>count,filter(){return this;},or(){return this;},getAttribute:async()=>label};
      }
      const page = {
        url:()=>url, locator:()=>control(0),
        getByRole:(role, options)=>control(options.name.test('Google 계정: QA') ? accountCount : options.name.test('Logout') ? 1 : 0),
      };
    """

    def execute(code, **_kwargs):
        assert "fixture-password" not in code
        script = setup + f"({code})(page).then(result=>console.log(JSON.stringify(result)));"
        result = subprocess.run([node, "-"], input=script, text=True, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    session = Mock()
    session.call.side_effect = execute
    service = login.BrowserLoginService(session, Vault())
    service.save_account(login.SITES["google"]["url"], "qa@example.test", "fixture-password")
    if state is None:
        with pytest.raises(ValueError):
            service.inspect(login.SITES["google"]["url"])
    else:
        assert service.inspect(login.SITES["google"]["url"]) == screen(
            origin="https://myaccount.google.com", state=state,
            username_field=False, password_field=False, submit=False)
    with pytest.raises(ValueError, match="먼저 연결"):
        service.fill("qa@example.test", "fixture-password", True, url=login.SITES["google"]["url"])
    assert session.call.call_count == 1 and not service._binding


def test_saved_fill_requires_same_bound_origin_and_handles_two_step_screen():
    session = Mock()
    session.call.return_value = screen(password_field=False)
    vault = Vault()
    service = login.BrowserLoginService(session, vault)
    service.save_account(login.SITES["naver"]["url"], "owner", "fixture-password")
    with pytest.raises(ValueError, match="먼저 연결"):
        service.fill_saved(login.SITES["naver"]["url"])
    assert not session.call.called
    service.inspect(login.SITES["naver"]["url"])
    with pytest.raises(ValueError):
        service.fill_saved(login.SITES["google"]["url"])
    assert session.call.call_count == 1
    service.fill_saved(login.SITES["naver"]["url"], True)
    assert 'password=""' in session.call.call_args.args[0]
    assert "fixture-password" not in session.call.call_args.args[0]
    session.call.return_value = screen(username_field=False)
    service.inspect(login.SITES["naver"]["url"])
    result = service.fill_saved(login.SITES["naver"]["url"], True)
    code = session.call.call_args.args[0]
    assert 'username="owner", password="fixture-password"' in code
    assert "page.getByText(username, {exact:true})" in code
    assert result == screen(username_field=False)
    assert vault.load("anis-browser-login", "https://nid.naver.com")["password"] == "fixture-password"


@pytest.mark.parametrize("response", [{"error_code": "login_screen"}, screen(origin="https://evil.example", state="unknown")])
def test_invalid_or_changed_origin_revokes_binding_before_next_input(response):
    session = Mock()
    session.call.return_value = screen()
    service = login.BrowserLoginService(session, Vault())
    service.inspect(login.SITES["naver"]["url"])
    session.call.return_value = response
    if "error_code" in response:
        with pytest.raises(ValueError):
            service.fill("owner", "fixture-password", url=login.SITES["naver"]["url"])
    else:
        assert service.fill("owner", "fixture-password", url=login.SITES["naver"]["url"])["state"] == "unknown"
    count = session.call.call_count
    with pytest.raises(ValueError, match="먼저 연결"):
        service.fill("owner", "fixture-password", url=login.SITES["naver"]["url"])
    assert session.call.call_count == count


def test_disconnect_discards_inflight_result_and_cancellation_clears_binding():
    session = Mock()
    service = login.BrowserLoginService(session, Vault())

    def stale_result(*_args, **_kwargs):
        service.disconnect()
        return screen()

    session.call.side_effect = stale_result
    with pytest.raises(ValueError, match="취소"):
        service.inspect(login.SITES["naver"]["url"])
    assert not service._binding and not service._screen and session.close.call_count == 1
    session.call.side_effect = None
    session.call.return_value = screen()
    service.inspect(login.SITES["naver"]["url"])
    checks = []

    def cancelled_after_call():
        checks.append(True)
        if len(checks) == 2:
            raise ToolCancelledError("fixture-cancelled")

    with pytest.raises(ToolCancelledError):
        service.fill("owner", "fixture-password", url=login.SITES["naver"]["url"], checkpoint=cancelled_after_call)
    assert not service._binding


@pytest.mark.parametrize("saved", [False, True])
def test_other_dialog_rebinding_cannot_send_old_origin_credentials(saved):
    session, vault = Mock(), Vault()
    session.call.return_value = screen()
    service = login.BrowserLoginService(session, vault)
    url = login.SITES["naver"]["url"]
    service.save_account(url, "owner", "fixture-password")
    service.inspect(url)

    def rebind():
        session.call.return_value = screen(origin="https://accounts.google.com")
        service.inspect(login.SITES["google"]["url"])

    if saved:
        original_load = vault.load

        def load_after_other_dialog_inspects(provider, origin):
            vault.load = original_load
            rebind()
            return original_load(provider, origin)

        vault.load = load_after_other_dialog_inspects
        operation = lambda: service.fill_saved(url, True)
    else:
        rebind()
        operation = lambda: service.fill("owner", "fixture-password", True, url=url)
    with pytest.raises(ValueError):
        operation()
    assert session.call.call_count == 2
    assert all("fixture-password" not in call.args[0] for call in session.call.call_args_list)


@pytest.mark.parametrize("utterance,url", [
    ("네이버 로그인해줘", login.SITES["naver"]["url"]),
    ('"https://example.com/signin?next=%2Fhome" 로그인해줘', "https://example.com/signin?next=%2Fhome"),
])
def test_natural_login_intent_and_open_do_not_claim_authentication(monkeypatch, utterance, url):
    plugin = BrowserLoginPlugin()
    registry = PluginRegistry()
    registry.register_plugin(BrowserPlugin())
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve(utterance)
    assert resolution.ready and resolution.tool_name == "browser_login_open"
    assert resolution.slots == {"url": url}
    plugin.service = Mock()
    plugin.service.open.return_value = {"state": "opened", "origin": login.login_origin(url)}
    bridge = Mock()
    bridge.available.return_value = True
    monkeypatch.setattr("plugins.browser_login.get_interface_control_bridge", lambda: bridge)
    result = plugin.execute_tool(resolution.tool_name, resolution.slots)
    assert result.status == ToolRunStatus.UNVERIFIED
    assert "성공은 아직 확인하지 않았습니다" in result.raw_output
    plugin.service.open.assert_called_once_with(url)
    bridge.call.assert_called_once_with("open_surface", "browser_login:" + url)
    rejected = plugin.execute_tool("browser_login_open", {"url": url, "password": "fixture-password"})
    assert rejected.status == ToolRunStatus.FAILED
    assert "fixture-password" not in str(rejected)


@pytest.mark.parametrize("utterance", ["네이버와 구글 로그인해줘", "https://one.example/ https://two.example/ 로그인해줘"])
def test_multiple_login_targets_require_selection_and_clear_stale_target(utterance):
    plugin = BrowserLoginPlugin()
    assert not plugin.extract_slots("web.login", utterance, {"url": login.SITES["naver"]["url"]}).get("url")
    registry = PluginRegistry()
    registry.register_plugin(BrowserPlugin())
    registry.register_plugin(plugin)
    resolution = IntentRouter(registry).resolve(utterance)
    assert resolution.matched and not resolution.ready
