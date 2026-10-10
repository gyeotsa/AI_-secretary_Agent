"""Shared browser-mail tools and exact draft counts, using synthetic fixtures."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
from unittest.mock import Mock

import pytest

from core.browser_mail import BrowserMailError, BrowserMailService
from core.plugin import PluginRegistry, ToolCancelledError
from plugins.browser_mail import BrowserMailPlugin
from test_browser_mail import ACCOUNT, credentials


def test_shared_service_singleton_has_a_separate_workflow_lock(monkeypatch):
    import core.browser_mail as module
    monkeypatch.setattr(module, "_SERVICE", None)
    first = module.get_browser_mail_service()
    assert first is module.get_browser_mail_service()
    assert first.workflow_lock is not first._lock


@pytest.mark.parametrize("configured", [False, True])
def test_automatic_inbox_requires_token_and_reuses_running_chrome(monkeypatch, configured):
    service = BrowserMailService(Mock(), credentials(configured))
    monkeypatch.setattr("core.browser_mail.psutil.process_iter", lambda _: [Mock(info={"name": "chrome.exe"})])
    opened = Mock()
    monkeypatch.setattr("core.browser_mail.open_chrome", opened)
    service.list_current = Mock(return_value={"provider": "gmail"})
    if not configured:
        with pytest.raises(BrowserMailError, match="토큰"):
            service.list_inbox("gmail")
        service.list_current.assert_not_called()
    else:
        assert service.list_inbox("gmail", 4) == {"provider": "gmail"}
        assert service.list_current.call_args.kwargs["_inbox"] is True
    opened.assert_not_called()


def test_closed_chrome_is_opened_with_fixed_mail_url_before_collection(monkeypatch):
    service = BrowserMailService(Mock(), credentials(True))
    monkeypatch.setattr("core.browser_mail.psutil.process_iter", lambda _: [])
    opened = Mock()
    monkeypatch.setattr("core.browser_mail.open_chrome", opened)
    service.list_current = Mock()
    service.list_inbox("naver")
    opened.assert_called_once_with("https://mail.naver.com/")
    service.list_current.assert_called_once()


def draft_service(**changes):
    session = Mock()
    service = BrowserMailService(session, credentials())
    service._provider, service._binding = "gmail", "test-binding"
    service._fingerprint = hashlib.sha256(ACCOUNT.encode()).hexdigest()
    session.call.return_value = {"provider": "gmail", "binding": service._binding, "account": ACCOUNT,
                                "fingerprint": service._fingerprint, "count": 125, **changes}
    return service, session


@pytest.mark.parametrize("count", [0, 1, 125])
def test_draft_count_uses_verified_integer_total(count):
    service, _ = draft_service(count=count)
    assert service.draft_count("gmail") == count


@pytest.mark.parametrize("changes", [{"count": None}, {"count": True}, {"count": -1},
    {"count": 10000001}, {"account": "changed@example.test"}, {"fingerprint": "wrong"},
    {"binding": "replaced"}, {"provider": "naver"}, {"error_code": "draft_count"}])
def test_unverified_draft_total_never_becomes_zero(changes):
    service, _ = draft_service(**changes)
    with pytest.raises(BrowserMailError):
        service.draft_count("gmail")


def test_draft_disconnect_cancel_and_remote_error_fail_without_content():
    service, session = draft_service()
    def stale(*args, **kwargs):
        service.disconnect()
        return {"count": 0}
    session.call.side_effect = stale
    with pytest.raises(BrowserMailError):
        service.draft_count("gmail")
    service, session = draft_service()
    session.call.side_effect = RuntimeError("private mail body fixture-token")
    with pytest.raises(BrowserMailError) as error:
        service.draft_count("gmail")
    assert "private" not in str(error.value) and "fixture" not in str(error.value)
    service, _ = draft_service()
    with pytest.raises(ToolCancelledError):
        service.draft_count("gmail", checkpoint=lambda: (_ for _ in ()).throw(ToolCancelledError()))


def tool_services():
    browser = Mock()
    browser.workflow_lock = threading.Lock()
    brief = Mock()
    brief.collect.return_value = {"providers": [{"provider": "gmail", "new_count": 2,
        "list_count": 5, "reply_count": 1, "draft_count": 3}], "summary": "신규 2건, 회신 검토 1건, 초안 3건"}
    return BrowserMailPlugin(browser, brief), browser, brief


def test_tools_contracts_do_not_claim_authentication_and_intent_selects_provider():
    plugin, _, _ = tool_services()
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    assert registry.validate_contracts() == {}
    assert plugin.probe_authentication().state == "unchecked"
    tools = {tool.name: tool for tool in plugin.get_tools()}
    for name in ("browser_mail_collect_summary", "browser_mail_list_inbox", "browser_mail_read_message"):
        assert tools[name].required_permissions == ["cloud_read"]
        assert tools[name].cancellable and tools[name].max_retries == 0
    assert plugin.extract_slots("mail.brief", "네이버 메일 현황 알려줘", {}) == {"providers": ["naver"]}


def test_summary_reuses_shared_workflow_and_only_returns_aggregate():
    plugin, _, brief = tool_services()
    result = plugin.execute_tool("browser_mail_collect_summary", {})
    assert result.succeeded and json.loads(result.raw_output) == brief.collect.return_value
    assert brief.collect.call_args.kwargs["providers"] == ("naver", "gmail")


def test_no_provider_verified_is_explicitly_unverified():
    plugin, _, brief = tool_services()
    brief.collect.return_value = {"providers": [{"provider": "gmail", "list_count": None,
        "new_count": None, "reply_count": None, "draft_count": None, "error": "연결 확인 불가"}], "summary": "확인 불가"}
    result = plugin.execute_tool("browser_mail_collect_summary", {})
    assert result.status == "unverified"
    assert json.loads(result.raw_output)["providers"][0]["new_count"] is None


def test_mail_read_tool_analyzes_locally_and_never_returns_body_or_reply_quote():
    plugin, browser, _ = tool_services()
    browser.read_message.return_value = {"body": "private full body fixture", "provider": "gmail"}
    browser.analysis.analyze_message.return_value = {"status": "complete", "summary": "짧은 요약",
        "category_id": "work", "reply_required": True, "partial": False, "reply_evidence": "private quote"}
    result = plugin.execute_tool("browser_mail_read_message", {"provider": "gmail", "message_ref": "a" * 36 + ":0"})
    assert result.succeeded
    assert "private" not in result.raw_output
    assert json.loads(result.raw_output)["reply_required"] is True
    assert not browser.workflow_lock.locked()


@pytest.mark.parametrize("name,data", [("browser_mail_list_inbox", {"provider": "outlook"}),
    ("browser_mail_collect_summary", {"providers": ["gmail", "gmail"]}),
    ("browser_mail_read_message", {"provider": "gmail", "message_ref": "https://evil.test/"}),
    ("browser_mail_category_settings", {"action": "set"})])
def test_invalid_tool_input_is_rejected_without_mail_reads(name, data):
    plugin, browser, brief = tool_services()
    assert not plugin.execute_tool(name, data).succeeded
    browser.read_message.assert_not_called()
    brief.collect.assert_not_called()


def test_busy_workflow_and_cancel_are_not_replayed():
    plugin, browser, _ = tool_services()
    browser.workflow_lock.acquire()
    assert not plugin.execute_tool("browser_mail_list_inbox", {"provider": "gmail"}).succeeded
    browser.list_inbox.assert_not_called()
    browser.workflow_lock.release()
    browser.list_inbox.side_effect = ToolCancelledError()
    with pytest.raises(ToolCancelledError):
        plugin.execute_tool("browser_mail_list_inbox", {"provider": "gmail"})
    assert not browser.workflow_lock.locked()


def test_category_settings_get_and_save_reuse_analysis_validation():
    plugin, browser, _ = tool_services()
    settings = {"enabled": True, "categories": [{"id": "other", "name": "기타", "description": "기타", "notify": False}]}
    browser.analysis.settings.return_value = settings
    browser.analysis.save_settings.return_value = settings
    assert json.loads(plugin.execute_tool("browser_mail_category_settings", {}).raw_output) == settings
    result = plugin.execute_tool("browser_mail_category_settings", {"action": "set", **settings})
    assert result.succeeded
    browser.analysis.save_settings.assert_called_once_with(True, settings["categories"])


def test_real_draft_dom_counts_total_not_page_rows_and_preserves_user_navigation():
    node = shutil.which("node")
    playwright = Path(__file__).parent / "data/tools/playwright-mcp/node_modules/playwright"
    if not node or not playwright.is_dir():
        pytest.skip("Installed Node.js and Playwright are required for isolated DOM verification")
    fingerprint = hashlib.sha256(ACCOUNT.encode()).hexdigest()
    gmail = BrowserMailService._draft_snippet("gmail", "binding", fingerprint)
    naver = BrowserMailService._draft_snippet("naver", "binding", fingerprint)
    inbox = BrowserMailService._snippet("gmail", 2, "binding", fingerprint, bind=True, inbox=True)
    script = f"const {{chromium}}=require({json.dumps(str(playwright))}); const gmail=({gmail}), naver=({naver}), inbox=({inbox});\n"
    script += r"""
      const assert=require('node:assert/strict');
      const account='fixture-reader@example.test';
      let mode='total', otherAccount=false;
      const gmailHTML=()=>`<header><a aria-label="Google Account: Fixture (${otherAccount?'other@example.test':account})">Profile</a></header><div class="Dj" style="display:none">1071개 중1–50</div><table style="display:none"><tr class="zA" role="row"><td class="yW">Inbox sender</td></tr></table><main role="main"></main><script>
        const render=()=>{const draft=location.hash==='#drafts';document.querySelector('main').innerHTML=draft?
          ${JSON.stringify(mode==='empty'?'<table><tr><td class="TC">No drafts.</td></tr></table>':mode==='unknown'?'<table><tr class="zA" role="row"><td class="yW">Draft</td></tr></table>':mode==='korean'?'<div class="Dj">2개 중 1–2</div><table><tr class="zA" role="row"><td class="yW">임시보관</td></tr></table>':'<div class="Dj">1–50 of 125</div><table><tr class="zA" role="row"><td class="yW">Draft</td></tr></table>')}:
          '<table><tr class="zA" role="row"><td class="yW">Sender</td><td class="bog"><span data-legacy-thread-id="0000000000000001">Subject</span></td><td class="xW">Today</td></tr></table>';};
        addEventListener('hashchange',render);render();</script>`;
      const naverHTML=url=>`<div id="gnb"><a class="gnb_mail_address">${otherAccount?'other@example.test':account}</a></div><a class="mailbox_label svg_temporary" title="임시보관함" href="#" onclick="event.preventDefault();location.assign('/v2/folders/3')">임시보관함</a>`+
        (url.endsWith('/3')?'<h2 class="mailbox_title"><span class="text">임시보관함</span><a class="total"><span class="blind">전체 메일</span>22<span class="blind">개</span></a></h2><ul class="mail_list"><li class="mail_item">row</li></ul>':'<ul class="mail_list"><li class="mail_item">inbox</li></ul>');
      (async()=>{
        const browser=await chromium.launch({headless:true,channel:'chrome'});
        try{
          const context=await browser.newContext();
          await context.route('https://mail.google.com/**',route=>route.fulfill({contentType:'text/html; charset=utf-8',body:gmailHTML()}));
          await context.route('https://mail.naver.com/**',route=>route.fulfill({contentType:'text/html; charset=utf-8',body:naverHTML(route.request().url())}));
          const page=await context.newPage();
          const reset=async()=>{await page.goto('https://mail.google.com/mail/u/2/#inbox');await page.reload();page[Symbol.for('anis.browser.mail.binding')]='binding';};
          await reset();let result=await gmail(page);assert.equal(result.count,125,JSON.stringify(result));assert.equal(page.url(),'https://mail.google.com/mail/u/2/#inbox');
          mode='korean';await reset();result=await gmail(page);assert.equal(result.count,2);
          mode='empty';await reset();result=await gmail(page);assert.equal(result.count,0);
          mode='unknown';await reset();result=await gmail(page);assert.equal(result.error_code,'draft_count');assert.equal(page.url(),'https://mail.google.com/mail/u/2/#inbox');
          mode='total';otherAccount=true;await reset();result=await gmail(page);assert.equal(result.error_code,'account_changed');assert.equal(page.url(),'https://mail.google.com/mail/u/2/#inbox');otherAccount=false;
          await reset();const evaluate=page.evaluate.bind(page),userURL='https://mail.google.com/mail/u/2/#sent';
          page.evaluate=async(fn,arg)=>{if(arg?.from)await page.goto(userURL);return evaluate(fn,arg);};
          result=await gmail(page);assert.equal(result.error_code,'tab_changed');assert.equal(page.url(),userURL);page.evaluate=evaluate;
          await reset();await page.goto('https://mail.google.com/mail/u/2/#sent');result=await inbox(page);assert.equal(result.account,account);assert.equal(page.url(),'https://mail.google.com/mail/u/2/#inbox');
          await page.goto('https://mail.naver.com/v2/folders/0/all');page[Symbol.for('anis.browser.mail.binding')]='binding';
          result=await naver(page);assert.equal(result.count,22);assert.equal(page.url(),'https://mail.naver.com/v2/folders/0/all');
          assert.equal(context.pages().length,1);console.log(JSON.stringify({verified:true}));
        }finally{await browser.close();}
      })().catch(error=>{console.error(error.stack);process.exitCode=1;});
    """
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8", capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"verified": True}
