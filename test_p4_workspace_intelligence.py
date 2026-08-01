import json
from pathlib import Path

from core.project_indexer import ProjectIndexer
from core.memory import ConversationMemory
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
