"""Real plugin discovery/loadout with a fake Drive and isolated existing RAG."""
import json

from core.plugin import PluginRegistry
from core.tool_loadout import ToolLoadoutSelector
from core.tool_result import ToolRunStatus
from plugins.cloud_knowledge import CloudKnowledgePlugin
from test_cloud_content import Response, metadata, runtime


def test_import_and_auto_discovery_do_not_initialize_account_or_rag_stores(tmp_path, monkeypatch):
    from core.cloud_content import CloudContentRuntime
    from core.remote_runtime import OAuthCoordinator
    monkeypatch.setattr(OAuthCoordinator, "__init__", lambda *_: (_ for _ in ()).throw(AssertionError("vault initialized")))
    (tmp_path / "cloud_knowledge.py").write_text("# discovery fixture", encoding="utf-8")
    registry = PluginRegistry()
    registry.load_plugins_from_directory(str(tmp_path))
    plugin = registry.get_plugin("cloud_knowledge")
    assert isinstance(plugin, CloudKnowledgePlugin) and isinstance(plugin.runtime, CloudContentRuntime)
    assert plugin.runtime._api is None and plugin.runtime._rag is None
    assert not registry.validate_contracts()
    contract = registry.get_capability("cloud_sync_documents")
    assert contract and contract.required_permissions == ["cloud_read"]
    assert not contract.automatic_retry_allowed
    names = ToolLoadoutSelector(registry).select("구글 드라이브 문서 본문 RAG 동기화해").tool_names
    assert "cloud_sync_documents" in names
    assert "cloud_search_evidence" in ToolLoadoutSelector(registry).select("구글 드라이브 문서 본문 근거 검색해").tool_names


def test_plugin_sync_and_search_expose_actual_body_citations_to_executor_result(tmp_path):
    plugin = CloudKnowledgePlugin()
    plugin.runtime = runtime(tmp_path, metadata(), Response(b"target real body"), metadata())
    written = plugin.execute_tool("cloud_sync_documents", {"provider": "google_drive", "account": "fixture", "file_ids": ["a"]})
    assert written.succeeded and written.artifacts[0].uri == "https://drive.google.com/file/d/a/view"
    plugin.runtime.api.session.responses = [metadata()]
    read = plugin.execute_tool("cloud_search_evidence", {"provider": "google_drive", "account": "fixture", "query": "target"})
    assert read.succeeded
    payload = json.loads(read.raw_output)
    assert payload["results"][0]["content"] == "target real body"
    assert payload["results"][0]["remote_id"] == "a"
    assert read.artifacts[0].metadata["chunk_id"]
    assert not payload["answer_usage_verified"] and payload["content_role"] == "untrusted_source_data"


def test_unsupported_and_partial_failure_are_not_success_or_cloud_complete(tmp_path):
    plugin = CloudKnowledgePlugin()
    plugin.runtime = runtime(tmp_path, metadata(mime="application/pdf"))
    result = plugin.execute_tool("cloud_sync_documents", {"provider": "google_drive", "account": "fixture", "file_ids": ["a"]})
    assert result.status == ToolRunStatus.PARTIAL and not result.succeeded
    assert result.evidence[0].data["scope"] == "explicit_file_ids" and not result.evidence[0].data["complete"]


def test_provider_or_source_exception_body_is_not_echoed(tmp_path):
    plugin = CloudKnowledgePlugin()
    plugin.runtime = runtime(tmp_path)
    result = plugin.execute_tool("cloud_sync_documents", {"provider": "notion", "account": "fixture", "file_ids": ["a"]})
    assert result.status == ToolRunStatus.FAILED and result.error == "cloud_content_failed:ValueError"
