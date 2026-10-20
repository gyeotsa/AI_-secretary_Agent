"""Personal knowledge may contain user code, but never the application's own corpus."""
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3

import pytest

from config import Config
from core.knowledge_memory import EpistemicStatus, KnowledgeRecord, MemoryKind
from core.knowledge_sources import is_internal_knowledge, is_internal_path
from core.obsidian_vault import ObsidianVault, ObsidianVaultError
from core.project_indexer import ProjectIndexer
from core.rag import VectorRAGManager


APP_ROOT = Path(__file__).resolve().parent


@pytest.fixture
def rag(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    monkeypatch.setattr(VectorRAGManager, "_init_vector_rag", lambda self: None)
    return VectorRAGManager()


def test_source_boundary_uses_provenance_and_keeps_personal_code(tmp_path):
    personal = tmp_path / "my-project" / "README.md"
    assert is_internal_path(APP_ROOT / "README.md")
    assert is_internal_path((APP_ROOT / "core" / "rag.py").as_uri())
    assert not is_internal_path(personal)
    assert not is_internal_path("manual")
    assert is_internal_path("file://[broken")
    assert not is_internal_path(f"'{personal}'")
    assert not is_internal_path("https://example.com/anis")
    assert is_internal_knowledge("memory://old", {"source_metadata": {"source_path": str(APP_ROOT)}})
    assert is_internal_knowledge("memory://old", {"evidence": [{"source_path": str(APP_ROOT)}]})
    assert not is_internal_knowledge("memory://personal", {"subject": "Anis 사용 선호"})


def test_personal_runtime_vault_is_allowed_but_runtime_diagnostics_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(APP_ROOT / "data" / "assistant.db"))
    assert not is_internal_path(APP_ROOT / "data" / "AnisKnowledgeVault" / "wiki" / "personal.md")
    assert not is_internal_path(APP_ROOT / "data" / "research_reports" / "personal.md")
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    assert is_internal_path(tmp_path / "diagnostics" / "runtime.md")
    assert not is_internal_path(tmp_path / "research_reports" / "my-research.md")


def test_rag_blocks_ingestion_and_legacy_results_without_deleting_sources(rag, tmp_path):
    personal = tmp_path / "README.md"
    personal.write_text("개인 프로젝트 Anis 사용 안내", encoding="utf-8")
    assert "성공적으로 추가" in rag.add_document(str(personal))
    assert rag.add_document(str(APP_ROOT / "README.md")).startswith("오류:")
    with pytest.raises(ValueError):
        rag.add_text_document("개발 문서", doc_id="denied", source_uri=(APP_ROOT / "core" / "rag.py").as_uri())
    rag.documents["global::legacy-dev"] = {
        "doc_id": "legacy-dev", "namespace": "global", "source": str(APP_ROOT / "README.md"),
        "chunks": ["개인 개발 문서"], "chunk_metadata": [],
    }
    before = (APP_ROOT / "README.md").stat().st_mtime_ns
    results = rag.search_docs("개인")
    assert results and all(item["source"] == str(personal) for item in results)
    assert "legacy-dev" not in rag.list_documents()
    assert "global::legacy-dev" in rag.documents
    assert (APP_ROOT / "README.md").stat().st_mtime_ns == before


def test_legacy_chroma_provenance_is_filtered_before_reranker(rag):
    rag.documents["global::legacy"] = {
        "doc_id": "legacy", "source": "memory://old", "namespace": "global", "chunks": ["개발 내용"],
        "metadata": {"source_path": str(APP_ROOT / "core" / "rag.py")},
        "chunk_metadata": [{"chunk_id": "blocked", "content": "개발 내용"}],
    }
    rag.use_vector_rag = True
    rag.collection = SimpleNamespace(query=lambda **kwargs: {
        "documents": [["개발 내용", "개인 작업"]], "distances": [[0, 0]],
        "metadatas": [[{"chunk_id": "blocked", "source": "memory://old"},
                        {"chunk_id": "personal", "source": "memory://personal", "namespace": "global"}]],
    })
    seen = []
    rag.reranker = SimpleNamespace(predict=lambda pairs: seen.extend(pairs) or [1] * len(pairs))
    assert [item["content"] for item in rag.search_docs("개인")] == ["개인 작업"]
    assert seen == [("개인", "개인 작업")]
    seen.clear()
    assert [item["content"] for item in rag._vector_search("개인", 3)] == ["개인 작업"]
    assert seen == [("개인", "개인 작업")]


