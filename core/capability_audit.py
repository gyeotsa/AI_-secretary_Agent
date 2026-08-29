"""Deterministic capability inventory for product QA and startup diagnostics."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from core.permission import TOOL_PERMISSION_MAP
from core.plugin import PluginRegistry


@dataclass(frozen=True)
class CapabilityAuditReport:
    plugin_count: int
    tool_count: int
    intent_count: int
    intent_bound_tools: tuple[str, ...]
    planner_reachable_tools: tuple[str, ...]
    runtime_only_tools: tuple[str, ...]
    errors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    warnings: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "plugin_count": self.plugin_count,
            "tool_count": self.tool_count,
            "intent_count": self.intent_count,
            "intent_bound_tools": list(self.intent_bound_tools),
            "planner_reachable_tools": list(self.planner_reachable_tools),
            "runtime_only_tools": list(self.runtime_only_tools),
            "errors": {key: list(value) for key, value in self.errors.items()},
            "warnings": {key: list(value) for key, value in self.warnings.items()},
        }


def audit_capabilities(
    registry: PluginRegistry,
    *,
    known_permission_ids: Iterable[str],
    runtime_only_tools: Iterable[str] = (),
) -> CapabilityAuditReport:
    """Audit registration, reachability, schema and permission references.

    Intent-bound tools have a deterministic natural-language route. Other
    model-callable tools remain reachable through descriptor-grounded Planner
    selection. Runtime-only tools must be explicitly declared so recursive or
    blocking tools are never accidentally counted as Planner capabilities.
    """
    tools = registry.get_all_tools()
    intents = registry.get_all_intents()
    tool_names = {tool.name for tool in tools}
    intent_bound = {intent.tool_name for _plugin, intent in intents}
    runtime_only = {str(name) for name in runtime_only_tools}
    known_permissions = {str(name) for name in known_permission_ids}
    errors: dict[str, list[str]] = {
        key: list(values) for key, values in registry.validate_contracts().items()
    }
    warnings: dict[str, list[str]] = {}

    for tool in tools:
        current = errors.setdefault(tool.name, [])
        required_fields = set(tool.input_schema.get("required", []) or [])
        properties = set((tool.input_schema.get("properties") or {}).keys())
        missing_properties = sorted(required_fields - properties)
        if missing_properties:
            current.append(
                "required에 선언했지만 properties에 없는 입력: "
                + ", ".join(missing_properties)
            )
        unknown_permissions = sorted(
            set(tool.required_permissions) - known_permissions
        )
        if unknown_permissions:
            current.append(
                "등록되지 않은 권한 ID 참조: " + ", ".join(unknown_permissions)
            )
        mapped = TOOL_PERMISSION_MAP.get(tool.name)
        if mapped and mapped not in known_permissions:
            current.append(f"TOOL_PERMISSION_MAP의 권한 ID가 등록되지 않음: {mapped}")
        if not current:
            errors.pop(tool.name, None)

    dangling_map = sorted(set(TOOL_PERMISSION_MAP) - tool_names)
    if dangling_map:
        warnings["permission_map"] = [
            "현재 Registry에 없는 레거시 Tool 매핑: " + ", ".join(dangling_map)
        ]
    unknown_runtime_only = sorted(runtime_only - tool_names)
    if unknown_runtime_only:
        warnings["runtime_only"] = [
            "현재 Registry에 없지만 모델 제외 목록에 남은 레거시 Tool: "
            + ", ".join(unknown_runtime_only)
        ]
    registered_runtime_only = runtime_only & tool_names
    planner_reachable = tool_names - registered_runtime_only
    unreachable = tool_names - intent_bound - planner_reachable - registered_runtime_only
    if unreachable:
        errors["reachability"] = [
            "Intent, Planner, Runtime 어디에서도 도달할 수 없는 Tool: "
            + ", ".join(sorted(unreachable))
        ]

    return CapabilityAuditReport(
        plugin_count=len(registry.plugins),
        tool_count=len(tools),
        intent_count=len(intents),
        intent_bound_tools=tuple(sorted(intent_bound)),
        planner_reachable_tools=tuple(sorted(planner_reachable)),
        runtime_only_tools=tuple(sorted(registered_runtime_only)),
        errors={key: tuple(values) for key, values in errors.items() if values},
        warnings={key: tuple(values) for key, values in warnings.items() if values},
    )
