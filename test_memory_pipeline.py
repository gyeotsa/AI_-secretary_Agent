import copy
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from core.knowledge_memory import EpistemicStatus, KnowledgeMemoryStore, KnowledgeRecord, MemoryKind
from core.memory_pipeline import MemoryEventPipeline
from core.rag import VectorRAGManager


class FakeConsolidator:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def should_consider(self, text):
        return "기억" in text or "항상" in text

    def consolidate(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.fail:
            raise RuntimeError("temporary")
        return ["memory-1"]


def test_exchange_capture_is_cheap_and_habits_require_repetition(tmp_path):
    pipeline = MemoryEventPipeline(tmp_path / "events.db")
    for session in ("s1", "s2", "s2"):
        pipeline.record_exchange(session_id=session, workspace="global",
                                 user_text="보고서를 생성해줘",
                                 assistant_text="[DEBUG] hidden\n완료했습니다")
    assert pipeline.pending_count() == 3
    habits = pipeline.habits(minimum_count=3, minimum_sessions=2)
    assert len(habits) == 1 and habits[0]["session_count"] == 2
    with sqlite3.connect(pipeline.db_path) as db:
        assistant = db.execute("SELECT assistant_text FROM memory_events LIMIT 1").fetchone()[0]
    assert "DEBUG" not in assistant and assistant == "완료했습니다"


def test_idle_consolidation_retries_failed_events(tmp_path):
    pipeline = MemoryEventPipeline(tmp_path / "events.db")
    pipeline.record_exchange(session_id="s", workspace="w",
                             user_text="항상 기억해", assistant_text="응")
    failed = pipeline.consolidate_pending(FakeConsolidator(fail=True))
    assert failed["processed"] == 0 and pipeline.pending_count() == 1
    completed = pipeline.consolidate_pending(FakeConsolidator())
    assert completed["processed"] == 1 and pipeline.pending_count() == 0


def test_deleted_chat_events_cannot_be_consolidated_again(tmp_path):
    pipeline = MemoryEventPipeline(tmp_path / "events.db")
    pipeline.record_exchange(session_id="deleted", workspace="w",
                             user_text="항상 기억해", assistant_text="지난 답변")
    pipeline.consolidate_pending(FakeConsolidator())
    pipeline.record_exchange(session_id="deleted", workspace="w",
                             user_text="앞으로 기억해", assistant_text="아직 처리 전")
    pipeline.record_exchange(session_id="kept", workspace="w",
                             user_text="다른 대화를 항상 기억해", assistant_text="유지")

    assert pipeline.delete_session("deleted") == 2
    assert pipeline.delete_session("deleted") == 0
    consolidator = FakeConsolidator()
    pipeline.consolidate_pending(consolidator)
    assert [call[1]["session_id"] for call in consolidator.calls] == ["kept"]
    with sqlite3.connect(pipeline.db_path) as db:
        assert db.execute("SELECT DISTINCT session_id FROM memory_events").fetchall() == [("kept",)]


def test_approved_result_audit_does_not_duplicate_indexed_memory(tmp_path):
    pipeline = MemoryEventPipeline(tmp_path / "events.db")
    pipeline.record_approved_result(workspace="mockup", instruction="저장",
                                    result={"renderer": "v4"}, needs_consolidation=False)
    assert pipeline.pending_count() == 0


def test_retrieval_usage_tracks_inclusion(tmp_path):
    pipeline = MemoryEventPipeline(tmp_path / "events.db")
    trace = pipeline.record_retrieval("질문", [{"chunk_id": "c1", "source": "note.md"}])
    pipeline.mark_included(trace, ["c1"])
    # 검색 결과를 prompt에 넣었다는 사실과 최종 답변이 실제 근거로 사용했다는
    # 사실은 서로 다른 품질 지표다. 사용 여부는 답변 합성기가 명시적으로
    # 확인한 뒤에만 기록한다.
    assert pipeline.retrieval_summary() == {"total": 1, "included": 1, "used": 0}
    pipeline.mark_used(trace, ["c1"])
    assert pipeline.retrieval_summary() == {"total": 1, "included": 1, "used": 1}


def test_memory_lifecycle_expires_stale_records_and_reports_conflicts(tmp_path):
    store = KnowledgeMemoryStore(tmp_path / "knowledge.db")
    old = KnowledgeRecord(content="오래된 정보", kind=MemoryKind.FACT, subject="날씨",
                          predicate="is", epistemic_status=EpistemicStatus.FACT,
                          source_label="test", expires_at=time.time() - 1)
    store.remember(old)
    for content in ("A", "B"):
        store.remember(KnowledgeRecord(content=content, kind=MemoryKind.PREFERENCE,
                                       subject="말투", predicate="prefers", source_label="user"))
    report = store.maintain_lifecycle()
    assert old.record_id in report["expired_ids"]
    assert report["conflicts"]
    assert store.get(old.record_id).status == "expired"


def _simple_rag(tmp_path):
    manager = VectorRAGManager.__new__(VectorRAGManager)
    manager.data_dir = str(tmp_path)
    manager.rag_file = str(tmp_path / "simple_rag.json")
    manager.documents = {}
    manager.namespace = "global"
    manager._store_lock = threading.RLock()
    manager.usage_tracker = None
    manager.last_retrieval_trace = ""
    manager._retrieval_local = threading.local()
    manager.use_vector_rag = False
    manager.reranker = None
    return manager


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_rag_save_retries_only_bounded_windows_replace_conflicts(tmp_path, monkeypatch, winerror):
    import core.rag as rag_module

    manager = _simple_rag(tmp_path)
    manager.documents = {"global::record": {"chunks": ["기록"]}}
    replace, attempts, delays = rag_module.os.replace, [], []
    def temporarily_locked(source, destination):
        attempts.append(destination)
        if len(attempts) < 3:
            error = PermissionError("synthetic file lock")
            error.winerror = winerror
            raise error
        replace(source, destination)
    monkeypatch.setattr(rag_module.os, "replace", temporarily_locked)
    monkeypatch.setattr(rag_module.time, "sleep", delays.append)
    manager._save()
    assert len(attempts) == 3 and delays == [0.05, 0.1]
    assert json.loads(Path(manager.rag_file).read_text(encoding="utf-8")) == manager.documents
    assert not list(tmp_path.glob("simple_rag.json.tmp.*"))


@pytest.mark.parametrize("operation", ["add_file", "add_text", "add_new_text", "remove_file", "remove_text", "revalidate"])
def test_rag_failed_persistence_preserves_memory_and_disk_for_retry(tmp_path, monkeypatch, operation):
    import core.rag as rag_module

    manager = _simple_rag(tmp_path)
    manager.add_text_document("기존 자료의 검증된 내용입니다.", doc_id="record", namespace="workspace",
                              metadata={"source_type": "web", "expires_at": time.time()+60})
    original = copy.deepcopy(manager.documents)
    source = tmp_path / "record"
    source.write_text("새롭게 작성한 내용입니다.", encoding="utf-8")
    replace, attempts = rag_module.os.replace, []
    def locked(*args):
        attempts.append(True)
        error = PermissionError("synthetic persistent lock")
        error.winerror = 5
        raise error
    monkeypatch.setattr(rag_module.os, "replace", locked)
    monkeypatch.setattr(rag_module.time, "sleep", lambda _: None)
    operations = {
        "add_file": lambda: manager.add_document(str(source), namespace="workspace"),
        "add_text": lambda: manager.add_text_document("수정한 내용입니다.", doc_id="record", namespace="workspace"),
        "add_new_text": lambda: manager.add_text_document("새 문서입니다.", doc_id="new", namespace="workspace"),
        "remove_file": lambda: manager.remove_document("record", namespace="workspace"),
        "remove_text": lambda: manager.remove_text_document("record", namespace="workspace"),
        "revalidate": lambda: manager.revalidate_document("record", namespace="workspace", verified_at=123456789),
    }
    if operation == "add_file":
        assert "문서 추가 오류" in operations[operation]()
    else:
        with pytest.raises(PermissionError):
            operations[operation]()
    assert len(attempts) == 3 and manager.documents == original
    assert json.loads(Path(manager.rag_file).read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob("simple_rag.json.tmp.*"))
    monkeypatch.setattr(rag_module.os, "replace", replace)
    assert operations[operation]()
    assert json.loads(Path(manager.rag_file).read_text(encoding="utf-8")) == manager.documents != original


def test_rag_save_does_not_retry_unrelated_write_errors(tmp_path, monkeypatch):
    import core.rag as rag_module

    manager, attempts = _simple_rag(tmp_path), []
    def failed(*args):
        attempts.append(True)
        raise OSError("synthetic storage failure")
    monkeypatch.setattr(rag_module.os, "replace", failed)
    with pytest.raises(OSError):
        manager._save()
    assert len(attempts) == 1 and not list(tmp_path.glob("simple_rag.json.tmp.*"))


def test_concurrent_workspace_rag_operations_keep_namespaces_isolated(tmp_path):
    manager = _simple_rag(tmp_path)
    barrier = threading.Barrier(2)
    observed = {}

    def worker(namespace, content):
        manager.add_text_document(
            content, doc_id="same-id", namespace=namespace,
            metadata={"approved": True},
        )
        barrier.wait(timeout=2)
        observed[namespace] = manager.search_docs(
            "공통검색어", namespace=namespace, top_k=5,
        )

    first = threading.Thread(
        target=worker, args=("project-a:specialist:document", "공통검색어 A전용"),
    )
    second = threading.Thread(
        target=worker, args=("project-b:specialist:document", "공통검색어 B전용"),
    )
    first.start(); second.start()
    first.join(3); second.join(3)

    assert not first.is_alive() and not second.is_alive()
    assert manager.namespace == "global"
    assert [item["content"] for item in observed["project-a:specialist:document"]] == [
        "공통검색어 A전용"
    ]
    assert [item["content"] for item in observed["project-b:specialist:document"]] == [
        "공통검색어 B전용"
    ]
    assert (tmp_path / "simple_rag.json").is_file()
