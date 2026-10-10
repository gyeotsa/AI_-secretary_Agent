from copy import deepcopy
import hashlib
import json
import os
import subprocess
import sys
import threading

import pytest

from core.mail_brief import MailBriefError, MailBriefService, _event_boot_id
from core.plugin import ToolCancelledError


class Vault:
    def __init__(self, directory):
        self.directory, self.saved = directory, {}

    def load(self, namespace, key):
        return deepcopy(self.saved.get((namespace, key)))

    def save(self, namespace, key, value):
        self.saved[namespace, key] = deepcopy(value)


def item(number, **changes):
    value = {"message_key": hashlib.sha256(str(number).encode()).hexdigest(),
             "message_ref": f"reference-{number}", "sender": "private-sender-marker",
             "subject": "private-subject-marker", "date": "2026-10-09", "unread": True}
    value.update(changes)
    return value


class Analysis:
    def __init__(self):
        self.records = {}
        self.failure = self.read_failure = False
        self.collected = []

    def collect(self, provider, account, items, read_message, *, checkpoint, progress):
        checkpoint()
        self.collected.append(provider)
        if self.failure:
            raise RuntimeError("private-body-marker")
        for mail in items:
            self.records.setdefault(mail["message_key"], {
                "message_key": mail["message_key"], "status": "complete", "partial": False,
                "category_id": "work", "reply_required": False})
        if progress:
            progress({"processed": len(items), "total": len(items)})

    def current_records(self, provider, account, items):
        if self.read_failure:
            raise RuntimeError("private-body-marker")
        return deepcopy(list(self.records.values()))

    def settings(self):
        return {"enabled": True, "categories": [{"id": "work", "notify": True},
                                                {"id": "promotion", "notify": False}]}


class Browser:
    def __init__(self):
        self.workflow_lock = threading.Lock()
        self.analysis = Analysis()
        self.items = {"naver": [item(1)], "gmail": [item(2)]}
        self.errors, self.draft_errors = set(), set()
        self.drafts = {"naver": 2, "gmail": 0}
        self.calls = []

    def list_inbox(self, provider, *, checkpoint):
        checkpoint()
        self.calls.append((provider, "list"))
        if provider in self.errors:
            raise RuntimeError("private-token-marker")
        return {"provider": provider, "account": provider + "@example.invalid",
                "items": deepcopy(self.items[provider])}

    def read_message(self, *args, **kwargs):
        raise AssertionError("Fake analysis does not read private content")

    def draft_count(self, provider, *, checkpoint):
        checkpoint()
        self.calls.append((provider, "drafts"))
        if provider in self.draft_errors:
            raise RuntimeError("private-body-marker")
        return self.drafts[provider]


@pytest.fixture
def service(tmp_path):
    return MailBriefService(browser=Browser(), vault=Vault(tmp_path), boot_provider=lambda: "boot-1")


def test_first_snapshot_is_a_baseline_not_new_and_unread_is_separate(service):
    result = service.collect()
    assert [c["new_count"] for c in result["providers"]] == [None, None]
    assert [c["unread_count"] for c in result["providers"]] == [1, 1]
    assert [c["draft_count"] for c in result["providers"]] == [2, 0]
    assert "최초 확인" in result["summary"] and "신규 1건" not in result["summary"]
    assert service.browser.calls == [("naver", "list"), ("naver", "drafts"),
                                    ("gmail", "list"), ("gmail", "drafts")]
    service.browser.items["naver"][0]["unread"] = False
    again = service.collect()["providers"][0]
    assert again["unread_count"] == 0 and again["new_count"] == 0 and again["updated_count"] == 0


def test_restart_counts_new_keys_and_changed_threads_separately(service):
    service.collect()
    browser = service.browser
    browser.items["naver"][0]["date"] = "2026-10-10"
    browser.items["naver"].append(item(3))
    restarted = MailBriefService(browser=browser, vault=service.vault)
    result = restarted.collect()["providers"][0]
    assert result["new_count"] == result["updated_count"] == result["filtered_new_count"] == 1
    assert result["list_count"] == 2


def test_filter_current_complete_records_and_notify_categories(service):
    service.collect(("gmail",))
    browser = service.browser
    browser.items["gmail"] = [item(n) for n in range(2, 8)]
    for number, status, category, partial, reply in (
            (2, "complete", "work", False, True),
            (3, "complete", "promotion", False, True),
            (4, "partial", "work", True, None),
            (5, "unknown", None, False, None),
            (6, "complete", "work", False, True),
            (99, "complete", "work", False, True)):
        browser.analysis.records[item(number)["message_key"]] = {
            "message_key": item(number)["message_key"], "status": status, "category_id": category,
            "partial": partial, "reply_required": reply}
    result = service.collect(("gmail",))["providers"][0]
    assert result["new_count"] == 5
    assert result["filtered_new_count"] == 2
    assert result["reply_count"] == 2
    assert result["partial_count"] == result["unknown_count"] == 1


