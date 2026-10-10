"""Private inbox checkpoints and one aggregate mail brief per Windows startup."""
from __future__ import annotations

import os
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

from config import Config
from core.mail_analysis import _digest, _metadata, _scope
from core.plugin import ToolCancelledError
from core.turn_context import check_turn_cancelled


class MailBriefError(ValueError):
    """Fixed application diagnostics without account or mail content."""


_NAMESPACE = "anis-mail-brief"
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_LIMIT = 2000
_NAMES = {"naver": "네이버", "gmail": "Gmail"}
_LOCK = threading.RLock()
_EVENT_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}


def _event_boot_id(xml):
    root = ET.fromstring(xml)
    provider = root.find("e:System/e:Provider", _EVENT_NS)
    if (provider is None or provider.get("Name") != "Microsoft-Windows-Kernel-Boot"
            or root.findtext("e:System/e:EventID", namespaces=_EVENT_NS) != "27"):
        return None
    value = next((e.text for e in root.findall("e:EventData/e:Data", _EVENT_NS)
                  if e.get("Name") == "BootType"), "")
    if int(value, 0) not in (0, 1):
        return None
    record = root.findtext("e:System/e:EventRecordID", namespaces=_EVENT_NS)
    created = root.find("e:System/e:TimeCreated", _EVENT_NS)
    when = created.get("SystemTime") if created is not None else None
    if not record or not record.isdecimal() or not when or len(when) > 64:
        raise ValueError()
    return _digest(["System", "Microsoft-Windows-Kernel-Boot", record, when])


def windows_boot_id():
    """Cold and fast startup count; ordinary hibernation resume does not."""
    if os.name != "nt":
        raise MailBriefError("Windows 부팅 기준을 확인하지 못해 시작 메일 알림을 생략했습니다.")
    query = None
    try:
        import win32evtlog
        query = win32evtlog.EvtQuery(
            "System", win32evtlog.EvtQueryReverseDirection,
            '*[System[Provider[@Name="Microsoft-Windows-Kernel-Boot"] and EventID=27]]')
        events = win32evtlog.EvtNext(query, 32)
        try:
            for event in events:
                identifier = _event_boot_id(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml))
                if identifier:
                    return identifier
        finally:
            for event in events:
                event.Close()
    except Exception:
        pass
    finally:
        if query is not None:
            query.Close()
    raise MailBriefError("Windows 부팅 기준을 확인하지 못해 시작 메일 알림을 생략했습니다.")


