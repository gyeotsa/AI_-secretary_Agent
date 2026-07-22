"""Level 2 refactor regression tests (standard-library unittest)."""
import os
import tempfile
import unittest
from pathlib import Path

from core.harness import SafetyLayer
from core.plugin import get_plugin_registry
from core.tools import get_tool_executor, get_tools_schema


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
            result = get_tool_executor().execute_tool("write_file", {"path": str(target), "content": "x"})
            self.assertTrue(result.startswith("오류: 권한이 거부되었습니다:"))
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