def test_legacy_reply_unknown_is_separate_from_classification_unknown_and_filtered(service):
    service.browser.items["gmail"] = [item(n) for n in range(4)]
    for number, status, category in ((0, "complete", "work"), (1, "complete", "promotion"),
                                    (2, "unknown", None), (3, "partial", "work")):
        service.browser.analysis.records[item(number)["message_key"]] = {
            "message_key": item(number)["message_key"], "status": status, "category_id": category,
            "partial": status == "partial", "reply_required": None}
    result = service.collect(("gmail",))
    counts = result["providers"][0]
    assert counts["reply_count"] == 0 and counts["reply_unknown_count"] == 1
    assert counts["unknown_count"] == counts["partial_count"] == 1
    assert "회신·자료 전달 필요 여부 미확인 1건" in result["summary"]
    assert "선택한 알림 카테고리" in result["summary"] and "AI 추정" in result["summary"]


def test_removed_rows_that_return_are_not_new_again(service):
    service.collect(("gmail",))
    service.browser.items["gmail"] = [item(3)]
    service.collect(("gmail",))
    service.browser.items["gmail"] = [item(2)]
    assert service.collect(("gmail",))["providers"][0]["new_count"] == 0


def test_provider_failure_is_isolated_and_not_misreported_as_zero(service):
    service.browser.errors.add("naver")
    result = service.collect()
    assert result["providers"][0]["list_count"] is None
    assert result["providers"][0]["draft_count"] is None
    assert result["providers"][1]["list_count"] == 1
    assert result["providers"][0]["error"]
    assert "private-token-marker" not in json.dumps(result)


@pytest.mark.parametrize("value", [None, -1, True, "2"])
def test_unknown_draft_count_is_not_zero(service, value):
    service.browser.drafts["gmail"] = value
    result = service.collect(("gmail",))
    assert result["providers"][0]["draft_count"] is None
    assert "임시보관함 확인 실패" in result["summary"]


def test_analysis_failure_retains_list_and_draft_counts_without_exposing_exception(service):
    service.browser.analysis.failure = True
    result = service.collect(("gmail",))
    counts = result["providers"][0]
    assert counts["list_count"] == 1 and counts["draft_count"] == 0
    assert counts["unknown_count"] == 1 and counts["analysis_error"]
    assert "본문 수집" in counts["analysis_error"]
    assert "private-body-marker" not in json.dumps(result)


def test_record_failure_is_unknown_and_never_uses_old_records(service):
    service.browser.analysis.read_failure = True
    counts = service.collect(("gmail",))["providers"][0]
    assert counts["unknown_count"] == 1 and counts["reply_count"] == 0


def test_private_store_and_output_contain_no_mail_metadata_or_address(service):
    result = service.collect()
    output = json.dumps(result, ensure_ascii=False)
    saved = json.dumps(list(service.vault.saved.items()), ensure_ascii=False)
    for value in ("private-sender-marker", "private-subject-marker", "gmail@example.invalid"):
        assert value not in output and value not in saved
    assert "seen" in saved


def test_collect_cancellation_releases_workflow_and_does_not_save_baselines(service):
    def progress(value):
        if value["phase"] == "completed":
            raise ToolCancelledError("cancelled")
    with pytest.raises(ToolCancelledError):
        service.collect(progress=progress)
    assert service.vault.saved == {}
    assert service.browser.workflow_lock.acquire(blocking=False)
    service.browser.workflow_lock.release()


def test_busy_workflow_does_not_start_another_browser_collection(service):
    service.browser.workflow_lock.acquire()
    try:
        with pytest.raises(MailBriefError, match="다른 메일 작업"):
            service.collect()
        assert service.browser.calls == []
    finally:
        service.browser.workflow_lock.release()


def test_startup_waits_for_existing_mail_work_before_collecting(service):
    service.browser.workflow_lock.acquire()
    waiting, finished = threading.Event(), threading.Event()
    results = []

    def collect():
        results.append(service.collect(wait=True, checkpoint=waiting.set))
        finished.set()

    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    try:
        assert waiting.wait(2)
        assert not finished.is_set() and service.browser.calls == []
    finally:
        service.browser.workflow_lock.release()
    worker.join(2)
    assert finished.is_set() and len(results[0]["providers"]) == 2


def test_cancel_while_waiting_keeps_other_mail_work_lock_owned(service):
    service.browser.workflow_lock.acquire()
    attempts = 0

    def checkpoint():
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ToolCancelledError("cancelled")

    try:
        with pytest.raises(ToolCancelledError):
            service.collect(wait=True, checkpoint=checkpoint)
        assert service.browser.workflow_lock.locked() and service.browser.calls == []
    finally:
        service.browser.workflow_lock.release()


@pytest.mark.parametrize("providers", [(), ("unknown",), ("gmail", "gmail"), "gmail", ([],), ("gmail", "naver", "gmail")])
def test_invalid_provider_sets_do_not_touch_mail(service, providers):
    with pytest.raises(MailBriefError):
        service.collect(providers)
    assert service.browser.calls == []