class MailBriefService:
    def __init__(self, browser=None, vault=None, boot_provider=None):
        self._browser, self._vault = browser, vault
        self._boot_provider = boot_provider or windows_boot_id
        self._startup_lock = threading.Lock()
        self._lease = None
        self._claimed_boot = None
        self._pending_baselines = {}

    @property
    def browser(self):
        if self._browser is None:
            from core.browser_mail import get_browser_mail_service
            self._browser = get_browser_mail_service()
        return self._browser

    @property
    def vault(self):
        if self._vault is None:
            from core.remote_runtime import SecureTokenVault
            self._vault = SecureTokenVault(str(Path(Config.DB_PATH).with_name("mail_brief")))
        return self._vault

    def _load(self, key):
        try:
            return self.vault.load(_NAMESPACE, key)
        except Exception:
            raise MailBriefError("저장된 메일 집계 기준을 안전하게 열지 못했습니다.") from None

    def _save(self, key, value):
        try:
            self.vault.save(_NAMESPACE, key, value)
        except Exception:
            raise MailBriefError("메일 집계 기준을 암호화해 저장하지 못했습니다.") from None

    def _baseline(self, key):
        value = self._load(key)
        if value is None:
            return None
        if (not isinstance(value, dict) or set(value) != {"version", "seen"}
                or type(value["version"]) is not int or value["version"] != 1
                or not isinstance(value["seen"], dict) or len(value["seen"]) > _LIMIT
                or any(not isinstance(k, str) or not _KEY.fullmatch(k)
                       or not isinstance(v, str) or not _KEY.fullmatch(v)
                       for k, v in value["seen"].items())):
            raise MailBriefError("저장된 메일 집계 기준 형식이 올바르지 않습니다.")
        return value["seen"]

    def collect(self, providers=("naver", "gmail"), *, checkpoint=check_turn_cancelled, progress=None, wait=False):
        if (not isinstance(providers, (tuple, list)) or not 1 <= len(providers) <= 2
                or any(not isinstance(p, str) or p not in _NAMES for p in providers)
                or len(set(providers)) != len(providers) or type(wait) is not bool):
            raise MailBriefError("Gmail 또는 네이버 메일을 선택하세요.")
        workflow_lock = self.browser.workflow_lock
        while True:
            checkpoint()
            acquired = workflow_lock.acquire(timeout=0.25) if wait else workflow_lock.acquire(blocking=False)
            if acquired:
                break
            if not wait:
                raise MailBriefError("다른 메일 작업이 진행 중입니다. 작업이 끝난 뒤 다시 확인하세요.")
        try:
            with self._startup_lock:
                startup_boot = self._claimed_boot
            results, baselines = [], {}
            for provider in providers:
                checkpoint()
                counts = {"provider": provider, "list_count": None, "unread_count": None,
                          "new_count": None, "updated_count": 0, "reply_count": 0,
                          "reply_unknown_count": 0,
                          "filtered_new_count": 0, "draft_count": None,
                          "unknown_count": 0, "partial_count": 0, "error": "",
                          "analysis_error": "", "draft_error": ""}
                if progress is not None:
                    progress({"provider": provider, "phase": "list"})
                try:
                    listing = self.browser.list_inbox(provider, checkpoint=checkpoint)
                    checkpoint()
                    if not isinstance(listing, dict) or listing.get("provider") != provider:
                        raise MailBriefError("메일 목록을 확인하지 못했습니다.")
                    scope = _scope(provider, listing.get("account"))
                    items = listing.get("items")
                    if not isinstance(items, list) or len(items) > 50:
                        raise MailBriefError("메일 목록을 확인하지 못했습니다.")
                    current = {}
                    for item in items:
                        signature = _digest(_metadata(item))
                        if item["message_key"] in current or type(item.get("unread")) is not bool:
                            raise MailBriefError("메일 목록을 확인하지 못했습니다.")
                        current[item["message_key"]] = signature
                    key = "account-" + scope
                    with _LOCK:
                        previous = self._baseline(key)
                    new = set(current) - set(previous or {})
                    updated = {k for k in current if previous is not None and k in previous
                               and previous[k] != current[k]}
                    counts.update(list_count=len(items), unread_count=sum(i["unread"] for i in items),
                                  new_count=None if previous is None else len(new), updated_count=len(updated))
                    analysis = self.browser.analysis
                    try:
                        analysis.collect(provider, listing["account"], items, self.browser.read_message,
                                         checkpoint=checkpoint,
                                         progress=(lambda value, p=provider: progress(
                                             {"provider": p, "phase": "analysis", **value})) if progress else None)
                    except ToolCancelledError:
                        raise
                    except Exception:
                        counts["analysis_error"] = "메일 본문 수집 또는 요약·분류를 끝내지 못했습니다. Chrome 연결과 로컬 AI 상태를 확인하세요."
                    checkpoint()
                    try:
                        records = analysis.current_records(provider, listing["account"], items)
                        settings = analysis.settings()
                        allowed = {c["id"] for c in settings["categories"] if c["notify"]}
                        records = {r["message_key"]: r for r in records if r["message_key"] in current}
                        complete = {k: r for k, r in records.items() if r["status"] == "complete"
                                    and not r["partial"] and r["category_id"] in allowed}
                        counts["reply_count"] = sum(r.get("reply_required") is True for r in complete.values())
                        counts["reply_unknown_count"] = sum(r.get("reply_required") is None for r in complete.values())
                        counts["filtered_new_count"] = len(new & complete.keys()) if previous is not None else 0
                        counts["partial_count"] = sum(r["status"] == "partial" for r in records.values())
                        counts["unknown_count"] = len(items) - sum(
                            r["status"] in ("complete", "partial") for r in records.values())
                    except ToolCancelledError:
                        raise
                    except Exception:
                        counts["analysis_error"] = "메일 요약·분류 기록을 확인하지 못했습니다."
                        counts["unknown_count"] = len(items)
                    checkpoint()
                    try:
                        drafts = self.browser.draft_count(provider, checkpoint=checkpoint)
                        if type(drafts) is not int or drafts < 0:
                            raise ValueError()
                        counts["draft_count"] = drafts
                    except ToolCancelledError:
                        raise
                    except Exception:
                        counts["draft_error"] = "임시보관함 건수를 확인하지 못했습니다."
                    checkpoint()
                    seen = dict(previous or {})
                    for message_key, signature in current.items():
                        seen.pop(message_key, None)
                        seen[message_key] = signature
                    baselines[key] = {"version": 1, "seen": dict(list(seen.items())[-_LIMIT:])}
                    counts["error"] = counts["analysis_error"] or counts["draft_error"]
                except ToolCancelledError:
                    raise
                except Exception as exc:
                    checkpoint()
                    counts["error"] = (str(exc) if isinstance(exc, MailBriefError) else
                                       "받은편지함 수집을 확인하지 못했습니다. Chrome의 로그인·자동 연결 설정을 확인하세요.")
                results.append(counts)
                if progress is not None:
                    progress({"provider": provider, "phase": "completed", **counts})
            checkpoint()
            with self._startup_lock:
                if startup_boot is not None:
                    if self._lease is None or self._claimed_boot != startup_boot:
                        raise ToolCancelledError("시작 메일 확인이 취소되어 집계 기준을 저장하지 않았습니다.")
                    self._pending_baselines.update(baselines)
                else:
                    with _LOCK:
                        for key, value in baselines.items():
                            self._save(key, value)
            return {"providers": results, "summary": self._summary(results)}
        finally:
            workflow_lock.release()

    @staticmethod
    def _summary(results):
        lines = ["메일 확인 결과입니다."]
        for item in results:
            name = _NAMES[item["provider"]]
            if item["list_count"] is None:
                lines.append(f"{name}: {item['error']}")
                continue
            first = item["new_count"] is None
            received = (f"최초 확인 {item['list_count']}건" if first else
                        f"지난 확인 이후 신규 {item['new_count']}건 · 기존 목록 변경 {item['updated_count']}건")
            selected = "" if first else f" · 알림 선택 카테고리의 신규 {item['filtered_new_count']}건"
            drafts = ("임시보관함 확인 실패" if item["draft_count"] is None
                      else f"임시보관함 {item['draft_count']}건(미발송 초안)")
            lines.append(f"{name}: {received}{selected} · 선택한 알림 카테고리의 회신·자료 전달 검토 {item['reply_count']}건(AI 추정) · {drafts}.")
            if item["unknown_count"] or item["partial_count"]:
                lines.append(f"미분류 {item['unknown_count']}건 · 부분 요약 {item['partial_count']}건은 답장 검토 집계에서 제외했습니다.")
            if item["reply_unknown_count"]:
                lines.append(f"선택한 알림 카테고리에서 회신·자료 전달 필요 여부 미확인 {item['reply_unknown_count']}건은 검토 집계에서 제외했습니다.")
            if item["analysis_error"]:
                lines.append(item["analysis_error"])
        lines.append("현재 받은편지함에서 최대 50건씩 확인했습니다. Gmail은 대화 한 건을 한 건으로 셉니다. 임시보관함 초안이 모두 발송 필요를 뜻하지는 않습니다.")
        return "\n".join(lines)

    def claim_startup(self):
        with self._startup_lock:
            if self._lease is not None:
                return None
            try:
                boot_id = self._boot_provider()
                if (not isinstance(boot_id, str) or not 1 <= len(boot_id) <= 256
                        or any(ord(c) < 32 or ord(c) == 127 for c in boot_id)):
                    raise ValueError()
            except Exception:
                raise MailBriefError("Windows 부팅 기준을 확인하지 못해 시작 메일 알림을 생략했습니다.") from None
            handle = None
            try:
                import msvcrt
                directory = Path(self.vault.directory)
                directory.mkdir(parents=True, exist_ok=True)
                handle = (directory / ".startup.lock").open("a+b")
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    handle.close()
                    return None
                record = self._load("startup")
                if record is not None and (not isinstance(record, dict) or set(record) != {"version", "boot_id"}
                        or type(record["version"]) is not int or record["version"] != 1
                        or not isinstance(record["boot_id"], str) or not 1 <= len(record["boot_id"]) <= 256):
                    raise MailBriefError("저장된 시작 메일 알림 기준 형식이 올바르지 않습니다.")
                if record is not None and record["boot_id"] == boot_id:
                    handle.close()
                    return None
                self._lease, self._claimed_boot = handle, boot_id
                self._pending_baselines.clear()
                return boot_id
            except Exception as exc:
                if handle is not None:
                    handle.close()
                if isinstance(exc, MailBriefError):
                    raise
                raise MailBriefError("시작 메일 알림의 중복 실행 방지를 확인하지 못했습니다.") from None

    def finish_startup(self, boot_id, delivered=True):
        with self._startup_lock:
            if self._lease is None or boot_id != self._claimed_boot or type(delivered) is not bool:
                raise MailBriefError("시작 메일 알림 실행 기준이 변경되었습니다.")
            try:
                if delivered:
                    with _LOCK:
                        for key, value in self._pending_baselines.items():
                            self._save(key, value)
                        self._save("startup", {"version": 1, "boot_id": boot_id})
            finally:
                self._clear_startup()

    def _clear_startup(self):
        if self._lease is not None:
            self._lease.close()
        self._lease, self._claimed_boot = None, None
        self._pending_baselines.clear()

    def cancel_startup(self):
        with self._startup_lock:
            self._clear_startup()


_SERVICE = None


def get_mail_brief_service():
    global _SERVICE
    with _LOCK:
        if _SERVICE is None:
            _SERVICE = MailBriefService()
        return _SERVICE