def test_legacy_obsidian_rag_recovers_original_note_provenance(rag, tmp_path):
    note = tmp_path / "vault" / "developer.md"
    note.parent.mkdir()
    text = f'---\nsource_uri: "{(APP_ROOT / "README.md").as_posix()}"\n---\n# 개발 원문\nlegacy developer content'
    note.write_text(text, encoding="utf-8")
    rag.documents["global::old-vault"] = {
        "doc_id": "old-vault", "namespace": "global", "source": str(note),
        "metadata": {"source_type": "obsidian"}, "chunks": [text],
        "chunk_metadata": [{"chunk_id": "old-vault", "content": text, "source_type": "obsidian"}],
    }
    assert rag.search_docs("developer") == []
    assert "old-vault" not in rag.list_documents()
    rag.add_text_document("개인 사용자 지식", doc_id="personal")
    rag.use_vector_rag = True
    rag.collection = SimpleNamespace(query=lambda **kwargs: {
        "documents": [["legacy developer tail without frontmatter"]], "distances": [[0]],
        "metadatas": [[{"chunk_id": "chroma-only", "source": str(note), "source_type": "obsidian"}]],
    })
    rag.reranker = SimpleNamespace(predict=lambda pairs: pytest.fail("internal source reached reranker"))
    assert rag.search_docs("developer") == []
    assert note.read_text(encoding="utf-8") == text


def test_tool_catalog_evidence_and_failed_add_cannot_bypass_boundary(rag, tmp_path):
    from core.tools import ToolExecutor
    personal = tmp_path / "README.md"
    personal.write_text("개인 안내", encoding="utf-8")
    rag.add_document(str(personal))
    rag.documents["global::internal"] = {"doc_id": "internal", "source": str(APP_ROOT / "README.md")}
    tools = ToolExecutor.__new__(ToolExecutor)
    tools._rag_manager = rag
    tools.safety = SimpleNamespace(validate_path=lambda path: (True, ""))
    assert not tools.add_document(str(APP_ROOT / "README.md")).succeeded
    result = tools.list_documents()
    assert result.succeeded and result.evidence[0].data["documents"] == ["README.md"]


def test_project_index_blocks_app_and_legacy_rows_but_keeps_user_project(tmp_path):
    personal = tmp_path / "my-project"
    personal.mkdir()
    source = personal / "app.py"
    source.write_text('def personal_work():\n    """Anis 사용자의 프로젝트."""\n    return 1\n', encoding="utf-8")
    indexer = ProjectIndexer(str(tmp_path / "index.db"))
    assert indexer.set_project_root(str(personal))
    indexer.sync_changes()
    assert indexer.search_symbols("personal_work")
    assert indexer.get_file_info(str(source))
    assert not indexer.set_project_root(str(APP_ROOT))
    assert indexer.project_root is None
    indexer.project_root = str(APP_ROOT)  # Simulate state restored by an older version.
    assert indexer.search_files("README") == indexer.search_symbols("Vector") == []
    assert indexer.get_file_info(str(APP_ROOT / "core" / "rag.py")) is None
    assert indexer.get_file_tree()["children"] == []
    assert indexer.sync_changes() == {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}