def test_corrupt_baseline_is_not_replaced_with_first_run_zero(service):
    service.collect(("gmail",))
    key = next(k for k in service.vault.saved if k[1].startswith("account-"))
    service.vault.saved[key] = {"version": 1, "seen": {"bad": "bad"}}
    counts = service.collect(("gmail",))["providers"][0]
    assert counts["list_count"] is None and "집계 기준 형식" in counts["error"]
    assert service.vault.saved[key]["seen"] == {"bad": "bad"}


def test_seen_retention_keeps_recent_2000_hashes(service):
    service.collect(("gmail",))
    key = next(k for k in service.vault.saved if k[1].startswith("account-"))
    seen = {item(n)["message_key"]: hashlib.sha256(b"metadata").hexdigest() for n in range(2000)}
    service.vault.saved[key] = {"version": 1, "seen": seen}
    service.browser.items["gmail"] = [item(3000)]
    result = service.collect(("gmail",))["providers"][0]
    assert result["new_count"] == 1
    retained = service.vault.saved[key]["seen"]
    assert len(retained) == 2000 and item(3000)["message_key"] in retained
    assert item(0)["message_key"] not in retained


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lease")
def test_startup_lease_blocks_other_instances_until_ui_delivery_and_new_boot(service):
    other = MailBriefService(vault=service.vault, boot_provider=lambda: "boot-1")
    assert service.claim_startup() == "boot-1"
    assert service.claim_startup() is None and other.claim_startup() is None
    service.finish_startup("boot-1")
    assert other.claim_startup() is None
    rebooted = MailBriefService(vault=service.vault, boot_provider=lambda: "boot-2")
    assert rebooted.claim_startup() == "boot-2"
    rebooted.cancel_startup()


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lease")
def test_startup_cancel_and_non_delivery_leave_collection_baseline_and_boot_uncommitted(service):
    assert service.claim_startup() == "boot-1"
    service.collect()
    assert service.vault.saved == {}
    service.cancel_startup()
    assert service.claim_startup() == "boot-1"
    result = service.collect()
    assert all(c["new_count"] is None for c in result["providers"])
    service.finish_startup("boot-1", delivered=False)
    assert service.vault.saved == {}
    assert service.claim_startup() == "boot-1"
    service.collect()
    service.finish_startup("boot-1", delivered=True)
    assert service.vault.load("anis-mail-brief", "startup")["boot_id"] == "boot-1"
    assert service.collect()["providers"][0]["new_count"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lease")
def test_startup_cancel_during_collection_cannot_save_as_manual_baseline(service):
    service.claim_startup()
    def cancel(value):
        if value["phase"] == "completed":
            service.cancel_startup()
    with pytest.raises(ToolCancelledError):
        service.collect(progress=cancel)
    assert service.vault.saved == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lease")
def test_store_failure_releases_lease_for_retry_without_marking_boot(service):
    service.claim_startup()
    original_save = service.vault.save
    def fail(*args):
        raise RuntimeError("private-token-marker")
    service.vault.save = fail
    with pytest.raises(MailBriefError, match="암호화"):
        service.finish_startup("boot-1")
    service.vault.save = original_save
    assert service.claim_startup() == "boot-1"
    service.cancel_startup()


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lease")
def test_startup_lock_is_owned_across_processes(service):
    assert service.claim_startup() == "boot-1"
    code = """import msvcrt,sys
f=open(sys.argv[1],'r+b')
try:
    msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
except OSError:
    print('locked')
else:
    print('acquired')
f.close()
"""
    result = subprocess.run([sys.executable, "-c", code, str(service.vault.directory / ".startup.lock")],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "locked"
    service.cancel_startup()


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lease")
def test_wrong_startup_id_cannot_mark_a_different_boot_delivered(service):
    service.claim_startup()
    with pytest.raises(MailBriefError):
        service.finish_startup("other-boot")
    assert service.vault.saved == {}
    service.cancel_startup()


@pytest.mark.parametrize("boot", [None, "", "x" * 257, "new\nboot", 1])
def test_unknown_boot_identifier_fails_closed_without_claim(service, boot):
    service._boot_provider = lambda: boot
    with pytest.raises(MailBriefError, match="부팅 기준"):
        service.claim_startup()
    assert service.vault.saved == {} and not (service.vault.directory / ".startup.lock").exists()


def event_xml(boot_type, *, record="100", created="2026-10-09T00:00:00Z"):
    return f'''<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>
<Provider Name="Microsoft-Windows-Kernel-Boot"/><EventID>27</EventID>
<EventRecordID>{record}</EventRecordID><TimeCreated SystemTime="{created}"/></System>
<EventData><Data Name="BootType">{boot_type}</Data></EventData></Event>'''


def test_boot_event_distinguishes_cold_fast_startup_and_hibernation_resume():
    assert len(_event_boot_id(event_xml("0"))) == 64
    assert len(_event_boot_id(event_xml("1"))) == 64
    assert _event_boot_id(event_xml("2")) is None
    assert _event_boot_id(event_xml("0x2")) is None
    assert _event_boot_id(event_xml("1")) != _event_boot_id(event_xml("1", record="101"))
    assert _event_boot_id(event_xml("1")) != _event_boot_id(event_xml("1", created="2026-10-10T00:00:00Z"))
