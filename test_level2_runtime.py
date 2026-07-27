"""Level 2 refactor regression tests (standard-library unittest)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.harness import SafetyLayer
from core.plugin import get_plugin_registry
from core.executor import Executor
from core.permission import PermissionManager
from core.tools import AUTO_LOOP_EXCLUDED_TOOLS, get_tool_executor, get_tools_schema
from core.tool_result import ToolRunResult
from core.tool_result import ToolRunStatus
from core.project_indexer import ProjectIndexer
from core.memory import SemanticMemoryManager


class SafetyLayerTests(unittest.TestCase):
    def test_path_prefix_does_not_escape_allowed_root(self):
        root = Path.cwd().resolve()
        sibling = root.parent / f"{root.name}-evil"
        self.assertTrue(SafetyLayer.validate_path(str(root))[0])
        self.assertFalse(SafetyLayer.validate_path(str(sibling))[0])

    def test_shell_operators_are_rejected(self):
        self.assertFalse(SafetyLayer.validate_command("git status & whoami")[0])
        self.assertFalse(SafetyLayer.validate_command("git status | more")[0])


class ToolRegistryTests(unittest.TestCase):
    def test_schema_names_are_unique_and_plugins_are_loaded(self):
        get_tool_executor()
        names = [tool["name"] for tool in get_tools_schema()]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({"filesystem_search", "git_status", "browser_get_text"}.issubset(names))
        self.assertTrue({"filesystem", "git", "browser"}.issubset(get_plugin_registry().plugins))

    def test_confirm_tool_is_denied_without_permission_callback(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            target = Path(directory) / "denied.txt"
            manager = PermissionManager(str(Path(directory) / "permissions.json"))
            with patch("core.permission.get_permission_manager", return_value=manager):
                result = get_tool_executor().execute_tool("write_file", {"path": str(target), "content": "x"})
            self.assertIsInstance(result, ToolRunResult)
            self.assertFalse(result.succeeded)
            self.assertTrue(result.raw_output.startswith("오류: 권한이 거부되었습니다:"))
            self.assertFalse(target.exists())

    def test_legacy_read_file_is_adapted_to_verified_tool_result(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            target = Path(directory) / "sample.txt"
            target.write_text("hello", encoding="utf-8")
            executor = get_tool_executor()
            executor.workspace.set_workspace(directory)
            result = executor.execute_tool("read_file", {"path": "sample.txt"})
            self.assertIsInstance(result, ToolRunResult)
            self.assertTrue(result.succeeded)
            self.assertEqual(result.raw_output, "hello")
            self.assertTrue(result.evidence)

    def test_legacy_tool_without_verifier_is_unverified(self):
        result = get_tool_executor().execute_tool("get_profile", {})
        self.assertIsInstance(result, ToolRunResult)
        self.assertEqual(result.status, ToolRunStatus.UNVERIFIED)
        self.assertFalse(result.succeeded)

    def test_directory_and_workspace_tools_return_direct_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            target = Path(directory)
            (target / "sample.py").write_text("print('ok')", encoding="utf-8")
            executor = get_tool_executor()
            workspace = executor.set_workspace(str(target))
            listing = executor.list_directory("")
            info = executor.get_workspace_info()
            tree = executor.get_workspace_tree()
            for result in (workspace, listing, info, tree):
                self.assertIsInstance(result, ToolRunResult)
                self.assertTrue(result.succeeded)
                self.assertTrue(result.evidence)
            self.assertIn("sample.py", listing.raw_output)

    def test_project_indexer_tools_return_query_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            target = Path(directory)
            source = target / "sample.py"
            source.write_text("def hello():\n    return 'ok'\n", encoding="utf-8")
            executor = get_tool_executor()
            executor.workspace.set_workspace(str(target))
            executor._project_indexer = ProjectIndexer(str(target / "index.db"))
            executor._project_indexer.set_project_root(str(target))
            indexed = executor.index_project()
            files = executor.search_files("sample")
            symbols = executor.search_symbols("hello")
            info = executor.get_file_info(str(source))
            for result in (indexed, files, symbols, info):
                self.assertIsInstance(result, ToolRunResult)
                self.assertTrue(result.succeeded)
                self.assertTrue(result.evidence)
            self.assertEqual(symbols.evidence[0].data["count"], 1)

    def test_semantic_memory_tools_verify_persistence(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            manager = SemanticMemoryManager(str(Path(directory) / "semantic.db"))
            executor = get_tool_executor()
            with patch("core.memory.get_semantic_memory", return_value=manager):
                added = executor.add_semantic_memory("owner", "지원님", "profile")
                fetched = executor.get_semantic_memory("owner")
                searched = executor.search_semantic_memory("지원")
                deleted = executor.delete_semantic_memory("owner")
            for result in (added, fetched, searched, deleted):
                self.assertIsInstance(result, ToolRunResult)
                self.assertTrue(result.succeeded)
                self.assertTrue(result.evidence)

    def test_rag_tools_return_document_and_source_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            source = Path(directory) / "guide.txt"
            source.write_text("권한 관리자는 승인을 담당합니다.", encoding="utf-8")

            class Rag:
                def __init__(self):
                    self.documents = {}

                def add_document(self, file_path):
                    self.documents[Path(file_path).name] = {
                        "chunks": [Path(file_path).read_text(encoding="utf-8")],
                        "source": file_path,
                    }
                    return "문서가 성공적으로 추가되었습니다."

                def search_docs(self, _query, _top_k):
                    return [{"content": "권한 관리자는 승인을 담당합니다.", "source": str(source)}]

                def list_documents(self):
                    return "저장된 문서 목록:\n- guide.txt"

            executor = get_tool_executor()
            executor._rag_manager = Rag()
            added = executor.add_document(str(source))
            searched = executor.search_docs("권한 관리자", 3)
            listed = executor.list_documents()
            for result in (added, searched, listed):
                self.assertIsInstance(result, ToolRunResult)
                self.assertTrue(result.succeeded)
                self.assertTrue(result.evidence)
            self.assertEqual(searched.artifacts[0].uri, str(source))

    def test_nested_multi_agent_tools_are_not_offered_to_reasoner(self):
        self.assertIn("execute_multi_agent", AUTO_LOOP_EXCLUDED_TOOLS)
        self.assertIn("get_task_history", AUTO_LOOP_EXCLUDED_TOOLS)

    def test_unimplemented_gmail_oauth_connection_fails_fast(self):
        message = Executor._unsupported_capability_message("내 Gmail을 연결해줘")
        self.assertIsNotNone(message)
        self.assertIn("아직 구현되어 있지 않습니다", message)


if __name__ == "__main__":
    unittest.main()
