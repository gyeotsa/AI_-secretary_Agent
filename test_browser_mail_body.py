"""Selected-body contracts with synthetic data only; no live account or vault."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from core.browser_mail import BODY_LIMIT, BrowserMailError, BrowserMailService
from core.plugin import ToolCancelledError


ACCOUNT = "body-reader@example.test"
REF = "12345678-1234-1234-1234-123456789abc:0"
ITEM = {"sender": "Synthetic sender", "subject": "Synthetic subject", "date": "2026-10-08",
        "unread": True, "message_ref": REF}
FINGERPRINT = hashlib.sha256(ACCOUNT.encode()).hexdigest()


def service_with_body(**changes):
    session, credentials = Mock(), Mock()
    credentials.status.return_value = {"configured": False}
    service = BrowserMailService(session, credentials)

    def response(code, **kwargs):
        config = json.loads(re.search(r"const config = (\{[^\n]+\});", code).group(1))
        common = {"provider": config["provider"], "binding": config["binding"], "account": ACCOUNT,
                  "fingerprint": FINGERPRINT}
        if "message_ref" in config:
            return {**common, "message_ref": config["message_ref"], "body": "<script>Literal text</script>\nSecond line",
                    "truncated": False, **changes}
        return {**common, "origin": config["origin"], "scope": "current_view", "items": [dict(ITEM)],
                "observed_count": 1}

    session.call.side_effect = response
    service.list_current("gmail")
    return service, session


def test_selected_body_exposes_only_bounded_plain_text_and_listed_metadata():
    service, session = service_with_body(password="synthetic-private-password", html="<img src=tracking>")
    result = service.read_message("gmail", REF)
    assert result == {"provider": "gmail", "account": ACCOUNT, "message_ref": REF,
                      "subject": ITEM["subject"], "sender": ITEM["sender"], "date": ITEM["date"],
                      "body": "<script>Literal text</script>\nSecond line", "truncated": False}
    assert "synthetic-private" not in json.dumps(result) and "html" not in result
    service.disconnect()


@pytest.mark.parametrize("provider,reference", [("gmail", "https://mail.google.com/arbitrary"),
                                               ("gmail", REF + "/evil"), ("gmail", 123),
                                               ("naver", REF), ("gmail", REF.replace(":0", ":1"))])
def test_invalid_or_unlisted_reference_never_calls_browser(provider, reference):
    service, session = service_with_body()
    session.call.reset_mock()
    with pytest.raises(BrowserMailError):
        service.read_message(provider, reference)
    session.call.assert_not_called()
    service.disconnect()


@pytest.mark.parametrize("changes", [{"provider": "naver"}, {"binding": "replacement-tab"},
                                      {"message_ref": REF.replace(":0", ":1")},
                                      {"account": "other-reader@example.test"}, {"fingerprint": "changed"},
                                      {"body": "x" * (BODY_LIMIT + 1)}, {"body": 7}, {"truncated": "false"},
                                      {"error_code": "tab_changed"}, {"error_code": "account_changed"}])
def test_invalid_body_contract_fails_closed_and_forgets_message_refs(changes):
    service, session = service_with_body(**changes)
    with pytest.raises(BrowserMailError):
        service.read_message("gmail", REF)
    assert not service._messages and not service._binding and session.close.called


@pytest.mark.parametrize("operation", ["disconnect", "switch", "refresh"])
def test_previous_list_reference_is_invalidated_by_list_or_connection_change(operation):
    service, session = service_with_body()
    if operation == "disconnect":
        service.disconnect()
    elif operation == "switch":
        service.switch_provider()
    else:
        original = session.call.side_effect

        def refresh(code, **kwargs):
            result = original(code, **kwargs)
            result["items"] = [{**ITEM, "message_ref": REF.replace(":0", ":1")}]
            return result

        session.call.side_effect = refresh
        service.list_current("gmail")
    session.call.reset_mock()
    with pytest.raises(BrowserMailError):
        service.read_message("gmail", REF)
    session.call.assert_not_called()


@pytest.mark.parametrize("kind", ["disconnect", "cancel", "exception"])
def test_late_body_or_raw_browser_error_never_publishes_content(kind):
    service, session = service_with_body()
    original = session.call.side_effect

    def response(code, **kwargs):
        if kind == "disconnect":
            service.disconnect()
            return original(code, **kwargs)
        if kind == "cancel":
            raise ToolCancelledError("synthetic cancellation")
        raise RuntimeError("synthetic-private-password and private message body")

    session.call.side_effect = response
    with pytest.raises(ToolCancelledError if kind == "cancel" else BrowserMailError) as error:
        service.read_message("gmail", REF)
    assert "synthetic-private-password" not in str(error.value) and "private message body" not in str(error.value)
    assert not service._messages


def test_truncation_is_explicit_and_empty_message_is_valid():
    for body, truncated in [("", False), ("x" * BODY_LIMIT, True)]:
        service, _ = service_with_body(body=body, truncated=truncated)
        assert service.read_message("gmail", REF)["truncated"] is truncated
        service.disconnect()


@pytest.mark.parametrize("stage", ["wait_body", "private-message-content", {"private": "message-content"}])
def test_body_failure_diagnostic_only_uses_allowed_stage_labels(stage):
    service, _ = service_with_body(error_code="message_body", stage=stage,
                                  error="private-password and private-message-content")
    with pytest.raises(BrowserMailError) as error:
        service.read_message("gmail", REF)
    assert ("본문 표시 대기 단계" in str(error.value)) == (stage == "wait_body")
    assert "private" not in str(error.value) and "message-content" not in str(error.value)


def test_real_selected_body_uses_bound_page_and_preserves_user_navigation():
    node = shutil.which("node")
    playwright = Path(__file__).parent / "data/tools/playwright-mcp/node_modules/playwright"
    if not node or not playwright.is_dir():
        pytest.skip("Installed Node.js and Playwright are required for isolated DOM verification")
    binding = "synthetic-body-binding"
    gmail_list = BrowserMailService._snippet("gmail", 2, binding, "", bind=True)
    naver_list = BrowserMailService._snippet("naver", 2, binding, "", bind=True)
    config = lambda code: json.loads(re.search(r"const config = (\{[^\n]+\});", code).group(1))
    gmail_ref, naver_ref = config(gmail_list)["references"] + ":0", config(naver_list)["references"] + ":0"
    gmail_body = BrowserMailService._body_snippet("gmail", gmail_ref, binding, FINGERPRINT)
    naver_body = BrowserMailService._body_snippet("naver", naver_ref, binding, FINGERPRINT)
    script = f"const {{chromium}}=require({json.dumps(str(playwright))});\n"
    script += f"const gmailList=({gmail_list}), naverList=({naver_list}), gmailBody=({gmail_body}), naverBody=({naver_body});\n"
    script += r"""
      const assert=require('node:assert/strict');
      const email='body-reader@example.test';
      let detailAccount=email, changedNaver=false;
      const gmailHtml=()=>'<header><a aria-label="Google Account: Fixture ('+email+')">Profile</a></header><main id="view"></main>'+String.raw`<script>
        window.fixture={huge:false,changedThread:false,bodyAccount:'body-reader@example.test'};
        const expanded='<div class="adn ads" data-message-id="synthetic-2"><div class="a3s aiL">Second synthetic body</div></div>';
        const render=()=>{
        const isBody=/^#inbox\/[a-f0-9]{16}$/.test(location.hash);
        document.querySelector('header a').setAttribute('aria-label','Google Account: Fixture ('+(isBody?fixture.bodyAccount:'body-reader@example.test')+')');
        if(isBody){
          document.querySelector('main').innerHTML='<h2 class="hP" data-legacy-thread-id="'+(fixture.changedThread?'0000000000000002':location.hash.split('/').pop())+'">Synthetic subject</h2><button aria-label="모두 펼치기">Expand</button><div class="adn ads" data-message-id="synthetic-1"><div class="a3s aiL">'+(fixture.huge?'x'.repeat(200001):'&lt;script&gt;Literal text&lt;/script&gt;<br>First synthetic body')+'</div></div>';
          document.querySelector('button').addEventListener('click',()=>setTimeout(()=>{document.querySelector('main').insertAdjacentHTML('beforeend',expanded);document.querySelector('button').setAttribute('aria-label','모두 접기');},50));
        }else{
          document.querySelector('main').innerHTML='<table><tr class="zA zE" role="row"><td class="yW">Synthetic sender</td><td class="bog"><span data-legacy-thread-id="0000000000000001">Synthetic subject</span></td><td class="xW">2026-10-08</td></tr></table>';
        }
        };
        window.addEventListener('hashchange',render);render();
      </script>`;
      const naverHeader=()=>`<div id="gnb"><div id="gnb_my_layer" class="gnb_my_li"><a id="gnb_my" href="#" onclick="event.preventDefault();const p=this.parentElement;p.classList.toggle('gnb_lyr_opened');p.querySelector('.gnb_mail_address').style.display=p.classList.contains('gnb_lyr_opened')?'block':'none'">Profile</a><a class="gnb_mail_address" style="display:none">${detailAccount}</a></div></div>`;
      const naverHtml=url=>naverHeader()+(url.includes('/v2/read/')?
        `<div class="mail_view_inner selected"><input class="toggle_bookmark" id="bookmark-${changedNaver?'2':'1'}"><div class="mail_view_contents_inner">Selected synthetic body<br>Second line</div></div><div class="mail_view_inner"><div class="mail_view_contents_inner">Other exchanged private text</div></div>`:
        '<ul class="mail_list"><li class="mail_item"><input class="toggle_read" type="checkbox"><button class="button_sender">Synthetic sender</button><a class="mail_title_link" href="/v2/popup/read/0/1"><span class="text">Synthetic subject</span></a><div class="mail_date_wrap"><span class="mail_date">2026-10-08</span></div></li></ul>');
      (async()=>{
        const browser=await chromium.launch({channel:'chrome',headless:true});
        try{
          const context=await browser.newContext();
          await context.route('**/*', route=>route.fulfill({contentType:'text/html; charset=utf-8',body:route.request().url().startsWith('https://mail.naver.com')?naverHtml(route.request().url()):gmailHtml()}));
          let createdPages=0;context.on('page',()=>createdPages++);
          const source=await context.newPage();
          await source.goto('https://mail.google.com/mail/u/0/#inbox');
          const gmail=await gmailList(source), originalUrl=source.url();
          assert.ok(gmail.items[0].message_ref);
          assert.ok(!JSON.stringify(gmail).includes('messageRoutes'));
          let result=await gmailBody(source);
          assert.equal(result.account,email);
          assert.equal(result.truncated,false);
          assert.ok(result.body.includes('<script>Literal text</script>')&&result.body.includes('Second synthetic body'));
          assert.equal(context.pages().length,1);assert.equal(createdPages,1);
          assert.equal(source.url(),originalUrl);
          await source.evaluate(()=>fixture.huge=true);result=await gmailBody(source);await source.evaluate(()=>fixture.huge=false);
          assert.equal(result.body.length,200000);assert.equal(result.truncated,true);
          assert.equal(source.url(),originalUrl);
          const resetGmail=async()=>{await source.goto(originalUrl);await source.locator('tr.zA').waitFor();await gmailList(source);};
          await source.evaluate(()=>fixture.changedThread=true);
          assert.equal((await gmailBody(source)).error_code,'message_changed');
          assert.notEqual(source.url(),originalUrl);
          await source.evaluate(()=>fixture.changedThread=false);await resetGmail();
          await source.evaluate(()=>fixture.bodyAccount='other-reader@example.test');
          assert.equal((await gmailBody(source)).error_code,'account_changed');
          assert.notEqual(source.url(),originalUrl);
          await source.evaluate(()=>fixture.bodyAccount='body-reader@example.test');await resetGmail();

          // A user changing the URL during account verification must keep their view.
          const evaluate=source.evaluate.bind(source), userUrl='https://mail.google.com/mail/u/0/#inbox/0000000000000002';
          let moved=false;
          source.evaluate=async(fn,arg)=>{const value=await evaluate(fn,arg);if(arg?.expectedFingerprint&&source.url()!==originalUrl&&!moved){moved=true;await source.goto(userUrl);}return value;};
          result=await gmailBody(source);
          assert.equal(result.error_code,'message_changed');assert.ok(!('body' in result));assert.equal(source.url(),userUrl);
          source.evaluate=evaluate;await resetGmail();

          // Check-and-return must also reject a move after the final account check.
          source.evaluate=async(fn,arg)=>{if(arg?.from){await source.goto(userUrl);}return evaluate(fn,arg);};
          result=await gmailBody(source);
          assert.equal(result.error_code,'message_changed');assert.equal(source.url(),userUrl);
          source.evaluate=evaluate;await resetGmail();

          // Replacing the route cache or binding mid-read invalidates the result.
          for(const change of ['cache','binding']){
            let changed=false;
            source.evaluate=async(fn,arg)=>{const value=await evaluate(fn,arg);if(arg?.expectedFingerprint&&source.url()!==originalUrl&&!changed){changed=true;if(change==='cache')source[Symbol.for('anis.browser.mail.messages')]={...source[Symbol.for('anis.browser.mail.messages')]};else source[Symbol.for('anis.browser.mail.binding')]='replacement-binding';}return value;};
            result=await gmailBody(source);assert.equal(result.error_code,change==='cache'?'message_changed':'tab_changed');assert.notEqual(source.url(),originalUrl);
            source.evaluate=evaluate;await resetGmail();
          }

          // A successful body read is discarded if list restoration cannot be confirmed.
          const waitForURL=source.waitForURL.bind(source);
          source.waitForURL=async()=>{throw new Error('synthetic restore failure');};
          result=await gmailBody(source);
          assert.equal(result.error_code,'message_body');assert.equal(result.stage,'restore_list');assert.ok(!('body' in result));
          source.waitForURL=waitForURL;await resetGmail();
          assert.equal(context.pages().length,1);
          source[Symbol.for('anis.browser.mail.binding')]='replacement-binding';
          assert.equal((await gmailBody(source)).error_code,'tab_changed');
          assert.equal(context.pages().length,1);
          await source.goto('https://mail.naver.com/v2/folders/0/all');
          await naverList(source);
          result=await naverBody(source);
          assert.equal(result.body,'Selected synthetic body\nSecond line');
          assert.ok(!result.body.includes('Other exchanged'));
          assert.equal(source.url(),'https://mail.naver.com/v2/folders/0/all');
          assert.equal(await source.locator('#gnb_my_layer').getAttribute('class'),'gnb_my_li');
          assert.equal(context.pages().length,1);
          changedNaver=true;
          assert.equal((await naverBody(source)).error_code,'message_changed');changedNaver=false;
          assert.equal(source.url(),'https://mail.naver.com/v2/read/0/1');
          await source.goto('https://mail.naver.com/v2/folders/0/all');await naverList(source);
          assert.equal(context.pages().length,1);
          source[Symbol.for('anis.browser.mail.messages')].routes.set(result.message_ref,{route:'https://evil.example/',subject:'Synthetic subject'});
          assert.equal((await naverBody(source)).error_code,'message_changed');
          assert.equal(context.pages().length,1);assert.equal(createdPages,1);
          await source.goto('https://mail.naver.com/v2/folders/1/all');
          assert.equal((await naverBody(source)).error_code,'message_changed');
          console.log(JSON.stringify({verified:true}));
        }finally{await browser.close();}
      })().catch(error=>{console.error(error.stack);process.exitCode=1;});
    """
    result = subprocess.run([node, "-"], input=script, text=True, encoding="utf-8", capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"verified": True}
