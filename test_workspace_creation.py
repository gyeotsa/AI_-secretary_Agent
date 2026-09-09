from pathlib import Path

from core.intent_router import IntentRouter
from core.plugin import PluginRegistry
from core.tool_result import ToolRunResult, ToolRunStatus
from core.verifier import ToolVerifier
from core.workspace import WorkspaceManager, get_workspace_manager
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
    assert isinstance(result, ToolRunResult)
    assert result.status == ToolRunStatus.SUCCEEDED
    assert result.evidence and result.artifacts[0].kind == "directory"
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
    assert isinstance(result, ToolRunResult)
    assert result.status == ToolRunStatus.SUCCEEDED
    assert result.evidence and result.artifacts[0].kind == "file"
    verification = ToolVerifier().verify(resolved.tool_name, resolved.slots, result)
    assert verification.success
    assert (tmp_path / "launch_monitor.py").is_file()


def test_workspace_info_reports_selected_folder(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    info = workspace.get_info()
    assert info.path == str(Path(tmp_path).resolve())


def test_write_intent_uses_confirmed_file_and_saves_generated_content(tmp_path, monkeypatch):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    target = tmp_path / "test.py"
    target.write_text("", encoding="utf-8")
    registry, router = _router()

    resolution = router.resolve(
        '해당 파일에 "hello world"를 출력하는 소스코드를 작성해줘.',
        "filesystem.write_file", {"filename": "test.py"},
    )
    assert resolution.intent_name == "filesystem.write_file"
    assert resolution.slots["filename"] == "test.py"

    class FakeCodingModel:
        def chat(self, messages):
            assert messages[0]["role"] == "system"
            assert "hello world" in messages[1]["content"]
            return '```python\nprint("hello world")\n```'

    monkeypatch.setattr("core.llm.get_llm_client", lambda role: FakeCodingModel())
    result = registry.execute_tool(resolution.tool_name, resolution.slots)
    verification = ToolVerifier().verify(resolution.tool_name, resolution.slots, result)
    assert verification.success
    assert target.read_text(encoding="utf-8") == 'print("hello world")\n'


def test_write_referent_without_dialogue_does_not_guess_by_disk_mtime(tmp_path):
    get_workspace_manager().set_workspace(str(tmp_path))
    (tmp_path / "unrelated.py").write_text("keep = 1", encoding="utf-8")
    _, router = _router()
    result = router.resolve('해당 파일에 "hello world"를 출력하는 소스코드를 작성해줘.')
    assert result.question
    assert not result.slots.get("filename")


def test_create_file_never_overwrites_existing_file(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    target = tmp_path / "keep.py"
    target.write_text("important", encoding="utf-8")
    registry, _router_instance = _router()

    result = registry.execute_tool(
        "filesystem_create_file", {"filename": "keep.py", "content": ""}
    )
    assert result.raw_output.startswith("오류:")
    assert target.read_text(encoding="utf-8") == "important"


def test_write_follow_up_keeps_original_instruction(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    registry, router = _router()

    first = router.resolve('"안녕"을 출력하는 소스코드를 작성해줘.')
    assert first.question
    assert "안녕" in first.slots["instruction"]
    follow_up = router.resolve("test.py를 수정할거야", first.intent_name, first.slots)
    assert follow_up.ready
    assert follow_up.slots["filename"] == "test.py"
    assert "안녕" in follow_up.slots["instruction"]


def test_filename_without_extension_resolves_unique_workspace_file(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    (tmp_path / "test.py").write_text("", encoding="utf-8")
    _registry, router = _router()

    resolution = router.resolve('test파일에 "안녕"을 출력하는 코드를 작성해줘.')
    assert resolution.ready
    assert resolution.slots["filename"] == "test.py"


def test_refusal_is_not_written_over_existing_source(tmp_path, monkeypatch):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    target = tmp_path / "test.py"
    target.write_text("original\n", encoding="utf-8")
    registry, _router_instance = _router()

    class RefusingModel:
        def chat(self, messages):
            return "죄송합니다, 직접 파일을 수정하거나 코드를 작성하는 기능은 없습니다."

    monkeypatch.setattr("core.llm.get_llm_client", lambda role: RefusingModel())
    result = registry.execute_tool(
        "filesystem_write_file",
        {"filename": "test.py", "instruction": '"안녕"을 출력해줘'},
    )
    assert result.raw_output.startswith("오류:")
    assert target.read_text(encoding="utf-8") == "original\n"


def test_extension_and_modify_verb_route_to_write_intent(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    (tmp_path / "test.py").write_text("old\n", encoding="utf-8")
    _registry, router = _router()

    resolution = router.resolve(
        'test.py를 수정해줘. "안녕"이라고 출력이 가능하게 수정해줘.'
    )
    assert resolution.ready
    assert resolution.intent_name == "filesystem.write_file"
    assert resolution.tool_name == "filesystem_write_file"


def test_intent_resolution_without_workspace_does_not_raise():
    registry = PluginRegistry()
    plugin = FilesystemPlugin()
    plugin.workspace = WorkspaceManager()
    registry.register_plugin(plugin)
    router = IntentRouter(registry)

    resolution = router.resolve('test파일에 "안녕"을 출력하게 수정해줘.')
    assert resolution.matched
    assert resolution.question


def test_target_first_then_instruction_continues_same_write_task(tmp_path):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    (tmp_path / "test.py").write_text("old\n", encoding="utf-8")
    _registry, router = _router()

    first = router.resolve("test파일을 수정해줘")
    assert first.intent_name == "filesystem.write_file"
    assert first.slots["filename"] == "test.py"
    assert "어떤 내용" in first.question

    follow_up = router.resolve(
        '"안녕"이라는 출력을 하도록 소스코드를 작성해줘',
        first.intent_name,
        first.slots,
    )
    assert follow_up.ready
    assert follow_up.slots["filename"] == "test.py"
    assert "안녕" in follow_up.slots["instruction"]


def test_missing_requested_literal_is_not_saved(tmp_path, monkeypatch):
    workspace = get_workspace_manager()
    workspace.set_workspace(str(tmp_path))
    target = tmp_path / "test.py"
    target.write_text("original\n", encoding="utf-8")
    registry, _router_instance = _router()

    class WrongModel:
        def chat(self, messages):
            return 'print("다른 내용")'

    monkeypatch.setattr("core.llm.get_llm_client", lambda role: WrongModel())
    result = registry.execute_tool(
        "filesystem_write_file",
        {"filename": "test.py", "instruction": '"안녕"을 출력해줘'},
    )
    assert result.raw_output.startswith("오류:")
    assert target.read_text(encoding="utf-8") == "original\n"
