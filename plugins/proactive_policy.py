"""Interruption policy, notification digest, proposals, and scheduler health tools."""
from __future__ import annotations

from typing import Any, Dict, List

from core.plugin import BasePlugin, ToolSchema
from core.proactive_runtime import InterruptionContextManager, ProactiveStore
from core.scheduler import get_automation_engine
from core.runtime.event_bus import Event, get_event_bus
from core.tool_result import Evidence, ToolRunResult


class ProactivePolicyPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "proactive_policy"
        self.description = "방해 제어·알림 보류함·선제 제안·Scheduler 운영 진단"
        self.context = InterruptionContextManager()
        self.store = ProactiveStore()

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("interruption_status", "집중·회의·전체화면·방해 금지 상태를 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False},
                ["proactive_read"], side_effect="read", verification_required=False),
            ToolSchema("interruption_update", "사용자가 명시한 방해 제어 상태를 영구 저장합니다", {
                "type": "object", "properties": {
                    "focus_mode": {"type": "boolean"}, "meeting": {"type": "boolean"},
                    "fullscreen": {"type": "boolean"}, "do_not_disturb": {"type": "boolean"}},
                "minProperties": 1, "additionalProperties": False},
                ["proactive_manage"], side_effect="change"),
            ToolSchema("proactive_pending_notifications", "보류된 알림과 알림 이유·근거를 조회합니다", {
                "type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "additionalProperties": False}, ["proactive_read"], side_effect="read"),
            ToolSchema("proactive_create_proposal", "실행하지 않고 선제 행동 제안만 저장합니다", {
                "type": "object", "properties": {"description": {"type": "string", "minLength": 1},
                    "tool_name": {"type": "string", "minLength": 1}, "tool_input": {"type": "object"},
                    "reason": {"type": "string", "minLength": 1}},
                "required": ["description", "tool_name", "tool_input", "reason"], "additionalProperties": False},
                ["proactive_read"], side_effect="change"),
            ToolSchema("proactive_approve_proposal", "제안을 승인 상태로 바꾸지만 대상 Tool을 실행하지는 않습니다", {
                "type": "object", "properties": {"proposal_id": {"type": "string", "minLength": 1}},
                "required": ["proposal_id"], "additionalProperties": False},
                ["automation"], side_effect="change"),
            ToolSchema("proactive_proposal_status", "선제 제안의 승인 상태와 원래 Tool 계약을 조회합니다", {
                "type": "object", "properties": {"proposal_id": {"type": "string", "minLength": 1}},
                "required": ["proposal_id"], "additionalProperties": False},
                ["proactive_read"], side_effect="read"),
            ToolSchema("scheduler_runtime_health", "Scheduler heartbeat·복원·실행 상태를 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False},
                ["proactive_read"], side_effect="read", verification_required=False),
            ToolSchema("scheduler_accelerated_soak", "Scheduler loop를 가속 반복해 장시간 안정성을 점검합니다", {
                "type": "object", "properties": {"iterations": {"type": "integer", "minimum": 1, "maximum": 100000}},
                "additionalProperties": False}, ["automation"], side_effect="execute"),
        ]

    @staticmethod
    def _ok(name: str, raw: str, kind: str, detail: dict):
        return ToolRunResult.successful(tool_name=name, raw_output=raw,
                                        evidence=[Evidence(kind, raw, detail)])

    def execute_tool(self, name: str, data: Dict[str, Any]):
        try:
            if name == "interruption_status":
                context = self.context.load()
                return self._ok(name, "현재 방해 제어 상태를 확인했습니다.", "interruption_context", context.__dict__)
            if name == "interruption_update":
                context = self.context.update(**data)
                get_event_bus().publish(Event("interruption_context_changed", self.name,
                                             data={"context": context.__dict__}))
                return self._ok(name, "방해 제어 상태를 저장했습니다.", "interruption_context", context.__dict__)
            if name == "proactive_pending_notifications":
                items = self.store.pending(int(data.get("limit", 20)))
                return self._ok(name, f"보류된 알림 {len(items)}건을 조회했습니다.", "notification_queue", {"items": items})
            if name == "proactive_create_proposal":
                proposal = self.store.propose(data["description"], data["tool_name"], data["tool_input"], data["reason"])
                return self._ok(name, "선제 제안을 저장했습니다. 실제 작업은 실행하지 않았습니다.",
                                "proactive_proposal", proposal)
            if name == "proactive_approve_proposal":
                proposal = self.store.approve(data["proposal_id"])
                proposal["execution_required"] = True
                return self._ok(name, "제안을 승인했습니다. 대상 Tool은 별도 권한 검사를 거쳐 실행해야 합니다.",
                                "proposal_approval", proposal)
            if name == "proactive_proposal_status":
                proposal = self.store.get_proposal(data["proposal_id"])
                if not proposal:
                    raise ValueError("제안을 찾지 못했습니다.")
                return self._ok(name, "선제 제안 상태를 확인했습니다.", "proactive_proposal", proposal)
            if name == "scheduler_runtime_health":
                detail = get_automation_engine().runtime_diagnostics()
                return self._ok(name, "Scheduler 운영 상태를 확인했습니다.", "scheduler_health", detail)
            if name == "scheduler_accelerated_soak":
                detail = get_automation_engine().soak_test(int(data.get("iterations", 1000)))
                if detail["failures"]:
                    return ToolRunResult.failed(tool_name=name, error=str(detail["failures"]),
                                                evidence=[Evidence("scheduler_soak", "가속 soak 중 오류가 발생했습니다.", detail)])
                return self._ok(name, f"Scheduler {detail['iterations']}회 가속 soak를 통과했습니다.",
                                "scheduler_soak", detail)
            raise ValueError(f"지원하지 않는 도구: {name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name, error=str(exc))
