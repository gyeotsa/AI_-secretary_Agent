"""Repository analysis and atomic minimal-patch tools."""
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List
import json

from core.coding_agent import CodingAgent, FileEdit
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult
from core.workspace import get_workspace_manager


class CodingPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "coding"
        self.description = "저장소 분석과 검증·롤백 가능한 최소 코드 변경"
        self.workspace = get_workspace_manager()

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema(
                "coding_analyze_repository", "현재 Workspace의 구조·의존성·Git 상태를 분석합니다",
                {"type": "object", "properties": {"max_files": {"type": "integer", "default": 5000}}},
                ["filesystem_read"], side_effect="read",
            ),
            ToolSchema(
                "coding_apply_patch", "해시로 사용자 변경을 보호하며 최소 patch 후 검증합니다",
                {
                    "type": "object",
                    "properties": {
                        "edits": {
                            "type": "array", "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string"},
                                    "expected_sha256": {"type": "string"},
                                },
                                "required": ["path", "old_text", "new_text"],
                            },
                        },
                        "validation_commands": {
                            "type": "array", "items": {
                                "type": "array", "items": {"type": "string"},
                            },
                        },
                    },
                    "required": ["edits"],
                },
                ["filesystem_write", "run_command"], side_effect="change",
            ),
            ToolSchema(
                "coding_plan_change", "관련 파일·심볼·영향 범위와 검증 전략을 계획합니다",
                {"type": "object", "properties": {"request": {"type": "string"}},
                 "required": ["request"]},
                ["filesystem_read"], side_effect="read",
            ),
        ]

    def _agent(self) -> CodingAgent:
        root = self.workspace.get_workspace_path()
        if not root:
            raise ValueError("먼저 상단에서 코드 Workspace를 선택해 주세요.")
        return CodingAgent(root)

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        try:
            agent = self._agent()
            if tool_name == "coding_analyze_repository":
                snapshot = agent.analyze_repository(int(tool_input.get("max_files", 5000)))
                payload = asdict(snapshot)
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence(
                        "repository_snapshot", "저장소 구조·의존성·Git 상태를 조회했습니다.",
                        {"root": snapshot.root, "file_count": len(snapshot.files),
                         "branch": snapshot.git_branch, "git_status": snapshot.git_status},
                    )],
                    artifacts=[Artifact("directory", snapshot.root, {"role": "repository"})],
                )
            if tool_name == "coding_apply_patch":
                edits = [FileEdit(**item) for item in tool_input["edits"]]
                commands = tool_input.get("validation_commands")
                result = agent.apply_transaction(edits, commands)
                if not result.succeeded:
                    return ToolRunResult.failed(tool_name=tool_name, error=result.error)
                diff_hash = sha256(result.diff.encode("utf-8")).hexdigest()
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=json.dumps({
                        "status": result.status,
                        "changed_files": result.changed_files,
                        "diff": result.diff,
                        "validation_output": result.validation_output,
                    }, ensure_ascii=False),
                    evidence=[Evidence(
                        "coding_transaction",
                        "최소 patch 적용, 검증 명령 성공과 최종 diff 생성을 확인했습니다.",
                        {"changed_files": result.changed_files, "diff_sha256": diff_hash,
                         "validation_output": result.validation_output},
                    )],
                    artifacts=[
                        Artifact("file", str(Path(agent.root, name)), {"changed": True})
                        for name in result.changed_files
                    ],
                )
            if tool_name == "coding_plan_change":
                plan = agent.build_plan(str(tool_input["request"]))
                payload = asdict(plan)
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence(
                        "coding_plan", "관련 파일·심볼·영향 범위와 검증 전략을 생성했습니다.",
                        {"related_files": plan.related_files,
                         "symbol_count": len(plan.related_symbols),
                         "impact_scope": plan.impact_scope,
                         "validation_commands": plan.validation_commands},
                    )],
                    artifacts=[Artifact("directory", str(agent.root), {"role": "planned_repository"})],
                )
            return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 Tool: {tool_name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