def test_fresh_vault_has_no_personal_nodes_or_rag_scaffolds(rag, tmp_path):
    vault = ObsidianVault(tmp_path / "vault", rag=rag, settings_path=tmp_path / "vault-settings.json")
    assert vault.build_graph()["nodes"] == []
    assert vault.sync_to_rag() == {"indexed": 0, "unchanged": 0, "removed": 0, "errors": []}
    assert rag.documents == {}


def test_saved_internal_vault_location_falls_back_without_writing_app_tree(rag, tmp_path):
    settings = tmp_path / "vault-settings.json"
    settings.write_text(json.dumps({"vault_path": str(APP_ROOT)}), encoding="utf-8")
    vault = ObsidianVault(rag=rag, settings_path=settings)
    assert vault.root == tmp_path / "AnisKnowledgeVault"
    assert vault.build_graph()["nodes"] == []
    with pytest.raises(ObsidianVaultError):
        vault.configure(APP_ROOT)
    with pytest.raises(ObsidianVaultError):
        ObsidianVault(APP_ROOT, settings_path=settings)


def test_vault_hides_developer_provenance_and_preserves_user_notes(rag, tmp_path, monkeypatch):
    vault = ObsidianVault(tmp_path / "vault", rag=rag, settings_path=tmp_path / "vault-settings.json")
    record = KnowledgeRecord(content="Anis에서 내 프로젝트를 정리해", kind=MemoryKind.PROJECT,
                             subject="개인 프로젝트", epistemic_status=EpistemicStatus.USER_CLAIM,
                             source_label="사용자 발화", workspace_namespace="my-project")
    personal = vault.upsert_record(record)
    legacy = vault.root / "wiki" / "facts" / "developer.md"
    legacy.write_text(vault._frontmatter({"source_uri": str(APP_ROOT / "README.md")})
                      + "\n# 개발 문서\n개발 본문", encoding="utf-8")
    personal.write_text(personal.read_text(encoding="utf-8") + "\n[[facts/developer]]\n", encoding="utf-8")
    assert legacy not in vault._notes()
    assert all(item["path"] != str(legacy) for item in vault.explore("개인 프로젝트", depth=3))
    assert all(node["relative_path"] != "wiki/facts/developer.md" for node in vault.build_graph()["nodes"])
    with pytest.raises(ObsidianVaultError):
        vault.read_note("wiki/facts/developer.md")
    monkeypatch.setattr("os.startfile", lambda uri: pytest.fail("developer note was opened"))
    with pytest.raises(ObsidianVaultError):
        vault.open_note("wiki/facts/developer.md")
    opened = []
    monkeypatch.setattr("os.startfile", opened.append)
    vault.open_note("wiki/actions/decision.md")
    assert len(opened) == 1
    rag.set_namespace("some-other-project")
    assert not vault.sync_to_rag()["errors"]
    note_docs = [doc for doc in rag.documents.values() if doc["source"] == str(personal)]
    assert len(note_docs) == 1 and note_docs[0]["namespace"] == "my-project"
    assert legacy.exists() and "Anis" in vault.read_note(personal.relative_to(vault.root))["body"]
    mixed_map = vault.root / "wiki" / "topics" / "mixed.md"
    personal_link = personal.relative_to(vault.root / "wiki").with_suffix("").as_posix()
    mixed_map.write_text(vault._frontmatter({"managed_by": "anis", "type": "topics_map"})
                         + f"\n# 지도\n[[{personal_link}]]\n[[facts/developer]]\n", encoding="utf-8")
    assert "facts/developer" not in vault.read_note(mixed_map.relative_to(vault.root))["content"]
    with pytest.raises(ObsidianVaultError):
        vault.open_note(mixed_map.relative_to(vault.root))
    record.source_uri = str(APP_ROOT / "core" / "rag.py")
    with pytest.raises(ObsidianVaultError):
        vault.upsert_record(record)


