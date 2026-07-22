"""Git tools using argument-list subprocess calls only."""
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema


class GitPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "git"
        self.description = "Git 저장소 조회와 승인 기반 변경 작업"

    def get_tools(self) -> List[ToolSchema]:
        repo = {"repo_path": {"type": "string", "description": "Git 저장소 경로"}}
        return [
            ToolSchema("git_status", "Git 상태를 조회합니다", {"type": "object", "properties": repo, "required": ["repo_path"]}, ["filesystem_read"]),
            ToolSchema("git_diff", "Git diff를 조회합니다", {"type": "object", "properties": repo, "required": ["repo_path"]}, ["filesystem_read"]),
            ToolSchema("git_log", "최근 Git 로그를 조회합니다", {"type": "object", "properties": {**repo, "limit": {"type": "integer", "default": 10}}, "required": ["repo_path"]}, ["filesystem_read"]),
            ToolSchema("git_commit", "현재 stage된 변경을 커밋합니다", {"type": "object", "properties": {**repo, "message": {"type": "string"}}, "required": ["repo_path", "message"]}, ["git_commit"]),
            ToolSchema("git_push", "현재 브랜치를 원격 저장소로 push합니다", {"type": "object", "properties": repo, "required": ["repo_path"]}, ["git_push"]),
            ToolSchema("git_pull", "원격 변경을 fast-forward 방식으로 받습니다", {"type": "object", "properties": repo, "required": ["repo_path"]}, ["git_push"]),
        ]

    @staticmethod
    def _repo(path: str) -> Path:
        ok, message = SafetyLayer.validate_path(path)
        if not ok:
            raise ValueError(message)
        repo = Path(path).expanduser().resolve()
        if not (repo / ".git").exists():
            raise ValueError(f"Git 저장소가 아닙니다: {path}")
        return repo

    @staticmethod
    def _run(repo: Path, args: List[str]) -> str:
        result = subprocess.run(["git", "-C", str(repo), *args], shell=False, capture_output=True,
                                text=True, timeout=60, encoding="utf-8", errors="replace")
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            return f"오류: git 종료 코드 {result.returncode}: {output}"
        return output or "성공"

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        try:
            repo = self._repo(str(tool_input["repo_path"]))
            commands = {
                "git_status": ["status", "--short", "--branch"],
                "git_diff": ["diff", "--"],
                "git_commit": ["commit", "-m", str(tool_input.get("message", ""))],
                "git_push": ["push"],
                "git_pull": ["pull", "--ff-only"],
            }
            if tool_name == "git_log":
                limit = max(1, min(int(tool_input.get("limit", 10)), 100))
                return self._run(repo, ["log", f"-{limit}", "--oneline", "--decorate"])
            if tool_name not in commands:
                return f"오류: 알 수 없는 툴 '{tool_name}'"
            return self._run(repo, commands[tool_name])
        except Exception as exc:
            return f"오류: {exc}"
