"""Mail-list contracts using synthetic data and an isolated offline Chrome page."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core.browser_mail import BrowserMailError, BrowserMailService
from core.plugin import ToolCancelledError


ACCOUNT = "fixture-reader@example.test"
ITEM = {"sender": "Fixture sender", "subject": "Synthetic subject", "date": "2026-10-08", "unread": True}


def credentials(configured=False):
    result = Mock()
    result.status.return_value = {"configured": configured}
    result.environment.return_value = {}
    return result


def response(code, *, account=ACCOUNT, **changes):
    config = json.loads(re.search(r"const config = (\{[^\n]+\});", code).group(1))
    return {"provider": config["provider"], "origin": config["origin"], "binding": config["binding"],
            "scope": "current_view", "account": account,
            "fingerprint": hashlib.sha256(account.encode()).hexdigest(),
            "items": [dict(ITEM)], "observed_count": 3, **changes}


def service_with_response(**changes):
    session = Mock()
    session.call.side_effect = lambda code, **kwargs: response(code, **changes)
    return BrowserMailService(session, credentials()), session


@pytest.mark.parametrize("provider", ["gmail", "naver"])
def test_mail_result_contains_only_current_view_metadata_and_reuses_binding(provider):
    service, session = service_with_response(password="fixture-private-password", body="fixture-private-body",
                                             total_count=99999, new_count=999)
    try:
        result = service.list_current(provider, limit=2)
        assert result == {"provider": provider, "account": ACCOUNT, "items": [ITEM],
                          "scope": "current_view", "observed_count": 3}
        binding = service._binding
        service.list_current(provider, limit=2)
        first, second = [call.args[0] for call in session.call.call_args_list]
        assert "page[key] = config.binding;" in first
        assert "page[key] = config.binding;" not in second
        assert service._binding == binding
        assert "fixture-private" not in json.dumps(result)
    finally:
        service.disconnect()


@pytest.mark.parametrize("args", [{"provider": "outlook"}, {"provider": True},
                                  {"provider": "gmail", "limit": True},
                                  {"provider": "gmail", "limit": 0},
                                  {"provider": "gmail", "limit": 51}])
def test_invalid_request_never_calls_browser(args):
    service, session = service_with_response()
    with pytest.raises(BrowserMailError):
        service.list_current(**args)
    session.call.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"scope": "all_mail"}, {"origin": "https://mail.google.com.evil.example"},
    {"binding": "other-tab"}, {"fingerprint": "other-account"},
    {"observed_count": True}, {"observed_count": 0}, {"observed_count": 10001},
    {"items": [{**ITEM, "unread": "false"}]},
    {"items": [{**ITEM, "subject": "x" * 2001}]},
])
def test_invalid_browser_contract_fails_closed_and_discards_binding(changes):
    service, session = service_with_response(**changes)
    with pytest.raises(BrowserMailError):
        service.list_current("gmail")
    assert not service._binding and not service._fingerprint
    assert session.close.call_count == 1


def test_stable_mail_key_is_preserved_for_collection_across_list_refreshes():
    item = {**ITEM, "message_ref": "12345678-1234-1234-1234-123456789abc:0", "message_key": "a" * 64}
    service, _ = service_with_response(items=[item])
    try:
        assert service.list_current("gmail")["items"][0]["message_key"] == "a" * 64
        assert service.list_current("gmail")["items"][0]["message_key"] == "a" * 64
    finally:
        service.disconnect()


@pytest.mark.parametrize("key", ["private-route", "A" * 64, 17])
def test_invalid_stable_mail_key_fails_closed(key):
    service, session = service_with_response(items=[{**ITEM,
        "message_ref": "12345678-1234-1234-1234-123456789abc:0", "message_key": key}])
    with pytest.raises(BrowserMailError):
        service.list_current("gmail")
    assert not service._messages and session.close.called


def test_account_change_during_refresh_requires_explicit_reconnection():
    service, session = service_with_response()
    service.list_current("gmail")
    session.call.side_effect = lambda code, **kwargs: response(code, account="other-fixture@example.test")
    with pytest.raises(BrowserMailError, match="계정"):
        service.list_current("gmail")
    assert not service._binding and not service._fingerprint


def test_disconnection_and_cancellation_cannot_publish_inflight_result():
    service, session = service_with_response()

    def stale(code, **kwargs):
        service.disconnect()
        return response(code)

    session.call.side_effect = stale
    with pytest.raises(BrowserMailError):
        service.list_current("gmail")
    assert not service._binding
    session.call.side_effect = lambda code, **kwargs: response(code)
    checks = []

    def cancel_after_browser():
        checks.append(True)
        if len(checks) == 2:
            raise ToolCancelledError("synthetic cancellation")

    with pytest.raises(ToolCancelledError):
        service.list_current("gmail", checkpoint=cancel_after_browser)
    assert not service._binding


def test_arbitrary_browser_error_never_exposes_page_text_or_credentials():
    session = Mock()
    session.call.side_effect = RuntimeError("fixture-secret-token and private page text")
    service = BrowserMailService(session, credentials())
    with pytest.raises(BrowserMailError) as error:
        service.list_current("gmail")
    assert "fixture-secret" not in str(error.value) and "private page" not in str(error.value)
    assert not service._binding


def test_open_uses_only_fixed_provider_url_without_claiming_authentication(monkeypatch):
    service, session = service_with_response()
    opened = []
    monkeypatch.setattr("core.browser_mail.open_chrome", lambda url: opened.append(url) or {"opened": True})
    assert service.open("gmail") == {"opened": True}
    assert opened == ["https://mail.google.com/mail/u/0/#inbox"]
    session.call.assert_not_called()
    assert not service._fingerprint


@pytest.mark.parametrize("final_mail_error", [False, True])
def test_saved_login_is_reused_but_only_final_mail_list_proves_success(final_mail_error):
    service, session = service_with_response()
    calls = []

    def browser(code, **kwargs):
        calls.append(code)
        if len(calls) == 1:
            return {"error_code": "login_required"}
        assert not service._fingerprint
        return {"error_code": "mail_list"} if final_mail_error else response(code)

    session.call.side_effect = browser
    login = Mock()
    login.inspect.return_value = {"state": "login_required", "origin": "https://accounts.google.com",
                                 "username_field": True, "password_field": False}
    login.account_status.return_value = {"configured": True}
    login.fill_saved.side_effect = [
        {"state": "login_required", "origin": "https://accounts.google.com",
         "username_field": False, "password_field": True},
        {"state": "unknown", "origin": "https://mail.google.com"},
    ]
    service.login_service = login
    if final_mail_error:
        with pytest.raises(BrowserMailError):
            service.list_current("gmail")
        assert not service._fingerprint and login.disconnect.called
    else:
        assert service.list_current("gmail")["items"] == [ITEM]
        assert not login.disconnect.called
    assert len(calls) == 2
    login.inspect.assert_called_once()
    assert login.inspect.call_args.args == ("https://accounts.google.com/",)
    assert login.fill_saved.call_count == 2
    assert login.fill_saved.call_args.kwargs["submit"] is True


@pytest.mark.parametrize("blocked", ["extra_auth", "unconfigured", "same_form"])
def test_saved_login_stops_on_required_user_action_without_repeated_submission(blocked):
    service, session = service_with_response()
    session.call.return_value = {"error_code": "login_required"}
    session.call.side_effect = None
    login = Mock()
    screen = {"state": "user_action_required" if blocked == "extra_auth" else "login_required",
              "origin": "https://accounts.google.com", "username_field": True, "password_field": True}
    login.inspect.return_value = screen
    login.account_status.return_value = {"configured": blocked != "unconfigured"}
    login.fill_saved.return_value = screen
    service.login_service = login
    with pytest.raises(BrowserMailError):
        service.list_current("gmail")
    assert login.fill_saved.call_count == (1 if blocked == "same_form" else 0)
    assert session.call.call_count == 1 and not service._fingerprint
    login.disconnect.assert_called_once()


def test_verified_login_landing_navigates_once_to_fixed_mail_url():
    service, session = service_with_response()
    calls = []

    def browser(code, **kwargs):
        calls.append(code)
        if len(calls) == 1:
            return {"error_code": "login_required"}
        return {"opened": True} if "await page.goto(config.url" in code else response(code)

    session.call.side_effect = browser
    login = Mock()
    login.inspect.return_value = {"state": "authenticated", "origin": "https://myaccount.google.com"}
    service.login_service = login
    assert service.list_current("gmail")["scope"] == "current_view"
    assert len(calls) == 3 and sum("await page.goto(config.url" in code for code in calls) == 1
    assert '"url": "https://mail.google.com/mail/u/0/#inbox"' in calls[1]
    login.fill_saved.assert_not_called()


def automatic_service(tabs=None):
    session = Mock()
    service = BrowserMailService(session, credentials(True))
    tabs = tabs if tabs is not None else {"gmail": [(1, "mail")], "naver": [(3, "mail")]}
    accounts, created = {}, []

    def browser(code, **kwargs):
        config = json.loads(re.search(r"const config = (\{[^\n]+\});", code).group(1))
        if "nonce" in config:
            provider = "gmail" if config["origin"] == "https://mail.google.com" else "naver"
            if "const created = await context.newPage()" in code:
                created.append(provider)
                tabs[provider] = [(5, "mail")]
            return {"tabs": [{"index": index, "kind": kind, "route": f"{config['nonce']}:{index}"}
                             for index, kind in tabs.get(provider, [])]}
        assert config["route"].endswith(f":{session.select_tab.call_args.args[0]}")
        return response(code, account=accounts.get(config["provider"], ACCOUNT))

    session.call.side_effect = browser
    session.select_tab.side_effect = lambda index, **kwargs: {"selected": True, "index": index}
    return service, session, accounts, created


def test_owned_mail_session_uses_only_credential_environment_and_mail_client_name(monkeypatch):
    configured = credentials(True)
    factory = Mock()
    monkeypatch.setattr("core.browser_mail.BrowserExtensionSession", factory)
    service = BrowserMailService(connection_credentials=configured)
    factory.assert_called_once_with(environment=configured.environment, client_name="ANIS Mail")
    configured.environment.assert_not_called()
    service.disconnect()


def test_automatic_switch_and_back_select_provider_tabs_without_reapproval_or_account_reset():
    service, session, _, created = automatic_service()
    service.list_current("gmail")
    gmail_fingerprint = service._fingerprints["gmail"]
    service.switch_provider()
    service.list_current("naver")
    service.list_current("gmail")
    assert [call.args[0] for call in session.select_tab.call_args_list] == [1, 3, 1]
    assert service._fingerprints["gmail"] == gmail_fingerprint
    assert not created and not session.close.called
    assert all(call.kwargs["timeout"] == 90 for call in session.call.call_args_list)
    service.disconnect()
    assert not service._fingerprints and session.close.call_count == 1


def test_manual_provider_switch_keeps_explicit_selected_tab_contract():
    service, session = service_with_response()
    service.list_current("gmail")
    service.switch_provider()
    service.list_current("naver")
    session.select_tab.assert_not_called()
    assert session.close.call_count == 1
    assert all('"route": ""' in call.args[0] for call in session.call.call_args_list)


@pytest.mark.parametrize("tabs", [[(1, "mail"), (2, "mail")], [(1, "login"), (2, "login")]])
def test_automatic_ambiguous_tabs_fail_without_opening_or_reading_pages(tabs):
    service, session, _, created = automatic_service({"gmail": tabs})
    with pytest.raises(BrowserMailError, match="여러 개"):
        service.list_current("gmail")
    assert session.call.call_count == 1 and not created
    session.select_tab.assert_not_called()
    assert not service._fingerprint


def test_automatic_mailbox_is_preferred_over_unrelated_provider_login_screens():
    service, session, _, created = automatic_service({"gmail": [(1, "login"), (2, "mail"), (3, "login")]})
    assert service.list_current("gmail")["items"] == [ITEM]
    assert session.select_tab.call_args.args == (2,) and not created


def test_automatic_missing_mailbox_creates_one_fixed_provider_page_only():
    service, session, _, created = automatic_service({})
    service.list_current("naver")
    service.list_current("naver")
    assert created == ["naver"]
    creation_code = next(call.args[0] for call in session.call.call_args_list
                         if "const created = await context.newPage()" in call.args[0])
    assert '"url": "https://mail.naver.com/"' in creation_code
    assert "await created.goto(config.url" in creation_code and "await page.goto" not in creation_code


def test_automatic_account_guard_survives_switch_and_failed_retry_until_explicit_disconnect():
    service, session, accounts, _ = automatic_service()
    service.list_current("gmail")
    service.list_current("naver")
    accounts["gmail"] = "other-fixture@example.test"
    for _ in range(2):
        with pytest.raises(BrowserMailError, match="계정"):
            service.list_current("gmail")
        assert service._fingerprints["gmail"] == hashlib.sha256(ACCOUNT.encode()).hexdigest()
    service.disconnect()
    assert service.list_current("gmail")["account"] == accounts["gmail"]


def test_automatic_replacement_page_rebinds_only_after_routing_and_retains_account_guard():
    tabs = {"gmail": [(1, "mail")]}
    service, session, _, _ = automatic_service(tabs)
    service.list_current("gmail")
    first_binding = service._binding
    tabs["gmail"] = [(8, "mail")]
    service.list_current("gmail")
    assert service._binding != first_binding
    assert session.select_tab.call_args.args == (8,)
    code = session.call.call_args.args[0]
    assert "page[key] = config.binding;" in code
    assert hashlib.sha256(ACCOUNT.encode()).hexdigest() in code


def test_automatic_login_inspection_requires_selected_mail_page_before_any_saved_input():
    service, session, _, _ = automatic_service({"naver": [(2, "login")]})
    inventory = session.call.side_effect

    def needs_login(code, **kwargs):
        return inventory(code, **kwargs) if '"nonce"' in code else {"error_code": "login_required"}

    session.call.side_effect = needs_login
    login = Mock()
    login.inspect.side_effect = ValueError("synthetic changed page")
    service.login_service = login
    with pytest.raises(BrowserMailError):
        service.list_current("naver")
    mail_code = session.call.call_args.args[0]
    config = json.loads(re.search(r"const config = (\{[^\n]+\});", mail_code).group(1))
    assert login.inspect.call_args.kwargs["expected_mail_binding"] == config["binding"]
    assert login.inspect.call_args.kwargs["expected_mail_route"] == config["route"]
    login.fill_saved.assert_not_called()


@pytest.mark.parametrize("kind", ["disconnect", "cancel"])
def test_automatic_select_cancellation_or_disconnect_never_reads_or_publishes_mail(kind):
    service, session, _, _ = automatic_service()

    def selected(index, **kwargs):
        if kind == "cancel":
            raise ToolCancelledError("synthetic cancellation")
        service.disconnect()
        return {"selected": True, "index": index}

    session.select_tab.side_effect = selected
    with pytest.raises(ToolCancelledError if kind == "cancel" else BrowserMailError):
        service.list_current("gmail")
    assert session.call.call_count == 1 and not service._fingerprint
    assert session.close.called


@pytest.mark.parametrize("tab", [
    {"index": True, "kind": "mail", "route": "invalid"},
    {"index": -1, "kind": "mail", "route": "invalid"},
    {"index": 0, "kind": "unrelated", "route": "invalid"},
    {"index": 0, "kind": "mail", "route": "invalid"},
])
def test_automatic_routing_result_validation_fails_closed(tab):
    service, session, _, created = automatic_service()
    session.call.side_effect = None
    session.call.return_value = {"tabs": [tab]}
    with pytest.raises(BrowserMailError):
        service.list_current("gmail")
    session.select_tab.assert_not_called()
    assert not created and not service._fingerprint


def test_real_automatic_tab_routing_is_readonly_allowlisted_and_guards_stale_index():
    node = shutil.which("node")
    playwright = Path(__file__).parent / "data/tools/playwright-mcp/node_modules/playwright"
    if not node or not playwright.is_dir():
        pytest.skip("Installed Node.js and Playwright are required for isolated routing verification")
    inventory = BrowserMailService._tabs_snippet("gmail", "fixture-route")
    naver_create = BrowserMailService._tabs_snippet("naver", "fixture-created", create=True)
    read = BrowserMailService._snippet("gmail", 1, "fixture-binding", "", bind=True, route="fixture-route:1")
    script = f"const {{chromium}}=require({json.dumps(str(playwright))});\n"
    script += f"const inventory=({inventory}), createNaver=({naver_create}), read=({read});\n"
    script += r"""
      const assert=require('node:assert/strict');
      (async()=>{
        const browser=await chromium.launch({channel:'chrome',headless:true});
        try {
          const context=await browser.newContext();
          const html='<header><a aria-label="Google Account: Fixture (fixture-reader@example.test)">Profile</a></header><table><tr class="zA zE" role="row"><td class="yW">Fixture sender</td><td class="bog">Synthetic subject</td><td class="xW">2026-10-08</td></tr></table>';
          await context.route('**/*',route=>route.fulfill({contentType:'text/html; charset=utf-8',body:html}));
          const urls=[
            'https://unrelated.example/?private=fixture-secret',
            'https://mail.google.com/mail/u/0/#inbox',
            'https://mail.naver.com/v2/folders/0/all',
            'https://mail.google.com.evil.example/',
            'https://accounts.google.com/v3/signin/identifier',
            'https://nid.naver.com/unrelated',
          ];
          const pages=[];
          for(const url of urls){const page=await context.newPage();await page.goto(url);pages.push(page);}
          const result=await inventory(pages[0]);
          assert.deepEqual(result,{tabs:[
            {index:1,kind:'mail',route:'fixture-route:1'},
            {index:4,kind:'login',route:'fixture-route:4'},
          ]});
          assert.deepEqual(context.pages().map(p=>p.url()),urls);
          assert.ok(!JSON.stringify(result).includes('private')&&!JSON.stringify(result).includes('fixture-secret'));
          assert.equal((await read(pages[1])).account,'fixture-reader@example.test');
          assert.equal((await read(pages[4])).error_code,'tab_changed');
          await pages[1].close();
          const replacement=await context.newPage();await replacement.goto(urls[1]);
          assert.equal((await read(replacement)).error_code,'tab_changed');
          const before=context.pages().length;
          assert.equal((await createNaver(pages[0])).error_code,'tab_changed');
          assert.equal(context.pages().length,before);
          await pages[2].close();
          const created=await createNaver(pages[0]);
          assert.equal(created.tabs.length,1);
          const fresh=context.pages()[created.tabs[0].index];
          assert.equal(fresh.url(),'https://mail.naver.com/');
          assert.equal(fresh[Symbol.for('anis.browser.mail.route')],created.tabs[0].route);
          assert.equal((await createNaver(pages[0])).error_code,'tab_changed');
          assert.equal(context.pages().length,before);
          assert.equal(pages[0].url(),urls[0]);
          console.log(JSON.stringify({verified:true}));
        } finally {await browser.close();}
      })().catch(error=>{console.error(error.stack);process.exitCode=1;});
    """
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8", capture_output=True, timeout=40)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"verified": True}


def test_real_dom_extractor_is_bounded_readonly_and_guards_origin_tab_and_account():
    node = shutil.which("node")
    playwright = Path(__file__).parent / "data/tools/playwright-mcp/node_modules/playwright"
    if not node or not playwright.is_dir():
        pytest.skip("Installed Node.js and Playwright are required for isolated DOM verification")
    binding = "synthetic-mail-tab"
    fingerprint = hashlib.sha256(ACCOUNT.encode()).hexdigest()
    first = BrowserMailService._snippet("gmail", 2, binding, "", bind=True)
    refresh = BrowserMailService._snippet("gmail", 2, binding, fingerprint, bind=False)
    naver_first = BrowserMailService._snippet("naver", 2, binding, "", bind=True)
    naver_refresh = BrowserMailService._snippet("naver", 2, binding, fingerprint, bind=False)
    script = f"const {{chromium}}=require({json.dumps(str(playwright))});\n"
    script += f"const first=({first}), refresh=({refresh});\n"
    script += f"const naverFirst=({naver_first}), naverRefresh=({naver_refresh});\n"
    script += r"""
      const assert = require('node:assert/strict');
      const email = 'fixture-reader@example.test';
      const header = `<header><a aria-label="Google Account: Fixture (${email})">Account</a></header>`;
      const row = (i, hidden=false) => `<tr role="row" class="zA ${i===1?'zE':''}" ${hidden?'style="display:none"':''}><td class="yW">Sender ${i}</td><td><span class="bog">Subject ${i}</span></td><td class="xW"><span title="2026-10-08 ${i}:00">today</span></td></tr>`;
      let html = header + `<main role="main"><table>${row(1)}${row(2)}${row(3)}${row(4,true)}</table></main>`;
      (async () => {
        const browser = await chromium.launch({channel:'chrome',headless:true});
        try {
          const context = await browser.newContext();
          await context.route('**/*', route => route.fulfill({contentType:'text/html; charset=utf-8',body:html}));
          const page = await context.newPage();
          await page.goto('https://mail.google.com/mail/u/0/#inbox');
          await page.evaluate(() => { window.fixtureClicked=0; document.addEventListener('click',()=>window.fixtureClicked++); });
          const result = await first(page);
          assert.equal(result.scope, 'current_view');
          assert.equal(result.account,email);
          assert.equal(result.observed_count,3);
          assert.deepEqual(result.items,[
            {sender:'Sender 1',subject:'Subject 1',date:'2026-10-08 1:00',unread:true},
            {sender:'Sender 2',subject:'Subject 2',date:'2026-10-08 2:00',unread:false},
          ]);
          assert.equal(await page.evaluate(()=>window.fixtureClicked),0);
          assert.equal(await page.locator('tr.zE').count(),1);
          assert.equal((await refresh(page)).account,email);
          await page.locator('header a').evaluate(e=>e.setAttribute('aria-label','Google Account: Other (other-fixture@example.test)'));
          assert.equal((await refresh(page)).error_code,'account_changed');
          page[Symbol.for('anis.browser.mail.binding')]='another-tab';
          assert.ok((await refresh(page)).error_code);
          await page.goto('https://mail.google.com.evil.example/');
          assert.equal((await first(page)).error_code,'origin');
          html = header + header.replace(email,'other-fixture@example.test') + '<main role="main"><table>'+row(1)+'</table></main>';
          await page.goto('https://mail.google.com/mail/u/0/#inbox');
          assert.equal((await first(page)).error_code,'account');
          const empty = '<td class="TC">검색어와 일치하는 메일이 없습니다. 발신자, 날짜, 크기 등의 검색 옵션을 사용해 보세요.</td>';
          html = header + '<main role="main"><table><tr>'+empty+'</tr></table></main>';
          await page.reload();
          const emptyResult = await first(page);
          assert.deepEqual(emptyResult.items,[]);
          assert.equal(emptyResult.observed_count,0);
          for (const nonList of [
            '<main role="main">Loading…</main>',
            '<main role="main"><article>Synthetic private message body</article></main>',
            '<main role="main"><table style="display:none"><tr>'+empty+'</tr></table></main>',
          ]) {
            html = header + nonList;
            await page.reload();
            assert.equal((await first(page)).error_code,'mail_list');
          }
          const naverHeader = `<div id="gnb"><div id="gnb_my_layer" class="gnb_my_li">
            <a id="gnb_my" href="#" onclick="event.preventDefault();const p=this.parentElement;p.classList.toggle('gnb_lyr_opened');p.querySelector('.gnb_mail_address').style.display=p.classList.contains('gnb_lyr_opened')?'block':'none';window.fixtureClicked++">Profile</a>
            <a class="gnb_mail_address" style="display:none">${email}</a></div></div>`;
          const naverRow = (i, read=false) => `<li class="mail_item ${read?'read':''}"><input class="toggle_read" type="checkbox" ${read?'checked':''}><button class="button_sender"><span class="blind">보낸 사람</span>${i===2?'<span>Sender 2</span>':`Sender ${i}`}</button><a class="mail_title_link"><span class="text">Subject ${i}</span></a><div class="mail_date_wrap"><span class="mail_date" style="display:none">hidden date</span><span class="mail_date">2026-10-08 ${i}:00</span></div></li>`;
          html = naverHeader + `<ul class="mail_list" style="min-height:1px">${naverRow(1)}${naverRow(2,true)}${naverRow(3)}</ul>`;
          await page.goto('https://mail.naver.com/v2/folders/0/all');
          await page.evaluate(()=>{window.fixtureClicked=0;});
          const naverResult = await naverFirst(page);
          assert.equal(naverResult.account,email);
          assert.equal(naverResult.observed_count,3);
          assert.deepEqual(naverResult.items,result.items);
          assert.equal(await page.locator('.button_sender .blind').count(),3);
          assert.equal(await page.locator('.button_sender').first().textContent(),'보낸 사람Sender 1');
          assert.equal(await page.locator('#gnb_my_layer').getAttribute('class'),'gnb_my_li');
          assert.equal(await page.evaluate(()=>window.fixtureClicked),2);
          assert.equal(await page.locator('li.read').count(),1);
          assert.equal(await page.locator('input.toggle_read:checked').count(),1);
          await page.locator('#gnb_my').click();
          assert.equal((await naverRefresh(page)).account,email);
          assert.equal(await page.evaluate(()=>window.fixtureClicked),3);
          await page.locator('.gnb_mail_address').evaluate(e=>{e.textContent='other-fixture@example.test';});
          assert.equal((await naverRefresh(page)).error_code,'account_changed');
          html = naverHeader + '<ul class="mail_list" style="min-height:1px"></ul>';
          await page.reload();
          assert.equal((await naverFirst(page)).error_code,'mail_list');
          assert.equal(await page.locator('#gnb_my_layer').getAttribute('class'),'gnb_my_li');
          console.log(JSON.stringify({verified:true}));
        } finally { await browser.close(); }
      })().catch(error => { console.error(error.stack); process.exitCode=1; });
    """
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"verified": True}


_APP = None


def pump_until(predicate, timeout=3):
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


@pytest.mark.parametrize("close_method", ["reject", "can_close_workspace_tab"])
def test_ui_current_page_filter_and_close_never_display_stale_results(close_method):
    from PyQt6.QtCore import Qt
    from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS
    assert pump_until(lambda: True)
    release, entered = threading.Event(), threading.Event()
    service = Mock()
    data = {"provider": "gmail", "account": ACCOUNT, "scope": "current_view",
            "items": [ITEM, {**ITEM, "subject": "<b>Literal mail title</b>", "unread": False}], "observed_count": 7}

    def list_current(provider, *, limit, checkpoint):
        assert provider == "gmail" and limit == 50
        entered.set()
        while not release.wait(.005):
            checkpoint()
        checkpoint()
        return data

    service.list_current.side_effect = list_current
    dialog = MailInboxDialog(service)
    login_urls = []
    dialog.login_requested.connect(login_urls.append)
    try:
        dialog._apply_result(data)
        assert dialog.table.rowCount() == 2
        assert dialog.table.item(1, 1).text() == "<b>Literal mail title</b>"
        assert dialog.account_label.textFormat() == Qt.TextFormat.PlainText
        assert "현재 페이지" in dialog.count_label.text() and "7" in dialog.count_label.text()
        dialog.unread_only.setChecked(True)
        assert dialog.table.rowCount() == 1
        dialog.search_input.setText("unmatched synthetic query")
        assert dialog.table.rowCount() == 0
        dialog.login_button.click()
        assert login_urls == ["https://accounts.google.com/"]
        assert not dialog._items
        dialog._start("list")
        assert pump_until(entered.is_set)
        assert dialog.table.rowCount() == 0 and not dialog.list_button.isEnabled()
        assert not dialog.login_button.isEnabled()
        dialog.login_button.click()
        assert len(login_urls) == 1
        getattr(dialog, close_method)()
        assert dialog._closed and not dialog._items
        release.set()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
        assert dialog.table.rowCount() == 0 and ACCOUNT not in dialog.account_label.text()
        assert service.disconnect.called
    finally:
        release.set()
        dialog.reject()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_ui_provider_change_cancels_inflight_mail_and_redacts_arbitrary_error():
    from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS
    assert pump_until(lambda: True)
    entered, release = threading.Event(), threading.Event()
    service = Mock()

    def slow(provider, *, limit, checkpoint):
        entered.set()
        while not release.wait(.005):
            checkpoint()
        checkpoint()
        raise RuntimeError("fixture-secret-token and private page text")

    service.list_current.side_effect = slow
    dialog = MailInboxDialog(service)
    try:
        dialog._start("list")
        assert pump_until(entered.is_set)
        dialog.provider.setCurrentIndex(1)
        release.set()
        assert pump_until(lambda: dialog._worker is None)
        assert not dialog._items and "fixture-secret" not in dialog.status_label.text()
        service.list_current.side_effect = RuntimeError("fixture-secret-token and private page text")
        dialog._start("list")
        assert pump_until(lambda: dialog._worker is None)
        assert "fixture-secret" not in dialog.status_label.text()
    finally:
        release.set()
        dialog.reject()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)


def test_ui_close_after_worker_finished_before_queued_result_is_delivered():
    from ui.mail_inbox_dialog import MailInboxDialog, _ACTIVE_OPERATIONS
    assert pump_until(lambda: True)
    service = Mock()
    service.list_current.return_value = {"provider": "gmail", "account": ACCOUNT, "scope": "current_view",
                                        "items": [ITEM], "observed_count": 1}
    dialog = MailInboxDialog(service)
    try:
        dialog._start("list")
        assert dialog._worker.wait(3000), "The synthetic worker must finish before GUI delivery"
        assert not dialog._items
        assert dialog.can_close_workspace_tab()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
        assert not dialog._items and dialog.table.rowCount() == 0
        assert ACCOUNT not in dialog.account_label.text()
        assert service.disconnect.called
    finally:
        dialog.reject()
        assert pump_until(lambda: dialog._worker is None and not _ACTIVE_OPERATIONS)