def test_vault_rag_opt_out_and_legacy_developer_sync_are_removed_only_from_index(rag, tmp_path):
    vault = ObsidianVault(tmp_path / "vault", rag=rag, settings_path=tmp_path / "vault-settings.json")
    note = vault.root / "wiki" / "facts" / "personal.md"
    note.write_text("# 개인 작업\n사용자 내용", encoding="utf-8")
    assert vault.sync_to_rag()["indexed"] == 1
    note.write_text("---\nrag_index: false\n---\n# 개인 작업\n사용자 내용", encoding="utf-8")
    assert vault.sync_to_rag()["removed"] == 1
    assert note.exists() and rag.documents == {}
    note.write_text(vault._frontmatter({"source_uri": str(APP_ROOT / "README.md")}) + "\n개발 자료", encoding="utf-8")
    assert vault.sync_to_rag()["indexed"] == 0
    assert note.exists()


def test_single_quoted_personal_note_sources_are_preserved(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "vault-settings.json")
    note = vault.root / "wiki" / "facts" / "quoted.md"
    source = (tmp_path / "personal.md").as_posix()
    note.write_text(f"---\nsource_uri: '{source}'\n---\n# 개인 노트\nAnis 사용 선호", encoding="utf-8")
    assert vault.read_note(note.relative_to(vault.root))["metadata"]["source_uri"] == source
    assert note in vault._notes()


def test_typed_and_legacy_memory_contexts_filter_existing_internal_records(tmp_path, monkeypatch):
    from core.knowledge_memory import MemoryPolicyError
    from core.memory import SemanticMemory, SemanticMemoryManager, build_memory_context, build_relevant_knowledge_context
    manager = SemanticMemoryManager(str(tmp_path / "semantic.db"))
    store = manager.knowledge_store
    for key, content in (("personal", "Anis 개인 작업 선호"), ("internal", "Anis forbidden developer marker")):
        manager.add_memory(SemanticMemory(key=key, category="사용자_프로필", content=content))
    provenance = {"source_path": str(APP_ROOT / "core" / "rag.py")}
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE knowledge_records SET source_uri=?, metadata=? WHERE subject='internal'",
                   (str(APP_ROOT / "core" / "rag.py"), json.dumps(provenance)))
        internal_id = db.execute("SELECT record_id FROM knowledge_records WHERE subject='internal'").fetchone()[0]
    with sqlite3.connect(manager.db_path) as db:
        db.execute("UPDATE semantic_memory SET metadata=? WHERE key='internal'", (json.dumps(provenance),))
    assert store.get(internal_id) is None
    assert [r.subject for r in store.search("Anis")] == ["personal"]
    assert manager.get_memory("internal") is None
    assert [r.key for r in manager.search_memories("Anis")] == ["personal"]
    assert [r.key for r in manager.get_memories_by_category("사용자_프로필")] == ["personal"]
    monkeypatch.setattr("core.knowledge_memory._store", store)
    monkeypatch.setattr("core.memory.get_episode_memory", lambda: SimpleNamespace(get_session_episodes=lambda *a, **kw: []))
    monkeypatch.setattr("core.memory.get_semantic_memory", lambda: manager)
    monkeypatch.setattr("core.memory.get_memory", lambda: SimpleNamespace(workspace_namespace="global"))
    for context in (build_memory_context("personal", query="Anis"), build_relevant_knowledge_context("Anis")):
        assert "개인 작업 선호" in context and "forbidden developer marker" not in context
    forbidden = KnowledgeRecord(content="개발 내용", kind=MemoryKind.FACT, subject="개발 자료", source_uri=str(APP_ROOT))
    with pytest.raises(MemoryPolicyError):
        store.remember(forbidden, automatic=False)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_records").fetchone()[0] == 2
    approved = dict(content="Anis forbidden developer marker", kind=MemoryKind.FACT, subject="internal",
                    source_label="사용자가 직접 확인한 내용")
    personal_id = store.remember(KnowledgeRecord(**approved))
    assert personal_id != internal_id
    assert store.remember(KnowledgeRecord(**approved)) == personal_id
