from types import SimpleNamespace

import pytest

from core.planner import Planner, PlanningError
from core.scratchpad import Scratchpad


class _Registry:
    def get_capability(self, name):
        return object() if name in {"tool_a", "tool_b", "word_create_document"} else None


def test_plan_contract_rejects_multiple_tools_in_one_step(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())
    with pytest.raises(PlanningError, match="정확히 하나"):
        Planner._validated_tasks([{
            "id": "task_1",
            "description": "두 도구를 한꺼번에 실행",
            "required_tools": ["tool_a", "tool_b"],
        }], ["tool_a", "tool_b"])


def test_plan_contract_rejects_unknown_dependency(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())
    with pytest.raises(PlanningError, match="dependency"):
        Planner._validated_tasks([{
            "id": "task_1",
            "description": "실행",
            "required_tools": ["tool_a"],
            "dependencies": ["missing"],
        }], ["tool_a"])


def test_planner_retries_invalid_json_without_creating_fake_fallback(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())
    monkeypatch.setattr("core.planner.get_tools_description_text", lambda **_kwargs: "tools")

    class _LLM:
        def __init__(self):
            self.calls = 0

        def chat(self, _messages):
            self.calls += 1
            if self.calls < 3:
                return '{"tasks": []}'
            return (
                '{"tasks":[{"id":"task_1","description":"실행",'
                '"required_tools":["tool_a"],"dependencies":[]}]}'
            )

    planner = Planner.__new__(Planner)
    planner.llm = _LLM()
    planner.scratchpad = Scratchpad()
    planner.context_manager = SimpleNamespace()

    tasks = planner.decompose_goal("작업해줘", allowed_tool_names=["tool_a"])

    assert planner.llm.calls == 3
    assert [task.required_tools for task in tasks] == [["tool_a"]]
    assert len(planner.scratchpad.tasks) == 1


def test_planner_raises_after_three_invalid_contracts(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())
    monkeypatch.setattr("core.planner.get_tools_description_text", lambda **_kwargs: "tools")

    class _LLM:
        def chat(self, _messages):
            return '{"tasks": []}'

    planner = Planner.__new__(Planner)
    planner.llm = _LLM()
    planner.scratchpad = Scratchpad()
    planner.context_manager = SimpleNamespace()

    with pytest.raises(PlanningError, match="3회"):
        planner.decompose_goal("작업해줘", allowed_tool_names=["tool_a"])
    assert planner.scratchpad.tasks == []


def test_plan_contract_rejects_dropped_quoted_user_content(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())
    task = {
        "id": "task_1",
        "description": "Word 문서 작성",
        "required_tools": ["word_create_document"],
        "dependencies": [],
        "tool_input": {"path": "report.docx"},
    }

    with pytest.raises(PlanningError, match="원문이 tool_input에서 누락"):
        Planner._validated_tasks(
            [task], ["word_create_document"],
            original_goal='제목은 "전문가 검증", 본문은 "실제 내용"으로 작성해줘.',
        )


def test_plan_contract_accepts_preserved_quoted_user_content(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())
    tasks = Planner._validated_tasks([{
        "id": "task_1",
        "description": "Word 문서 작성",
        "required_tools": ["word_create_document"],
        "dependencies": [],
        "tool_input": {
            "path": "report.docx",
            "title": "전문가 검증",
            "paragraphs": ["실제 내용"],
        },
    }], ["word_create_document"],
        original_goal='제목은 "전문가 검증", 본문은 "실제 내용"으로 작성해줘.',
    )

    assert tasks[0].tool_input["title"] == "전문가 검증"
    assert tasks[0].tool_input["paragraphs"] == ["실제 내용"]


def test_compound_plan_contract_rejects_a_missing_required_tool(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())

    with pytest.raises(PlanningError, match="독립 작업이 계획에서 누락"):
        Planner._validated_tasks([{
            "id": "task_1",
            "description": "첫 번째 작업만 실행",
            "required_tools": ["tool_a"],
            "dependencies": [],
        }], ["tool_a", "tool_b"], required_tool_names=["tool_a", "tool_b"])


def test_compound_planner_retries_until_every_required_tool_is_present(monkeypatch):
    monkeypatch.setattr("core.planner.get_plugin_registry", lambda: _Registry())
    monkeypatch.setattr("core.planner.get_tools_description_text", lambda **_kwargs: "tools")

    class _LLM:
        def __init__(self):
            self.calls = 0

        def chat(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return ('{"tasks":[{"id":"task_1","description":"첫 작업",'
                        '"required_tools":["tool_a"],"dependencies":[]}]}')
            return ('{"tasks":['
                    '{"id":"task_1","description":"첫 작업",'
                    '"required_tools":["tool_a"],"dependencies":[]},'
                    '{"id":"task_2","description":"둘째 작업",'
                    '"required_tools":["tool_b"],"dependencies":["task_1"]}]}')

    planner = Planner.__new__(Planner)
    planner.llm = _LLM()
    planner.scratchpad = Scratchpad()
    planner.context_manager = SimpleNamespace()

    tasks = planner.decompose_goal(
        "두 작업을 해줘", allowed_tool_names=["tool_a", "tool_b"],
        required_tool_names=["tool_a", "tool_b"],
    )

    assert planner.llm.calls == 2
    assert [task.required_tools[0] for task in tasks] == ["tool_a", "tool_b"]
