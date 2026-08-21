"""Anis self-inspection and permission-gated self-development plugin."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List
import json

from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema, get_plugin_registry
from core.self_development import SelfDevelopmentRuntime
from core.tool_result import Artifact, Evidence, ToolRunResult


class AgentSelfPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "agent_self"
        self.description = "아니스 자신의 실제 상태·역량 조회와 승인 기반 자체 프로젝트 수정"
        self._runtime = SelfDevelopmentRuntime()

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema(
                "agent_self_status", "아니스 자신의 런타임·저장소·플러그인·권한 상태를 실제 진단합니다",
                {"type": "object", "properties": {}, "additionalProperties": False},
                ["filesystem_read"], side_effect="read",
            ),
            ToolSchema(
                "agent_self_capabilities", "Plugin Registry를 기준으로 아니스가 할 수 있는 일을 동적으로 조회합니다",
                {"type": "object", "properties": {"query": {"type": "string"}},
                 "additionalProperties": False},
                [], side_effect="read",
            ),
            ToolSchema(
                "agent_self_plan_change", "아니스 자체 프로젝트 수정의 관련 파일·영향·검증 계획을 세웁니다",
                {"type": "object", "properties": {"request": {"type": "string"}},
                 "required": ["request"], "additionalProperties": False},
                ["filesystem_read"], side_effect="read",
            ),
            ToolSchema(
                "agent_self_execute_change", "명시적 승인 후 아니스 자체 코드·UI를 최소 patch하고 검증·롤백합니다",
                {"type": "object", "properties": {"request": {"type": "string"}},
                 "required": ["request"], "additionalProperties": False},
                ["filesystem_read", "filesystem_write", "shell_execute", "self_modify"],
                side_effect="change", timeout_seconds=360, max_retries=0, cancellable=True,
            ),
        ]

    def get_intents(self) -> List[IntentSchema]:
        return [
            IntentSchema(
                "self.status", "아니스 자신의 현재 상태와 문제 진단", "agent_self_status",
                ["너의 상태 분석", "본인 상태 진단", "아니스 상태 확인", "자기 상태 분석"], [],
                execution_hints=["상태", "진단", "분석", "점검"],
                utterance_patterns=[
                    r"(?:너|네|본인|자기|아니스|자비스).{0,30}(?:상태|런타임|플러그인|권한).{0,20}(?:분석|진단|확인|점검)",
                    r"(?:상태|런타임|플러그인|권한).{0,20}(?:분석|진단|확인|점검).{0,20}(?:너|네|본인|아니스|자비스)",
                ], request_type="query",
            ),
            IntentSchema(
                "self.capabilities", "아니스의 Registry 기반 현재 수행 가능 범위", "agent_self_capabilities",
                ["너는 뭘 할 수 있어", "아니스 기능 알려줘", "어떤 작업이 가능해"],
                [SlotSchema("query", "관심 작업", "어떤 종류의 역량을 확인할까요?", required=False)],
                execution_hints=["할 수", "가능", "기능", "역량"],
                utterance_patterns=[
                    r"(?:너|네|아니스|자비스).{0,30}(?:뭘|무엇|어떤|기능|역량).{0,20}(?:할 수|가능|있어|알려)",
                    r"(?:할 수 있는|가능한).{0,20}(?:일|기능|작업|범위)",
                ], request_type="query",
            ),
            IntentSchema(
                "self.plan_change", "아니스 자체 변경을 실행 전에 분석하고 계획", "agent_self_plan_change",
                ["너의 UI 수정 계획", "아니스 코드 변경 계획", "자기 개선 계획"],
                [SlotSchema("request", "자체 변경 요청", "어떤 자체 변경을 계획할까요?")],
                execution_hints=["계획", "분석"],
                utterance_patterns=[
                    r"(?:너|네|본인|자기|아니스|자비스).{0,25}(?:코드|UI|화면|기능).{0,25}(?:수정|변경|개선).{0,15}(?:계획|분석)",
                ], request_type="query",
            ),
            IntentSchema(
                "self.execute_change", "아니스 자체 코드·UI의 승인 기반 실제 변경", "agent_self_execute_change",
                ["너의 UI 수정", "아니스 코드 고쳐", "본인 기능 추가", "자기 자신 수정"],
                [SlotSchema("request", "자체 변경 요청", "아니스 자신의 무엇을 어떻게 수정할까요?")],
                execution_hints=["수정", "고쳐", "변경", "개선", "추가", "구현"],
                utterance_patterns=[
                    r"(?:너|네|본인|자기|아니스|자비스).{0,25}(?:코드|UI|화면|기능|프로젝트).{0,30}(?:수정|고쳐|변경|개선|추가|구현)",
                    r"(?:코드|UI|화면|기능|프로젝트).{0,25}(?:수정|고쳐|변경|개선|추가|구현).{0,20}(?:너|네|본인|자기|아니스|자비스)",
                ], request_type="change",
            ),
        ]

    def extract_slots(self, intent_name: str, text: str, current_slots: Dict[str, Any]):
        slots = dict(current_slots)
        if intent_name in {"self.plan_change", "self.execute_change"} and text.strip():
            slots["request"] = text.strip()
        elif intent_name == "self.capabilities":
            slots["query"] = text.strip()
        return slots

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        try:
            # A plugin can live in an isolated/test Registry as well as the
            # process-global Registry. Its capability report must describe the
            # Registry that actually owns it, not unrelated global state.
            registry = self.registry or get_plugin_registry()
            if tool_name == "agent_self_status":
                payload = self._runtime.inspect_status(registry)
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("self_status", "실제 저장소·Plugin Registry·권한·장치 런타임을 조회했습니다.",
                                       payload.get("summary", {}))],
                    artifacts=[Artifact("directory", str(self._runtime.root), {"role": "self_repository"})],
                )
            if tool_name == "agent_self_capabilities":
                payload = self._runtime.capability_inventory(registry, str(tool_input.get("query", "")))
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("registry_capabilities", "Plugin Registry 계약에서 현재 역량을 조회했습니다.",
                                       {"plugin_count": payload["plugin_count"],
                                        "tool_count": payload["tool_count"]})],
                )
            if tool_name == "agent_self_plan_change":
                plan = self._runtime.plan_change(str(tool_input["request"]))
                payload = asdict(plan)
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("self_change_plan", "자체 저장소의 관련 파일·영향 범위·검증 명령을 계산했습니다.",
                                       {"related_files": plan.related_files,
                                        "validation_commands": plan.validation_commands})],
                    artifacts=[Artifact("directory", str(self._runtime.root), {"role": "self_repository"})],
                )
            if tool_name == "agent_self_execute_change":
                from core.llm import get_llm_client
                plan, result = self._runtime.execute_change(
                    str(tool_input["request"]), get_llm_client("coding")
                )
                if not result.succeeded:
                    return ToolRunResult.failed(tool_name=tool_name, error=result.error)
                payload = {
                    "status": result.status, "summary": result.summary,
                    "changed_files": result.changed_files, "diff": result.diff,
                    "validation_output": result.validation_output,
                    "attempt_count": result.attempt_count,
                }
                return ToolRunResult.successful(
                    tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("self_change_transaction", "자체 변경을 원자적으로 적용하고 검증했습니다.",
                                       {"planned_files": plan.related_files,
                                        "changed_files": result.changed_files,
                                        "diff_sha256": sha256(result.diff.encode("utf-8")).hexdigest(),
                                        "attempt_count": result.attempt_count})],
                    artifacts=[Artifact("file", str(Path(self._runtime.root, name)), {"changed": True})
                               for name in result.changed_files],
                )
            return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 Tool: {tool_name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))

    def present_result(self, tool_name: str, result: str) -> str:
        try:
            payload = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
        if tool_name == "agent_self_status":
            summary = payload.get("summary", {})
            problems = summary.get("problems", [])
            return ("현재 상태는 정상입니다." if not problems else
                    "현재 확인된 문제는 " + " ".join(str(item) for item in problems))
        if tool_name == "agent_self_capabilities":
            labels = [
                f"{item['name']}({len(item.get('tools', []))}개)"
                for item in payload.get("plugins", [])
            ]
            return (f"현재 {payload.get('plugin_count', 0)}개 분야, {payload.get('tool_count', 0)}개 도구를 사용할 수 있습니다. "
                    + ", ".join(labels[:12]))
        if tool_name == "agent_self_plan_change":
            files = payload.get("related_files", [])
            return f"자체 변경 계획을 세웠습니다. 관련 파일 {len(files)}개를 확인했고 실행 전 승인이 필요합니다."
        if tool_name == "agent_self_execute_change":
            files = payload.get("changed_files", [])
            return (f"자체 변경과 검증을 완료했습니다. 변경 파일: {', '.join(files)}. "
                    "실행 중인 화면·코드 변경은 앱을 다시 시작하면 적용됩니다.")
        return result
