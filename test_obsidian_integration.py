from pathlib import Path

from core.knowledge_memory import EpistemicStatus, KnowledgeRecord, MemoryKind
from core.obsidian_vault import ObsidianVault, ObsidianVaultError
from plugins.obsidian import ObsidianPlugin


def _record(record_id="abc12345", subject="응답 말투", content="앞으로 자연스러운 반말을 사용해"):
    return KnowledgeRecord(
        record_id=record_id, content=content, kind=MemoryKind.PREFERENCE,
        subject=subject, predicate="prefers", epistemic_status=EpistemicStatus.USER_CLAIM,
        source_label="사용자 발화", confidence=0.95,
    )


def test_vault_creates_atomic_note_maps_and_graph_exploration(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    note = vault.upsert_record(_record())
    content = note.read_text(encoding="utf-8")
    assert "importance:" in content and "[[actions/preference|preference]]" in content
    assert list((vault.root / "wiki" / "topics").glob("*.md"))
    results = vault.explore("응답 말투", depth=2)
    assert any(item["path"] == str(note) for item in results)


def test_raw_conversations_are_archived_but_not_active_notes(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    raw = vault.archive_exchange("session/unsafe", "안녕", "반가워")
    assert raw.exists() and "rag_index: false" in raw.read_text(encoding="utf-8")
    assert raw not in vault._notes(include_raw=False)


def test_repeated_requests_are_promoted_without_indexing_every_chat(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    for session in ("s1", "s2", "s3"):
        vault.archive_exchange(session, "매출 보고서를 만들어줘", "확인할게")
    patterns = list((vault.root / "wiki" / "patterns").glob("action-*.md"))
    assert len(patterns) == 1
    content = patterns[0].read_text(encoding="utf-8")
    assert "count: 3" in content and "session_count: 3" in content


def test_sensitive_memory_is_not_exported(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    record = _record(content="내 API key는 sk_abcdefghijklmnop 이야")
    try:
        vault.upsert_record(record)
    except ObsidianVaultError:
        pass
    else:
        raise AssertionError("sensitive memory was written")


def test_vault_sync_only_indexes_wiki(tmp_path):
    class FakeRag:
        def __init__(self): self.paths = []; self.namespace = "global"; self.documents = {}
        def add_text_document(self, text, *, doc_id, namespace, source_uri, metadata):
            self.paths.append((Path(source_uri), metadata, doc_id)); return doc_id
        def remove_text_document(self, doc_id, namespace): return False
    rag = FakeRag()
    vault = ObsidianVault(tmp_path / "vault", rag=rag, settings_path=tmp_path / "settings.json")
    vault.upsert_record(_record())
    vault.archive_exchange("s", "잡담", "응")
    result = vault.sync_to_rag()
    assert result["indexed"] == len(rag.paths)
    assert all("raw" not in path.parts for path, _, _ in rag.paths)
    assert all(metadata["source_type"] == "obsidian" for _, metadata, _ in rag.paths)
    assert len({doc_id for _, _, doc_id in rag.paths}) == len(rag.paths)


def test_vault_reverse_sync_only_reindexes_changed_notes(tmp_path):
    class FakeRag:
        def __init__(self): self.calls = []; self.namespace = "global"
        def add_text_document(self, text, **kwargs): self.calls.append(kwargs); return kwargs["doc_id"]
        def remove_text_document(self, doc_id, namespace): return True
    rag = FakeRag()
    vault = ObsidianVault(tmp_path / "vault", rag=rag, settings_path=tmp_path / "settings.json")
    note = vault.upsert_record(_record())
    first = vault.sync_external_changes()
    second = vault.sync_external_changes()
    note.write_text(note.read_text(encoding="utf-8") + "\n사용자 편집\n", encoding="utf-8")
    third = vault.sync_external_changes()
    assert first["indexed"] > 0
    assert second["indexed"] == 0 and second["unchanged"] == first["indexed"]
    assert third["indexed"] == 1


def test_obsidian_plugin_contracts_are_registry_compatible():
    plugin = ObsidianPlugin()
    names = {tool.name for tool in plugin.get_tools()}
    assert {"obsidian_explore", "obsidian_sync_to_rag", "obsidian_lint"} <= names
    assert {intent.tool_name for intent in plugin.get_intents()} <= names


def test_vault_builds_filterable_global_and_local_graph(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    preference = vault.upsert_record(_record())
    project = vault.upsert_record(_record(
        record_id="project9", subject="그래프 UI", content="지식 그래프 작업공간을 구현해",
    ))
    # Add an explicit cross-note link to prove topology is produced from Markdown.
    relative = preference.relative_to(vault.root / "wiki").with_suffix("").as_posix()
    project.write_text(project.read_text(encoding="utf-8") + f"\n- [[{relative}|관련 선호]]\n", encoding="utf-8")

    graph = vault.build_graph(query="그래프", min_importance=0.1)
    assert any(node["relative_path"] == project.relative_to(vault.root).as_posix() for node in graph["nodes"])
    assert graph["facets"]["types"]

    local = vault.build_graph(center=project.relative_to(vault.root).as_posix(), depth=1)
    ids = {node["id"] for node in local["nodes"]}
    assert project.relative_to(vault.root).with_suffix("").as_posix() in ids
    assert preference.relative_to(vault.root).with_suffix("").as_posix() in ids
    assert local["edges"]


def test_vault_note_preview_rejects_paths_outside_vault(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    note = vault.upsert_record(_record())
    preview = vault.read_note(note.relative_to(vault.root))
    assert preview["title"] == "응답 말투"
    try:
        vault.read_note("../outside.md")
    except ObsidianVaultError:
        pass
    else:
        raise AssertionError("Vault 외부 문서가 열렸습니다.")
