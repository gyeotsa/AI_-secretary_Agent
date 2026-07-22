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
            self.assertTrue(result.startswith("오류: 권한이 거부되었습니다:"))
            self.assertFalse(target.exists())

    def test_nested_multi_agent_tools_are_not_offered_to_reasoner(self):
        self.assertIn("execute_multi_agent", AUTO_LOOP_EXCLUDED_TOOLS)
        self.assertIn("get_task_history", AUTO_LOOP_EXCLUDED_TOOLS)

    def test_unimplemented_gmail_oauth_connection_fails_fast(self):
        message = Executor._unsupported_capability_message("내 Gmail을 연결해줘")
        self.assertIsNotNone(message)
        self.assertIn("아직 구현되어 있지 않습니다", message)


if __name__ == "__main__":
    unittest.main()
