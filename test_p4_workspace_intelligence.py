import json
from pathlib import Path

from core.project_indexer import IgnoreRules, ProjectIndexer
from core.memory import ConversationMemory
from core.project_bootstrap import ProjectBootstrapper
from core.rag import VectorRAGManager
from core.workspace import WorkspaceManager


def test_workspace_catalog_alias_settings_and_restore(tmp_path):
    project = tmp_path / "demo"
    project.mkdir()
    state = tmp_path / "workspaces.json"

    manager = WorkspaceManager(str(state))
    assert manager.set_workspace(str(project), alias="업무 프로젝트")
    assert manager.update_settings({"python": "3.12", "auto_index": True})
    assert manager.get_info().alias == "업무 프로젝트"
    assert manager.get_info().namespace.startswith("workspace-")

    restored = WorkspaceManager(str(state))
    assert restored.current_workspace == str(project.resolve())
    assert restored.get_info().settings["auto_index"] is True
    assert restored.list_workspaces()[0].alias == "업무 프로젝트"


def test_workspace_metadata_reads_do_not_walk_the_project(tmp_path, monkeypatch):
    import os

    project = tmp_path / "repo"
    project.mkdir()
    (project / "main.py").write_text("pass", encoding="utf-8")
    manager = WorkspaceManager(str(tmp_path / "workspaces.json"))
    walk = os.walk
    traversals = []

    def tracked_walk(path):
        traversals.append(path)
        yield from walk(path)

    monkeypatch.setattr("core.workspace.os.walk", tracked_walk)
    assert manager.set_workspace(str(project))
    for _ in range(5):
        assert manager.get_info().path == str(project.resolve())
    assert traversals == []
    assert manager.refresh_info(str(project)).file_count == 1
    assert len(traversals) == 1
    assert manager.get_info().file_count == 1
    assert len(traversals) == 1


def test_late_workspace_metadata_scan_cannot_overwrite_new_project(tmp_path, monkeypatch):
    import os

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "main.py").write_text("pass", encoding="utf-8")
    manager = WorkspaceManager(str(tmp_path / "workspaces.json"))
    manager.set_workspace(str(first))
    walk = os.walk

    def switch_during_walk(path):
        manager.set_workspace(str(second))
        yield from walk(path)

    monkeypatch.setattr("core.workspace.os.walk", switch_during_walk)
    result = manager.refresh_info(str(first))
    assert result.path == str(first.resolve())
    assert result.file_count == 1
    assert manager.get_info().path == str(second.resolve())
    assert manager.get_info().file_count == 0


