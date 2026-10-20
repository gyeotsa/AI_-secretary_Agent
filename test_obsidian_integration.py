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


def test_delete_chat_archive_removes_only_generated_raw_files_and_blocks_queued_writer(tmp_path):
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    session_id = "chat[]*"
    raw = vault.archive_exchange(session_id, "삭제할 질문", "삭제할 답변")
    previous = raw.parent.parent / "2020-01" / raw.name
    previous.parent.mkdir()
    previous.write_bytes(raw.read_bytes())
    user_file = raw.parent.parent / "2020-02" / raw.name
    user_file.parent.mkdir()
    user_file.write_text("사용자가 직접 만든 파일", encoding="utf-8")
    other = vault.archive_exchange("other-chat", "유지할 질문", "유지할 답변")
    knowledge = vault.upsert_record(_record())

    assert vault.delete_session_archive(session_id) == 2
    assert not raw.exists() and not previous.exists()
    assert user_file.exists() and other.exists() and knowledge.exists()
    vault.archive_exchange(session_id, "삭제 전에 예약된 질문", "늦은 보관 쓰기")
    assert not raw.exists()
    assert vault.delete_session_archive(session_id) == 0


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


def test_graph_preview_renders_note_and_summary_without_internal_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from ui.knowledge_graph_workspace import KnowledgeGraphWindow

    app = QApplication.instance() or QApplication([])
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    record = _record(content='자연스러운 반말을 사용해\n\n```python\nprint("[[user/code|원문]]")\n```')
    note = vault.upsert_record(record)
    original = note.read_bytes()
    window = KnowledgeGraphWindow(vault)
    try:
        window.show_note(note.relative_to(vault.root).as_posix())
        text = window.preview.toPlainText()
        assert "자연스러운 반말을 사용해" in text
        assert 'print("[[user/code|원문]]")' in text
        assert "행동: 선호" in text and "[[actions/" not in text
        assert "유형: 선호" in text and "주제" in text
        assert "managed_by" not in text and record.record_id not in text
        assert "## 기억" not in text and "```python" not in text
        assert note.read_bytes() == original
    finally:
        window.close()
        app.processEvents()


def test_graph_preview_does_not_interpret_html_or_fetch_external_resources(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QTextDocument
    from PyQt6.QtWidgets import QApplication
    from ui.knowledge_graph_workspace import KnowledgeGraphWindow

    app = QApplication.instance() or QApplication([])
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    path = vault.root / "wiki" / "unsafe.md"
    path.write_text('# 외부 내용\n\n<img src="file:///private.png">\n\n'
                    '![remote](https://example.invalid/private.png)\n\n'
                    '[문서](file:///private.txt)\n\n<script>alert("x")</script>', encoding="utf-8")
    window = KnowledgeGraphWindow(vault)
    try:
        window.show_note("wiki/unsafe.md")
        rendered = window.preview.toHtml()
        assert '<script>' not in rendered and '<img src="file:///private.png"' not in rendered
        assert window.preview.openLinks() is False and window.preview.openExternalLinks() is False
        assert window.preview.loadResource(QTextDocument.ResourceType.ImageResource,
                                           QUrl("file:///private.png")) is None
        assert window.preview.loadResource(QTextDocument.ResourceType.ImageResource,
                                           QUrl("https://example.invalid/private.png")) is None
    finally:
        window.close()
        app.processEvents()


def test_graph_translates_known_setting_labels_only_in_display(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from ui.knowledge_graph_workspace import KnowledgeGraphWindow, _note_title

    app = QApplication.instance() or QApplication([])
    vault = ObsidianVault(tmp_path / "vault", settings_path=tmp_path / "settings.json")
    path = vault.upsert_record(_record(subject="사용자 설정 assistant_name", content="아니스"))
    original = path.read_bytes()
    window = KnowledgeGraphWindow(vault)
    try:
        window.show_note(path.relative_to(vault.root).as_posix())
        assert "사용자 설정 비서 이름" in window.preview.toPlainText()
        assert "assistant_name" not in window.preview.toPlainText()
        assert _note_title("assistant_name") == "비서 이름"
        assert _note_title("API assistant_name") == "API assistant_name"
        assert path.read_bytes() == original
        map_path = vault.root / "wiki" / "actions" / "preference.md"
        map_original = map_path.read_bytes()
        window.show_note("wiki/actions/preference.md")
        assert "사용자 설정 비서 이름" in window.preview.toPlainText()
        assert "유형: 작업별 기억 목록" in window.preview.toPlainText()
        assert "[[" not in window.preview.toPlainText()
        assert map_path.read_bytes() == map_original
        window.search.setText("비서 이름")
        window.refresh_graph()
        assert any(item.toolTip() == "사용자 설정 비서 이름" for item in window.graph_view.nodes.values())
    finally:
        window.close()
        app.processEvents()
