"""Workspace catalog, project profile, and bootstrap tools."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict, List

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.project_bootstrap import ProjectBootstrapper
from core.project_indexer import get_project_indexer
from core.tool_result import Artifact, Evidence, ToolRunResult
from core.workspace import get_workspace_manager


class WorkspacePlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "workspace"
        self.description = "Workspace 목록, 프로젝트 분석, 템플릿 기반 초기화"
        self.manager = get_workspace_manager()
        self.indexer = get_project_indexer()
        self.bootstrapper = ProjectBootstrapper()

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("workspace_list", "저장된 Workspace 목록을 조회합니다.",
                       {"type": "object", "properties": {}}, ["filesystem_read"], side_effect="read"),
            ToolSchema("workspace_set_alias", "현재 Workspace 별칭을 저장합니다.", {
                "type": "object", "properties": {"alias": {"type": "string"}}, "required": ["alias"]
            }, ["filesystem_write"], side_effect="change"),
            ToolSchema("workspace_project_profile", "현재 프로젝트 언어, 프레임워크, 테스트 명령을 감지합니다.",
                       {"type": "object", "properties": {}}, ["filesystem_read"], side_effect="read"),
            ToolSchema("workspace_create_project", "템플릿, 가상환경, Git을 포함한 새 프로젝트를 만듭니다.", {
                "type": "object", "properties": {
                    "parent": {"type": "string"}, "name": {"type": "string"},
                    "template": {"type": "string", "enum": self.bootstrapper.available_templates(), "default": "python-basic"},
                    "create_venv": {"type": "boolean", "default": True},
                    "init_git": {"type": "boolean", "default": True},
                }, "required": ["parent", "name"]
            }, ["filesystem_write", "shell_execute"], side_effect="change"),
        ]

    def get_intents(self) -> List[IntentSchema]:
        return [IntentSchema(
            "workspace.create_project", "템플릿 기반 새 개발 프로젝트 생성", "workspace_create_project",
            ["새 프로젝트", "프로젝트 초기화", "가상환경", "Git 초기화"],
            [SlotSchema("parent", "상위 폴더", "어느 폴더에 만들까요?", role="target"),
             SlotSchema("name", "프로젝트 이름", "프로젝트 이름을 알려주세요."),
             SlotSchema("template", "프로젝트 템플릿", "어떤 템플릿을 사용할까요?", required=False)],
            execution_hints=["생성", "초기화", "만들"], request_type="change",
        )]

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        try:
            if tool_name == "workspace_list":
                payload = [asdict(info) for info in self.manager.list_workspaces()]
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("workspace_catalog", "저장된 Workspace 목록을 조회했습니다.", {"count": len(payload)})],
                )
            if tool_name == "workspace_set_alias":
                if not self.manager.set_alias(str(tool_input["alias"])):
                    return ToolRunResult.failed(tool_name=tool_name, error="현재 Workspace가 없습니다.")
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output="Workspace 별칭을 저장했습니다.",
                    evidence=[Evidence("workspace_alias", "별칭 설정 파일을 확인했습니다.")],
                )
            if tool_name == "workspace_project_profile":
                payload = self.indexer.detect_project_profile()
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("project_profile", "프로젝트 manifest와 파일 확장자를 분석했습니다.", payload)],
                )
            if tool_name == "workspace_create_project":
                result = self.bootstrapper.create(
                    str(tool_input["parent"]), str(tool_input["name"]),
                    str(tool_input.get("template", "python-basic")),
                    bool(tool_input.get("create_venv", True)), bool(tool_input.get("init_git", True)),
                )
                self.manager.set_workspace(result.path)
                self.indexer.set_project_root(result.path)
                self.indexer.sync_changes()
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(asdict(result), ensure_ascii=False),
                    evidence=[Evidence("project_bootstrap", "프로젝트 파일과 초기화 상태를 확인했습니다.", asdict(result))],
                    artifacts=[Artifact("directory", result.path, {"template": result.template})],
                )
            return ToolRunResult.failed(tool_name=tool_name, error=f"지원하지 않는 Tool입니다: {tool_name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
