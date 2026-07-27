from pathlib import Path

from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.verifier import ToolVerifier
from core.workspace import get_workspace_manager
from plugins.filesystem import FilesystemPlugin


def _router():
    registry = PluginRegistry()
    registry.register_plugin(FilesystemPlugin())
    return registry, IntentRouter(registry)


def test_project_creation_collects_name_then_creates_real_directory(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    registry, router = _router()

    first = router.resolve("새로운 프로젝트를 하나 생성해줄래?")
    assert first.question
    follow_up = router.resolve("launch_monitor", first.intent_name, first.slots)
    assert follow_up.ready

    result = registry.execute_tool(follow_up.tool_name, follow_up.slots)
    verification = ToolVerifier().verify(follow_up.tool_name, follow_up.slots, result)
    assert verification.success
    assert (tmp_path / "launch_monitor").is_dir()
    assert workspace.get_workspace_path() == str((tmp_path / "launch_monitor").resolve())


def test_file_creation_requires_name_and_verifies_real_file(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    registry, router = _router()

    incomplete = router.resolve("우선 파이썬 파일을 하나 생성해줘.")
    assert incomplete.question
    assert not incomplete.ready

    resolved = router.resolve("launch_monitor.py로 해줘", incomplete.intent_name, incomplete.slots)
    assert resolved.ready
    result = registry.execute_tool(resolved.tool_name, resolved.slots)
    verification = ToolVerifier().verify(resolved.tool_name, resolved.slots, result)
    assert verification.success
    assert (tmp_path / "launch_monitor.py").is_file()


def test_workspace_info_reports_selected_folder(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    info = workspace.get_info()
    assert info.path == str(Path(tmp_path).resolve())


def test_write_intent_uses_recent_file_and_saves_generated_content(tmp_path, monkeypatch):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    target = tmp_path / "test.py"
    target.write_text("", encoding="utf-8")
    registry, router = _router()

    resolution = router.resolve(
        '해당 파일에 "hello world"를 출력하는 소스코드를 작성해줘.'
    )
    assert resolution.intent_name == "filesystem.write_file"
    assert resolution.slots["filename"] == "test.py"

    class FakeCodingModel:
        def chat(self, messages):
            assert "hello world" in messages[0]["content"]
            return '```python\nprint("hello world")\n```'

    monkeypatch.setattr("core.llm.get_llm_client", lambda role: FakeCodingModel())
    result = registry.execute_tool(resolution.tool_name, resolution.slots)
    verification = ToolVerifier().verify(resolution.tool_name, resolution.slots, result)
    assert verification.success
    assert target.read_text(encoding="utf-8") == 'print("hello world")\n'


def test_create_file_never_overwrites_existing_file(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    target = tmp_path / "keep.py"
    target.write_text("important", encoding="utf-8")
    registry, _router_instance = _router()

    result = registry.execute_tool(
        "filesystem_create_file", {"filename": "keep.py", "content": ""}
    )
    assert result.startswith("오류:")
    assert target.read_text(encoding="utf-8") == "important"