def test_remove_workspace_removes_catalog_only_and_clears_active_scope(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    source = project / "main.py"
    source.write_text("pass", encoding="utf-8")
    manager = WorkspaceManager(str(tmp_path / "workspaces.json"))
    manager.set_workspace(str(project))
    namespace = manager.get_namespace()

    assert manager.remove_workspace(namespace)
    assert manager.list_workspaces(include_unavailable=True) == []
    assert manager.current_workspace is None
    assert manager.get_info().namespace == "global"
    assert manager._state["last_workspace"] == ""
    assert source.read_text(encoding="utf-8") == "pass"
    assert not manager.remove_workspace(namespace)
    assert WorkspaceManager(str(manager.state_path)).current_workspace is None


def test_ignore_rule_discovery_prunes_dependency_and_ignored_directories(tmp_path, monkeypatch):
    import os

    project = tmp_path / "repo"
    project.mkdir()
    (project / ".gitignore").write_text("cache/\n", encoding="utf-8")
    for name in (".venv", "node_modules", "cache", "source"):
        folder = project / name / "nested"
        folder.mkdir(parents=True)
        (folder / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
    walk = os.walk
    visited = []

    def tracked_walk(path):
        for current, dirs, files in walk(path):
            visited.append(Path(current).relative_to(project).as_posix())
            yield current, dirs, files

    monkeypatch.setattr("core.project_indexer.os.walk", tracked_walk)
    rules = IgnoreRules(project)

    assert visited == [".", "source", "source/nested"]
    assert rules.ignored(project / "source" / "nested" / "hidden.py")


def test_selecting_project_does_not_discover_ignore_rules(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    indexer = ProjectIndexer(str(tmp_path / "index.db"))

    def fail_if_scanned(root):
        raise AssertionError("Project selection must not scan the filesystem")

    monkeypatch.setattr("core.project_indexer.IgnoreRules", fail_if_scanned)
    assert indexer.set_project_root(str(project))
    assert indexer.project_root == str(project.resolve())


def test_file_tree_loads_ignore_rules_once_after_project_selection(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".gitignore").write_text("cache/\n", encoding="utf-8")
    for name in (".venv", "cache", "source"):
        folder = project / name / "nested"
        folder.mkdir(parents=True)
        (folder / "main.py").write_text("pass", encoding="utf-8")
    indexer = ProjectIndexer(str(tmp_path / "index.db"))
    indexer.set_project_root(str(project))
    discoveries = []

    def tracked_rules(root):
        discoveries.append(root)
        return IgnoreRules(root)

    monkeypatch.setattr("core.project_indexer.IgnoreRules", tracked_rules)
    tree = indexer.get_file_tree()
    assert discoveries == [project.resolve()]
    assert [item["name"] for item in tree["children"]] == ["source", ".gitignore"]
    assert tree["children"][0]["children"][0]["children"][0]["name"] == "main.py"


def test_overlapping_project_scan_is_skipped(tmp_path, monkeypatch):
    import threading

    project = tmp_path / "repo"
    project.mkdir()
    indexer = ProjectIndexer(str(tmp_path / "index.db"))
    indexer.set_project_root(str(project))
    entered, release = threading.Event(), threading.Event()

    def blocking_files(root=None, rules=None):
        entered.set()
        assert release.wait(5)
        return iter(())

    monkeypatch.setattr(indexer, "_iter_files", blocking_files)
    worker = threading.Thread(target=indexer.sync_changes)
    worker.start()
    try:
        assert entered.wait(5)
        assert indexer.sync_changes() == {"added": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_project_scan_keeps_captured_scope_when_project_changes(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    source = first / "first.py"
    source.write_text("def first_function():\n    pass\n", encoding="utf-8")
    indexer = ProjectIndexer(str(tmp_path / "index.db"))
    indexer.set_project_root(str(first))
    iter_files = indexer._iter_files

    def switch_during_scan(root=None, rules=None):
        indexer.set_project_root(str(second))
        yield from iter_files(root, rules)

    monkeypatch.setattr(indexer, "_iter_files", switch_during_scan)
    assert indexer.sync_changes(root_path=str(first))["added"] == 1
    assert indexer.search_files("first.py") == []
    indexer.set_project_root(str(first))
    assert indexer.search_files("first.py")[0]["path"] == str(source.resolve())


def test_index_preview_handles_large_text_and_binary_without_full_file_reads(tmp_path, monkeypatch):
    import io

    project = tmp_path / "repo"
    project.mkdir()
    text_path, binary_path = project / "large.txt", project / "large.bin"
    text_path.write_text("한글" * 10000, encoding="utf-8")
    binary_path.write_bytes(b"\0" + b"x" * 100000)
    indexer = ProjectIndexer(str(tmp_path / "index.db"))
    indexer.set_project_root(str(project))
    original_open = Path.open
    binary_read_sizes = []

    class GuardedBinary(io.BytesIO):
        def read(self, size=-1):
            binary_read_sizes.append(size)
            assert 0 <= size <= 4096
            return super().read(size)

    def guarded_open(path, mode="r", *args, **kwargs):
        if path == binary_path and mode == "rb":
            return GuardedBinary(b"\0" + b"x" * 100000)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    assert indexer.sync_changes()["added"] == 2
    assert binary_read_sizes == [4096]
    assert indexer.get_file_info(str(text_path)).content_preview == "한글" * 250 + "..."
    assert not indexer.get_file_info(str(binary_path)).is_text


def test_project_index_honors_gitignore_and_syncs_changes(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".gitignore").write_text("ignored.py\ncache/\n", encoding="utf-8")
    (project / "main.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    (project / "ignored.py").write_text("SECRET = True\n", encoding="utf-8")
    (project / "cache").mkdir()
    (project / "cache" / "generated.py").write_text("x = 1\n", encoding="utf-8")

    indexer = ProjectIndexer(str(tmp_path / "index.db"))
    assert indexer.set_project_root(str(project))
    first = indexer.sync_changes()
    assert first["added"] == 2  # .gitignore and main.py
    assert indexer.search_files("ignored.py") == []
    assert indexer.search_symbols("greet")[0]["name"] == "greet"

    (project / "main.py").write_text("def welcome():\n    return 'hello'\n", encoding="utf-8")
    (project / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    result = indexer.sync_changes()
    assert result["updated"] == 1
    assert result["added"] == 1
    assert indexer.search_symbols("greet") == []
    assert indexer.search_symbols("welcome")

    (project / "new.py").unlink()
    assert indexer.sync_changes()["deleted"] == 1
    assert indexer.search_files("new.py") == []


def test_project_profile_detects_framework_and_commands(tmp_path):
    project = tmp_path / "web"
    project.mkdir()
    (project / "requirements.txt").write_text("fastapi\npytest\n", encoding="utf-8")
    (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "package.json").write_text(json.dumps({
        "dependencies": {"react": "latest"},
        "scripts": {"test": "vitest", "build": "vite build"},
    }), encoding="utf-8")
    (project / "view.tsx").write_text("export default () => null\n", encoding="utf-8")

    indexer = ProjectIndexer(str(tmp_path / "profile.db"))
    indexer.set_project_root(str(project))
    profile = indexer.detect_project_profile()
    assert {"Python", "TypeScript"}.issubset(profile["languages"])
    assert {"FastAPI", "pytest", "React"}.issubset(profile["frameworks"])
    assert "python -m pytest" in profile["test_commands"]
    assert "npm run test" in profile["test_commands"]


def test_memory_sessions_are_scoped_by_workspace(tmp_path):
    memory = ConversationMemory(str(tmp_path / "memory.db"))
    memory.set_namespace("workspace-a")
    session_a = memory.create_session("A")
    memory.save_message(session_a, "user", "alpha")
    assert [item[0] for item in memory.list_sessions()] == [session_a]

    memory.set_namespace("workspace-b")
    session_b = memory.create_session("B")
    memory.save_message(session_b, "user", "beta")
    assert [item[0] for item in memory.list_sessions()] == [session_b]
    assert memory.load_session(session_a)[0]["content"] == "alpha"


def test_simple_rag_search_is_scoped_by_namespace():
    rag = VectorRAGManager.__new__(VectorRAGManager)
    rag.namespace = "workspace-a"
    rag.documents = {
        "workspace-a::a.txt": {"chunks": ["shared alpha"], "source": "a.txt", "namespace": "workspace-a"},
        "workspace-b::b.txt": {"chunks": ["shared beta"], "source": "b.txt", "namespace": "workspace-b"},
    }
    result = rag._simple_search("shared", 5)
    assert [item["source"] for item in result] == ["a.txt"]


def test_project_bootstrap_creates_template_and_git_repository(tmp_path):
    result = ProjectBootstrapper().create(
        str(tmp_path), "sample-app", "python-cli", create_venv=False, init_git=True
    )
    root = Path(result.path)
    assert (root / "src" / "sample_app" / "main.py").exists()
    assert (root / "tests" / "test_main.py").exists()
    assert (root / ".jarvis-project.json").exists()
    assert (root / ".git").is_dir()
    assert result.git_initialized is True


def test_project_bootstrap_rejects_escape_and_existing_target(tmp_path):
    bootstrapper = ProjectBootstrapper()
    try:
        bootstrapper.create(str(tmp_path), "../escape", create_venv=False, init_git=False)
    except ValueError:
        pass
    else:
        raise AssertionError("경로 탈출 이름은 거부되어야 합니다.")

    existing = tmp_path / "exists"
    existing.mkdir()
    try:
        bootstrapper.create(str(tmp_path), "exists", create_venv=False, init_git=False)
    except FileExistsError:
        pass
    else:
        raise AssertionError("기존 프로젝트를 덮어쓰면 안 됩니다.")


def test_project_bootstrap_can_create_virtual_environment(tmp_path):
    result = ProjectBootstrapper().create(
        str(tmp_path), "venv-check", "empty", create_venv=True, init_git=False
    )
    python_name = "python.exe" if __import__("os").name == "nt" else "python"
    scripts = "Scripts" if __import__("os").name == "nt" else "bin"
    assert (Path(result.path) / ".venv" / scripts / python_name).exists()
    assert result.virtual_environment is True
def test_inaccessible_last_workspace_does_not_block_startup(tmp_path, monkeypatch):
    state = tmp_path / "workspaces.json"
    state.write_text('{"version":1,"last_workspace":"blocked","workspaces":{"blocked":{"path":"blocked"}}}', encoding="utf-8")
    original = __import__("pathlib").Path.is_dir

    def guarded(path):
        if str(path) == "blocked":
            raise PermissionError("access denied")
        return original(path)

    monkeypatch.setattr("pathlib.Path.is_dir", guarded)
    manager = WorkspaceManager(str(state), restore=True)
    assert manager.current_workspace is None
    assert manager._state["last_workspace"] == ""


def test_legacy_pytest_workspace_is_not_restored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = Path("data/workspaces.json")
    state.parent.mkdir()
    transient = tmp_path / ".pytest-tmp" / "test_case0"
    transient.mkdir(parents=True)
    key = str(transient.resolve())
    state.write_text(json.dumps({"version": 1, "last_workspace": key,
                                 "workspaces": {key: {"path": key}}}), encoding="utf-8")
    manager = WorkspaceManager(str(state), restore=True)
    assert manager.current_workspace is None
    assert manager._state["last_workspace"] == ""


def test_system_pytest_basetemp_is_pruned_from_real_catalog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # This fixture represents an OS temp root even when pytest --basetemp is
    # explicitly located in a workspace with different filesystem permissions.
    monkeypatch.setattr("core.workspace.tempfile.gettempdir", lambda: str(tmp_path))
    state = Path("data/workspaces.json")
    state.parent.mkdir()
    transient = tmp_path / "pytest-of-user" / "pytest-9" / "test_workspace0"
    transient.mkdir(parents=True)
    key = str(transient.resolve())
    state.write_text(json.dumps({
        "version": 1,
        "last_workspace": key,
        "workspaces": {
            key: {"path": key},
            str((tmp_path / "real-project").resolve()): {
                "path": str((tmp_path / "real-project").resolve())
            },
        },
    }), encoding="utf-8")

    manager = WorkspaceManager(str(state), restore=True)

    assert manager.current_workspace is None
    assert manager._state["last_workspace"] == ""
    assert key not in manager._state["workspaces"]


def test_pytest_named_real_workspace_outside_system_temp_is_preserved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("core.workspace.tempfile.gettempdir", lambda: str(tmp_path / "os-temp"))
    state = Path("data/workspaces.json")
    state.parent.mkdir()
    real = tmp_path / "pytest-project" / "source"
    real.mkdir(parents=True)
    key = str(real.resolve())
    state.write_text(json.dumps({"version": 1, "last_workspace": key,
                                 "workspaces": {key: {"path": key}}}), encoding="utf-8")
    manager = WorkspaceManager(str(state), restore=True)
    assert manager.current_workspace == key
    assert key in manager._state["workspaces"]
