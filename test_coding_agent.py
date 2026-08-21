from hashlib import sha256
from pathlib import Path
import json
import sys

from core.coding_agent import CodingAgent, FileEdit
from core.tool_result import ToolRunResult
from plugins.coding import CodingPlugin


def test_repository_analysis_collects_structure_dependencies_and_git_state(tmp_path):
    (tmp_path / "README.md").write_text("# Sample", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("", encoding="utf-8")

    snapshot = CodingAgent(tmp_path).analyze_repository()

    assert snapshot.readme_files == ["README.md"]
    assert snapshot.dependency_files == ["pyproject.toml"]
    assert "src\\app.py" in snapshot.files or "src/app.py" in snapshot.files
    assert not any("ignored.py" in name for name in snapshot.files)


def test_minimal_patch_preserves_surrounding_content_and_reports_diff(tmp_path):
    target = tmp_path / "app.py"
    original = "def greet():\n    return 'hello'\n\nprint(greet())\n"
    target.write_text(original, encoding="utf-8")
    digest = sha256(target.read_bytes()).hexdigest()

    result = CodingAgent(tmp_path).apply_transaction([
        FileEdit("app.py", "return 'hello'", "return '안녕'", digest),
    ])

    assert result.succeeded
    assert target.read_text(encoding="utf-8") == original.replace("hello", "안녕")
    assert "-    return 'hello'" in result.diff
    assert "+    return '안녕'" in result.diff
    assert result.validation_output


def test_validation_failure_rolls_back_every_changed_file(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")

    result = CodingAgent(tmp_path).apply_transaction(
        [
            FileEdit("first.py", "value = 1", "value = 10"),
            FileEdit("second.py", "value = 2", "value = 20"),
        ],
        [[sys.executable, "-c", "raise SystemExit(3)"]],
    )

    assert not result.succeeded
    assert result.rolled_back
    assert first.read_text(encoding="utf-8") == "value = 1\n"
    assert second.read_text(encoding="utf-8") == "value = 2\n"
    assert "검증 실패" in result.error


def test_hash_conflict_and_ambiguous_patch_do_not_modify_user_file(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    agent = CodingAgent(tmp_path)

    conflict = agent.apply_transaction([
        FileEdit("app.py", "x = 1", "x = 2", "0" * 64),
    ])
    ambiguous = agent.apply_transaction([
        FileEdit("app.py", "x = 1", "x = 2"),
    ])

    assert not conflict.succeeded and "충돌" in conflict.error
    assert not ambiguous.succeeded and "일치 2개" in ambiguous.error
    assert target.read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_workspace_escape_is_rejected(tmp_path):
    result = CodingAgent(tmp_path).apply_transaction([
        FileEdit("../outside.py", "x", "y"),
    ])
    assert not result.succeeded
    assert "Workspace 외부" in result.error


def test_self_development_protected_parts_are_not_indexed_or_writable(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "main.py").write_text("title = 'Anis'\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memory.json").write_text("{}", encoding="utf-8")
    agent = CodingAgent(tmp_path, denied_parts={"data"})

    snapshot = agent.analyze_repository()
    result = agent.apply_transaction([
        FileEdit("data/memory.json", "{}", '{"changed": true}'),
    ])

    assert any("main.py" in name for name in snapshot.files)
    assert not any("memory.json" in name for name in snapshot.files)
    assert not result.succeeded and "보호된 프로젝트 영역" in result.error


def test_coding_plugin_returns_typed_repository_and_patch_evidence(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    plugin = CodingPlugin()
    monkeypatch.setattr(plugin.workspace, "get_workspace_path", lambda: str(tmp_path))

    snapshot = plugin.execute_tool("coding_analyze_repository", {})
    changed = plugin.execute_tool("coding_apply_patch", {
        "edits": [{"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}],
    })

    assert isinstance(snapshot, ToolRunResult) and snapshot.succeeded
    assert snapshot.evidence[0].kind == "repository_snapshot"
    assert isinstance(changed, ToolRunResult) and changed.succeeded
    assert changed.evidence[0].kind == "coding_transaction"
    assert changed.artifacts[0].uri == str(target)
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_build_plan_finds_python_symbol_related_file_and_test(tmp_path):
    source = tmp_path / "service.py"
    test_file = tmp_path / "test_service.py"
    source.write_text(
        "class UserService:\n    def create_user(self):\n        return True\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from service import UserService\n\ndef test_create_user():\n    assert UserService().create_user()\n",
        encoding="utf-8",
    )

    plan = CodingAgent(tmp_path).build_plan("UserService create_user 동작을 수정해줘")

    assert "service.py" in plan.related_files
    assert any(symbol["name"] == "UserService" for symbol in plan.related_symbols)
    assert "test_service.py" in plan.impact_scope
    assert any(command[2:4] == ["pytest", "-q"] for command in plan.validation_commands)


def test_build_plan_ranks_ui_implementation_above_document_mentions(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "main_window.py").write_text(
        "class MainWindow:\n    def update_status(self):\n        pass\n", encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ui_notes.md").write_text(
        "UI 메인 화면 상태 표시를 읽기 쉽게 수정하는 설명 문서", encoding="utf-8",
    )

    plan = CodingAgent(tmp_path).build_plan("너의 메인 UI 상단 상태 표시를 읽기 쉽게 수정해줘")

    normalized = [path.replace("\\", "/") for path in plan.related_files]
    assert "ui/main_window.py" in normalized[:3]


def test_default_validation_runs_related_test_and_rolls_back_on_failure(tmp_path):
    source = tmp_path / "math_service.py"
    test_file = tmp_path / "test_math_service.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test_file.write_text(
        "from math_service import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    result = CodingAgent(tmp_path).apply_transaction([
        FileEdit("math_service.py", "return a + b", "return a - b"),
    ])

    assert not result.succeeded
    assert result.rolled_back
    assert "1 failed" in result.error
    assert "return a + b" in source.read_text(encoding="utf-8")


def test_manifest_validation_detects_javascript_quality_pipeline(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","typecheck":"tsc --noEmit","test":"vitest","build":"vite build"}}',
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text("const value = 1;\n", encoding="utf-8")
    monkeypatch.setattr("core.coding_agent.shutil.which", lambda name: f"C:/tools/{name}.cmd")

    commands = CodingAgent(tmp_path)._validation_commands_for_relative_paths(["app.js"])
    flattened = [" ".join(command) for command in commands]

    assert any("node --check" in command for command in flattened)
    assert any("run --if-present lint" in command for command in flattened)
    assert any("run --if-present typecheck" in command for command in flattened)
    assert any("run --if-present test" in command for command in flattened)
    assert any("run --if-present build" in command for command in flattened)


def test_diff_self_review_rejects_conflict_markers_without_writing(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = CodingAgent(tmp_path).apply_transaction([
        FileEdit("app.py", "value = 1", "<<<<<<< ours\nvalue = 2\n=======\nvalue = 3\n>>>>>>> theirs"),
    ])

    assert not result.succeeded
    assert "병합 충돌 표식" in result.error
    assert not result.rolled_back
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_failed_validation_can_repair_and_reverify_with_bounded_retry(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    failures = []

    def repair(result, attempt):
        failures.append((attempt, result.error))
        return [FileEdit("app.py", "value = 1", "value = 2")]

    result = CodingAgent(tmp_path).apply_with_recovery(
        [FileEdit("app.py", "value = 1", "value =")], repair, max_attempts=2,
    )

    assert result.succeeded
    assert result.attempt_count == 2
    assert failures and "py_compile" in failures[0][1]
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_natural_language_feature_request_requires_test_and_creates_it_atomically(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")

    class FeatureLLM:
        def chat(self, _messages):
            return json.dumps({
                "summary": "한국어 인사 기능",
                "change_kind": "feature",
                "edits": [
                    {"path": "service.py", "old_text": "return 'hello'",
                     "new_text": "return '안녕'", "create": False},
                    {"path": "test_service.py", "old_text": "",
                     "new_text": "from service import greet\n\ndef test_greet():\n    assert greet() == '안녕'\n",
                     "create": True},
                ],
            }, ensure_ascii=False)

    _plan, result = CodingAgent(tmp_path).execute_request(
        "greet 함수에 한국어 인사 기능을 구현해줘", FeatureLLM()
    )

    assert result.succeeded
    assert set(result.changed_files) == {"service.py", "test_service.py"}
    assert (tmp_path / "test_service.py").is_file()
    assert "1 passed" in result.validation_output


def test_feature_without_test_is_rejected_before_writing(tmp_path):
    target = tmp_path / "service.py"
    target.write_text("value = 1\n", encoding="utf-8")

    class MissingTestLLM:
        def chat(self, _messages):
            return json.dumps({
                "summary": "새 기능", "change_kind": "feature",
                "edits": [{"path": "service.py", "old_text": "value = 1",
                           "new_text": "value = 2", "create": False}],
            }, ensure_ascii=False)

    try:
        CodingAgent(tmp_path).execute_request("새 기능 구현", MissingTestLLM())
        assert False, "테스트 없는 feature는 거절되어야 합니다."
    except ValueError as exc:
        assert "테스트 edit" in str(exc)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
