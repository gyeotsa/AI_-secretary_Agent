import sqlite3
import time

from core.knowledge_memory import EpistemicStatus, KnowledgeMemoryStore, KnowledgeRecord, MemoryKind
from core.memory_pipeline import MemoryEventPipeline


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


def test_approved_result_audit_does_not_duplicate_indexed_memory(tmp_path):
    pipeline = MemoryEventPipeline(tmp_path / "events.db")
    pipeline.record_approved_result(workspace="mockup", instruction="저장",
                                    result={"renderer": "v4"}, needs_consolidation=False)
    assert pipeline.pending_count() == 0


def test_retrieval_usage_tracks_inclusion(tmp_path):
    pipeline = MemoryEventPipeline(tmp_path / "events.db")
    trace = pipeline.record_retrieval("질문", [{"chunk_id": "c1", "source": "note.md"}])
    pipeline.mark_included(trace, ["c1"])
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
