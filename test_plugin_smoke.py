from pathlib import Path

from plugins.filesystem import FilesystemPlugin
from plugins.git import GitPlugin
from plugins.system_tools import SystemToolsPlugin
from plugins.windows_control import WindowsControlPlugin
from core.tool_result import ToolRunResult


def test_filesystem_tools_execute_directly(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    (tmp_path / "sample.txt").write_text("ok", encoding="utf-8")
    plugin = FilesystemPlugin()
    assert "sample.txt" in plugin.execute_tool("filesystem_tree", {"path": str(tmp_path)})
    assert "sample.txt" in plugin.execute_tool("filesystem_search", {"path": str(tmp_path), "pattern": "*.txt"})


def test_git_read_tools_execute_against_current_repository(monkeypatch):
    root = Path(__file__).resolve().parent
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(root)])
    plugin = GitPlugin()
    for tool, data in (
        ("git_status", {"repo_path": str(root)}),
        ("git_diff", {"repo_path": str(root)}),
        ("git_log", {"repo_path": str(root), "limit": 2}),
    ):
        result = plugin.execute_tool(tool, data)
        assert isinstance(result, ToolRunResult)
        assert result.succeeded
        assert result.evidence[0].kind == "git_repository"


def test_system_tools_execute_directly():
    plugin = SystemToolsPlugin()
    assert plugin.execute_tool("get_time", {})
    assert plugin.execute_tool("get_date", {})
    assert isinstance(plugin.execute_tool("list_plugins", {}), str)


def test_windows_discovery_and_safe_failure_execute_directly(monkeypatch):
    plugin = WindowsControlPlugin()
    monkeypatch.setattr(plugin, "_discover_executables", lambda *_args: [])
    search = plugin.execute_tool("windows_find_apps", {"query": "notepad"})
    launch = plugin.execute_tool("windows_launch_app", {"target": "definitely-not-installed-jarvis-app"})
    focus = plugin.execute_tool("windows_focus_window", {"title": "definitely-not-open-jarvis-window"})
    assert isinstance(search, ToolRunResult) and search.succeeded
    assert isinstance(launch, ToolRunResult) and not launch.succeeded
    assert isinstance(focus, ToolRunResult) and not focus.succeeded
