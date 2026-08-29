"""Evidence-based self inspection and permission-gated repository development."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
import json
import platform
import urllib.request

from config import Config
from core.coding_agent import CodingAgent
from core.capability_audit import audit_capabilities
from core.permission import get_permission_manager
from core.tool_loadout import ToolLoadoutSelector


class SelfDevelopmentRuntime:
    """Own-project boundary for Anis; user workspaces remain a separate contract."""

    PROTECTED_PARTS = {
        ".git", ".venv", "venv", ".env", ".secrets", "secrets", "models",
        "data", "brain", "dist", "build", "release",
        "external", "tmp", "tmpdozr9d5t", "__pycache__", ".pytest_cache", ".pytest-tmp",
    }
    FORBIDDEN_REQUESTS = (
        "권한 검사 우회", "권한 검사를 우회", "보안 검사 우회", "보안 검사를 우회",
        "승인 없이", "비밀번호 보여", "토큰 보여", "api key 보여", "api 키 보여",
        "자격 증명 보여",
    )

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        self.agent = CodingAgent(self.root, denied_parts=set(self.PROTECTED_PARTS))

    def capability_inventory(self, registry, query: str = "") -> dict[str, Any]:
        selected: set[str] = set()
        selection_reason = "all_registry_capabilities"
        confidence = 1.0
        if str(query).strip():
            loadout = ToolLoadoutSelector(registry, max_tools=12).select(str(query))
            selected = set(loadout.tool_names)
            selection_reason = loadout.reason
            confidence = loadout.confidence
        permissions = get_permission_manager()
        status_by_name = {
            item.name: item for item in registry.get_plugin_statuses() if item.registered
        }
        plugins = []
        for plugin in registry.plugins.values():
            tools = []
            for schema in plugin.get_tools():
                if selected and schema.name not in selected:
                    continue
                tools.append({
                    "name": schema.name,
                    "description": schema.description,
                    "side_effect": schema.side_effect,
                    "required_permissions": list(schema.required_permissions),
                    "permission_ready": all(
                        permissions.check_permission(item)
                        for item in schema.required_permissions
                    ),
                })
            if tools:
                status = status_by_name.get(plugin.name)
                plugins.append({
                    "name": plugin.name, "description": plugin.description,
                    "enabled": plugin.enabled,
                    "connected": status.connected if status else None,
                    "connection_state": (
                        status.connection_state if status else "unchecked"
                    ),
                    "runtime_state": status.runtime_state if status else "unchecked",
                    "tools": tools,
                })
        from core.tools import AUTO_LOOP_EXCLUDED_TOOLS
        audit = audit_capabilities(
            registry,
            known_permission_ids=permissions.permissions,
            runtime_only_tools=AUTO_LOOP_EXCLUDED_TOOLS,
        )
        return {
            "query": str(query), "selection_reason": selection_reason,
            "confidence": confidence, "plugin_count": len(plugins),
            "tool_count": sum(len(item["tools"]) for item in plugins),
            "plugins": plugins,
            "capability_audit": audit.to_dict(),
        }

    def inspect_status(self, registry) -> dict[str, Any]:
        snapshot = self.agent.analyze_repository()
        plugin_statuses = [asdict(item) for item in registry.get_plugin_statuses()]
        unconfigured_plugins = [
            item for item in plugin_statuses
            if item.get("registered")
            and item.get("installation_state") == "confirmed"
            and item.get("contract_state") == "confirmed"
            and item.get("runtime_state") == "unchecked"
            and (
                item.get("connection_state") == "unchecked"
                or item.get("authentication_state") in {"unchecked", "failed"}
            )
        ]
        broken_plugins = [
            item for item in plugin_statuses
            if item.get("installation_state") == "failed"
            or item.get("contract_state") == "failed"
            or item.get("runtime_state") == "failed"
        ]
        unchecked_plugins = [
            item for item in plugin_statuses
            if item.get("verification_state") == "unchecked"
            and item not in unconfigured_plugins
        ]
        permissions = get_permission_manager().get_all_permissions()
        from core.tools import AUTO_LOOP_EXCLUDED_TOOLS
        capability_audit = audit_capabilities(
            registry,
            known_permission_ids=(item.id for item in permissions),
            runtime_only_tools=AUTO_LOOP_EXCLUDED_TOOLS,
        )
        report: dict[str, Any] = {
            "runtime": {
                "python": platform.python_version(), "platform": platform.platform(),
                "repository": snapshot.root, "branch": snapshot.git_branch,
            },
            "repository": {
                "file_count": len(snapshot.files), "git_status": snapshot.git_status,
                "dependency_files": snapshot.dependency_files,
            },
            "plugins": {
                "registered": sum(bool(item.get("registered")) for item in plugin_statuses),
                "verified": sum(
                    item.get("verification_state") == "confirmed"
                    for item in plugin_statuses
                ),
                "unconfigured": unconfigured_plugins,
                "broken": broken_plugins,
                "unchecked": unchecked_plugins,
            },
            "permissions": {
                "total": len(permissions),
                "allowed": sum(get_permission_manager().check_permission(item.id) for item in permissions),
                "self_modify": get_permission_manager().check_permission("self_modify"),
            },
            "capability_audit": capability_audit.to_dict(),
        }
        try:
            import psutil
            memory = psutil.virtual_memory()
            report["memory"] = {
                "used_percent": memory.percent,
                "available_mb": round(memory.available / 1024 ** 2),
            }
        except ImportError:
            report["memory"] = {"status": "psutil_not_installed"}
        try:
            import torch
            cuda = bool(torch.cuda.is_available())
            report["gpu"] = {
                "cuda_available": cuda,
                "device": torch.cuda.get_device_name(0) if cuda else "cpu",
            }
        except (ImportError, RuntimeError, OSError) as exc:
            report["gpu"] = {"cuda_available": False, "error": str(exc)}
        try:
            with urllib.request.urlopen(
                Config.OLLAMA_BASE_URL.rstrip("/") + "/api/tags", timeout=3
            ) as response:
                models = json.loads(response.read().decode("utf-8")).get("models", [])
            report["ollama"] = {
                "reachable": True,
                "models": sorted(str(item.get("name", "")) for item in models),
            }
        except Exception as exc:
            report["ollama"] = {"reachable": False, "error": str(exc)}
        problems = []
        if snapshot.git_status:
            problems.append("작업 트리에 커밋되지 않은 변경이 있습니다.")
        if report["plugins"]["broken"]:
            problems.append("계약 또는 의존성 문제가 있는 플러그인이 있습니다.")
        if report["plugins"]["unconfigured"]:
            problems.append("선택 계정 연동이 설정되지 않은 플러그인이 있습니다.")
        if not capability_audit.passed:
            problems.append("도구 계약·권한·도달성 감사에서 오류가 발견되었습니다.")
        if not report["ollama"]["reachable"]:
            problems.append("Ollama 서버에 연결하지 못했습니다.")
        if float(report.get("memory", {}).get("used_percent", 0)) >= 85:
            problems.append("RAM 사용률이 85% 이상입니다.")
        report["summary"] = {
            "status": "warning" if problems else "healthy", "problems": problems,
        }
        return report

    def plan_change(self, request: str):
        self._validate_request(request)
        return self.agent.build_plan(request)

    def execute_change(self, request: str, llm):
        self._validate_request(request)
        return self.agent.execute_request(request, llm)

    @classmethod
    def _validate_request(cls, request: str) -> None:
        normalized = " ".join(str(request or "").casefold().split())
        if not normalized:
            raise ValueError("자기 수정 요청이 비어 있습니다.")
        if any(marker in normalized for marker in cls.FORBIDDEN_REQUESTS):
            raise ValueError("권한·보안 우회 또는 자격 증명 노출 요청은 자기 수정으로 수행할 수 없습니다.")
