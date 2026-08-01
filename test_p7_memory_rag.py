import time

import pytest

from core.knowledge_memory import (
    EpistemicStatus, KnowledgeMemoryStore, KnowledgeRecord, MemoryKind,
    MemoryPolicyError,
)
from core.rag import VectorRAGManager


def memory_store(tmp_path):
    return KnowledgeMemoryStore(str(tmp_path / "knowledge.db"))


def test_memory_kinds_epistemic_source_and_metadata_filter(tmp_path):
    store = memory_store(tmp_path)
    record = KnowledgeRecord(
        content="프로젝트는 Python 3.12를 사용한다.", kind=MemoryKind.PROJECT,
        subject="Jarvis", predicate="python_version", epistemic_status=EpistemicStatus.FACT,
        source_uri="file:///README.md", workspace_namespace="ws-a", confidence=0.9,
        metadata={"branch": "main"},
    )
    record_id = store.remember(record)
    assert store.get(record_id).recorded_at > 0
    found = store.search("Python", kinds=[MemoryKind.PROJECT], workspace_namespace="ws-a",
                         metadata_filter={"branch": "main"})
    assert [item.record_id for item in found] == [record_id]
    assert store.search("Python", workspace_namespace="ws-b") == []
    with pytest.raises(MemoryPolicyError, match="출처"):
        store.remember(KnowledgeRecord(content="추측", kind=MemoryKind.FACT,
            subject="x", epistemic_status=EpistemicStatus.INFERENCE))


def test_sensitive_information_is_not_automatically_remembered(tmp_path):
    store = memory_store(tmp_path)
    with pytest.raises(MemoryPolicyError, match="민감 정보"):
        store.remember(KnowledgeRecord(content="비밀번호는 abc123입니다", kind=MemoryKind.PREFERENCE,
                                       subject="credential"), automatic=True)
    assert store.search(workspace_namespace="global") == []


def test_user_correction_supersedes_contradictory_active_records(tmp_path):
    store = memory_store(tmp_path)
    old_id = store.remember(KnowledgeRecord(content="파란색", kind=MemoryKind.PREFERENCE,
        subject="사용자", predicate="favorite_color"))
    new_id = store.correct(subject="사용자", predicate="favorite_color", content="초록색")
    assert store.get(old_id).status == "superseded"
    assert store.get(new_id).supersedes_id == old_id
    assert [item.content for item in store.search("사용자")] == ["초록색"]


def test_unresolved_contradictions_are_preserved_until_user_correction(tmp_path):
    store = memory_store(tmp_path)
    first = KnowledgeRecord(content="서울", kind=MemoryKind.FACT, subject="회의", predicate="location",
                            epistemic_status=EpistemicStatus.FACT, source_label="문서 A")
    second = KnowledgeRecord(content="부산", kind=MemoryKind.FACT, subject="회의", predicate="location",
                             epistemic_status=EpistemicStatus.FACT, source_label="문서 B")
    store.remember(first)
    store.remember(second)
    assert second.record_id in store.get(first.record_id).metadata["conflicts_with"]
    assert first.record_id in store.get(second.record_id).metadata["conflicts_with"]
    assert {item.content for item in store.search("회의")} == {"서울", "부산"}


@pytest.fixture
def rag(tmp_path, monkeypatch):
    monkeypatch.setattr("core.rag.Config.DB_PATH", str(tmp_path / "jarvis.db"))
    def no_vector(self):
        self.use_vector_rag = False
        self.vector_db = self.embedding_model = self.reranker = None
    monkeypatch.setattr(VectorRAGManager, "_init_vector_rag", no_vector)
    return VectorRAGManager()


def test_structure_preserving_chunking_and_claim_evidence_link(rag, tmp_path):
    document = tmp_path / "guide.md"
    document.write_text("# 설치\nPython 설치 방법입니다.\n\n# 실행\npytest로 검증합니다.", encoding="utf-8")
    assert "성공적으로 추가" in rag.add_document(str(document), {"team": "runtime"})
    results = rag.search_docs("pytest 검증", metadata_filter={"team": "runtime"})
    assert results and results[0]["section"] == "실행"
    assert results[0]["chunk_id"]
    assert results[0]["citation"]["source"] == str(document)
    assert results[0]["citation"]["start_line"] > 0


def test_modified_and_deleted_documents_are_synchronized(rag, tmp_path):
    document = tmp_path / "sync.txt"
    document.write_text("첫 번째 내용", encoding="utf-8")
    rag.sync_document(str(document))
    first_hash = rag.documents[rag._document_key("sync.txt")]["content_sha256"]
    document.write_text("두 번째 변경 내용", encoding="utf-8")
    rag.sync_document(str(document))
    assert rag.documents[rag._document_key("sync.txt")]["content_sha256"] != first_hash
    document.unlink()
    assert rag.sync_document(str(document)) == "삭제 동기화 완료"
    assert rag._document_key("sync.txt") not in rag.documents


def test_search_automatically_synchronizes_local_source_changes(rag, tmp_path):
    document = tmp_path / "automatic.txt"
    document.write_text("이전 키워드", encoding="utf-8")
    rag.add_document(str(document))
    document.write_text("새로운 검색어", encoding="utf-8")
    assert rag.search_docs("새로운 검색어")[0]["source"] == str(document)
    document.unlink()
    assert rag.search_docs("새로운 검색어") == []


def test_web_expiry_requires_revalidation_and_reranking(rag, tmp_path):
    stale = tmp_path / "stale.txt"
    stale.write_text("오늘 날씨는 맑음", encoding="utf-8")
    rag.add_document(str(stale), {"source_type": "web", "expires_at": time.time() - 1, "topic": "weather"})
    assert rag.search_docs("오늘 날씨", metadata_filter={"topic": "weather"}) == []
    stale_results = rag.search_docs("오늘 날씨", metadata_filter={"topic": "weather"}, include_stale=True)
    assert stale_results[0]["stale"] is True
    assert stale_results[0]["needs_revalidation"] is True

    fresh = tmp_path / "fresh.txt"
    fresh.write_text("오늘 날씨 오늘 날씨 최신 관측", encoding="utf-8")
    rag.add_document(str(fresh), {"source_type": "web", "expires_at": time.time() + 60, "topic": "weather"})
    assert rag.search_docs("오늘 날씨", metadata_filter={"topic": "weather"})[0]["source"] == str(fresh)


def test_web_default_ttl_and_explicit_revalidation(rag, tmp_path):
    document = tmp_path / "web.txt"
    document.write_text("새 소프트웨어 정보", encoding="utf-8")
    rag.add_document(str(document), {"source_type": "web", "topic": "software"})
    stored = rag.documents[rag._document_key("web.txt")]
    assert stored["metadata"]["expires_at"] > stored["metadata"]["recorded_at"]
    old_expiry = stored["metadata"]["expires_at"]
    assert rag.revalidate_document("web.txt", verified_at=old_expiry + 1, ttl_seconds=60)
    assert stored["metadata"]["expires_at"] == old_expiry + 61
