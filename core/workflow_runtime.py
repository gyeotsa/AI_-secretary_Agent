"""Declarative, evidence-bearing workflow presets and source-aware daily briefs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
import json
import os
import sqlite3
import sys
import threading
import time
import uuid

from core.runtime.event_bus import Event, get_event_bus


@dataclass
class SourceSection:
    key: str
    title: str
    status: str
    value: Any
    source: str
    updated_at: str
    detail: str = ""


class MorningBriefService:
    """Builds a brief without silently substituting model knowledge for live data."""

    def __init__(self, *, tool_executor=None, dialogue_state=None, permission_manager=None,
                 plugin_registry=None, automation_engine=None, user_profile=None):
        self.tool_executor = tool_executor
        self.dialogue_state = dialogue_state
        self.permission_manager = permission_manager
        self.plugin_registry = plugin_registry
        self.automation_engine = automation_engine
        self.user_profile = user_profile

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()

    def build(self, *, session_id: str = "", workspace_path: str = "",
              location: str = "") -> dict[str, Any]:
        sections = [self._clock(), self._weather(location), self._calendar(),
                    self._tasks(session_id, workspace_path), self._approvals(),
                    self._priorities(), self._runtime_health()]
        return {"generated_at": self._now(), "sections": [asdict(item) for item in sections],
                "all_sources_available": all(item.status == "available" for item in sections)}

    def _clock(self) -> SourceSection:
        now = datetime.now().astimezone()
        return SourceSection("clock", "현재 시각", "available",
                             now.strftime("%Y년 %m월 %d일 %H:%M:%S %Z"),
                             "Windows system clock", now.isoformat())

    def _weather(self, location: str) -> SourceSection:
        if not location and self.user_profile is not None:
            location = (self.user_profile.get_preference("weather_location", "")
                        or self.user_profile.get("location", ""))
        if not location:
            return SourceSection("weather", "실시간 날씨", "disconnected", None,
                                 "Open-Meteo", self._now(), "사용자 위치가 설정되지 않았습니다.")
        if self.tool_executor is None:
            return SourceSection("weather", "실시간 날씨", "disconnected", None,
                                 "Open-Meteo", self._now(), "날씨 도구 실행기가 연결되지 않았습니다.")
        try:
            result = self.tool_executor.execute_tool("get_weather", {"location": location})
            if not getattr(result, "succeeded", False):
                return SourceSection("weather", "실시간 날씨", "unavailable", None,
                                     "Open-Meteo", self._now(), str(getattr(result, "error", result)))
            raw_output = getattr(result, "raw_output", "")
            try:
                value = json.loads(raw_output)
            except (TypeError, json.JSONDecodeError):
                # Some weather adapters intentionally return a concise Korean
                # sentence.  It is still live tool evidence and must not be
                # discarded merely because it is not JSON.
                value = {"summary": str(raw_output).strip(), "location": location}
            return SourceSection("weather", "실시간 날씨", "available", value,
                                 "Open-Meteo live API", value.get("retrieved_at", self._now()))
        except Exception as exc:
            return SourceSection("weather", "실시간 날씨", "unavailable", None,
                                 "Open-Meteo", self._now(), f"{type(exc).__name__}: {exc}")

    def _calendar(self) -> SourceSection:
        provider = os.getenv("JARVIS_CALENDAR_PROVIDER", "").strip().casefold()
        account = os.getenv("JARVIS_CALENDAR_ACCOUNT", "").strip()
        if self.user_profile is not None:
            provider = str(self.user_profile.get_preference(
                "calendar_provider", provider
            ) or provider).strip().casefold()
            account = str(self.user_profile.get_preference(
                "calendar_account", account
            ) or account).strip()
        if provider not in {"google", "microsoft"} or not account:
            return SourceSection("calendar", "오늘 일정", "disconnected", None,
                                 "Google/Microsoft Calendar", self._now(),
                                 "calendar_provider와 calendar_account를 설정하세요.")
        if self.tool_executor is None:
            return SourceSection("calendar", "오늘 일정", "disconnected", None,
                                 "Google/Microsoft Calendar", self._now(),
                                 "캘린더 도구 실행기가 연결되지 않았습니다.")
        try:
            result = self.tool_executor.execute_tool(
                "calendar_read_range", {"provider": provider, "account": account}
            )
            if not getattr(result, "succeeded", False):
                return SourceSection(
                    "calendar", "오늘 일정", "unavailable", None,
                    f"{provider.title()} Calendar live API", self._now(),
                    str(getattr(result, "error", result)),
                )
            evidence = next((
                item.data for item in getattr(result, "evidence", ())
                if getattr(item, "kind", "") == "calendar_events"
            ), {})
            return SourceSection(
                "calendar", "오늘 일정", "available",
                list(evidence.get("events", [])),
                f"{provider.title()} Calendar live API",
                str(evidence.get("retrieved_at") or self._now()),
            )
        except Exception as exc:
            return SourceSection(
                "calendar", "오늘 일정", "unavailable", None,
                f"{provider.title()} Calendar live API", self._now(),
                f"{type(exc).__name__}: {exc}",
            )

    def _tasks(self, session_id: str, workspace_path: str) -> SourceSection:
        if self.dialogue_state is None or not session_id:
            return SourceSection("tasks", "미완료 작업", "disconnected", None,
                                 "DialogueStateStore", self._now(), "현재 세션이 연결되지 않았습니다.")
        tasks = self.dialogue_state.list_tasks(session_id, include_finished=False,
                                               workspace_path=workspace_path or None)
        value = [{"id": item.task_id, "goal": item.goal, "status": item.status,
                  "updated_at": item.updated_at} for item in tasks]
        return SourceSection("tasks", "미완료 작업", "available", value,
                             "DialogueStateStore", self._now())

    def _approvals(self) -> SourceSection:
        if self.permission_manager is None:
            return SourceSection("approvals", "권한·승인", "disconnected", None,
                                 "PermissionManager", self._now())
        pending = [{"id": item.id, "name": item.name, "level": item.level.value}
                   for item in self.permission_manager.get_all_permissions()
                   if item.decision.value == "undecided" and item.level.value != "safe"]
        return SourceSection("approvals", "권한·승인", "available", pending,
                             "PermissionManager", self._now())

    def _priorities(self) -> SourceSection:
        if self.user_profile is None:
            return SourceSection("priorities", "습관·우선순위", "disconnected", None,
                                 "UserProfile", self._now())
        value = {"favorite_commands": self.user_profile.get_favorite_commands()[:5],
                 "favorite_paths": self.user_profile.get_favorite_paths()[:5]}
        return SourceSection("priorities", "습관·우선순위", "available", value,
                             "UserProfile activity store", self._now())

    def _runtime_health(self) -> SourceSection:
        plugins = self.plugin_registry.get_plugin_statuses() if self.plugin_registry else []
        issues = [{
            "plugin": item.name,
            "installation_state": item.installation_state,
            "contract_state": item.contract_state,
            "runtime_state": item.runtime_state,
            "diagnostics": item.diagnostics,
        } for item in plugins if (
            item.installation_state == "failed"
            or item.contract_state == "failed"
            or item.runtime_state == "failed"
        )]
        unchecked = [
            item.name for item in plugins if item.verification_state == "unchecked"
        ]
        scheduler = (self.automation_engine.runtime_diagnostics()
                     if self.automation_engine and hasattr(self.automation_engine, "runtime_diagnostics")
                     else {"status": "disconnected"})
        return SourceSection("runtime", "Plugin·자동화 상태", "available",
                             {"plugin_issues": issues,
                              "plugin_unchecked": unchecked,
                              "scheduler": scheduler},
                             "PluginRegistry + AutomationEngine", self._now())


@dataclass
class WorkflowStepResult:
    step_id: str
    action: str
    status: str
    output: Any = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    duration_ms: float = 0.0


class WorkflowRuntime:
    """Executes checked-in workflow recipes through real runtime adapters."""

    def __init__(self, config_path: str | None = None, *, tool_executor=None,
                 brief_service: MorningBriefService | None = None, diagnostics=None,
                 memory_maintenance: Callable[[], Any] | None = None,
                 context_provider: Callable[[], dict[str, Any]] | None = None,
                 db_path: str = "data/workflow_runs.db"):
        # Recipes belong to the application, not the selected user workspace.
        # Explicit overrides remain caller-relative for tests/custom recipes.
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        self.config_path = (Path(config_path) if config_path is not None
                            else bundle_root / "config" / "workflows.json")
        self.tool_executor = tool_executor
        self.brief_service = brief_service
        self.diagnostics = diagnostics
        self.memory_maintenance = memory_maintenance
        self.context_provider = context_provider or (lambda: {})
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS workflow_runs(
                run_id TEXT PRIMARY KEY, preset TEXT NOT NULL, status TEXT NOT NULL,
                payload TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL)""")

    def presets(self) -> list[dict[str, Any]]:
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        return list(payload.get("presets", []))

    def match_trigger(self, text: str) -> str | None:
        """Return the exact preset id for a configured conversational trigger.

        Triggers stay in the checked-in workflow recipe rather than being
        duplicated as Python keyword branches.  A short polite suffix is
        accepted, while arbitrary containment is rejected to avoid hijacking
        ordinary conversation.
        """
        normalized = " ".join(str(text or "").strip().casefold().split())
        normalized = normalized.rstrip(" .,!?:;~。！？")
        polite_suffixes = (
            "해줘", "해주세요", "시작해줘", "시작해주세요", "실행해줘", "실행해주세요",
        )
        candidates = {normalized}
        for suffix in polite_suffixes:
            if normalized.endswith(suffix):
                candidates.add(normalized[:-len(suffix)].strip())
        for preset in self.presets():
            for trigger in preset.get("trigger", []):
                candidate = " ".join(str(trigger).strip().casefold().split())
                if candidate in candidates:
                    return str(preset["id"])
        return None

    @staticmethod
    def present_run(run: dict[str, Any]) -> str:
        """Build a concise user response from actual workflow step results."""
        lines = [f"{run.get('label', run.get('preset', '워크플로'))} 워크플로: {run.get('status', 'unknown')}"]
        for item in run.get("results", []):
            status = item.get("status", "unknown")
            label = item.get("step_id") or item.get("action") or "step"
            if status == "failed":
                lines.append(f"- {label}: 실패 · {item.get('error', '원인 미상')}")
            elif status == "awaiting_approval":
                lines.append(f"- {label}: 승인이 필요합니다.")
            elif status == "skipped":
                lines.append(f"- {label}: 건너뜀 · {item.get('error', '')}")
            elif item.get("action") == "morning_brief" and isinstance(item.get("output"), dict):
                lines.append(f"- {label}: 완료")
                for section in item["output"].get("sections", []):
                    value = section.get("value")
                    if value in (None, [], {}):
                        value = section.get("detail") or "연결되지 않음"
                    elif isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False, default=str)
                    lines.append(
                        f"  · {section.get('title')}: {str(value)[:500]} "
                        f"(출처: {section.get('source')}, 갱신: {section.get('updated_at')})"
                    )
            else:
                lines.append(f"- {label}: {status}")
        return "\n".join(lines)

    def execute(self, preset_id: str, *, approve: Callable[[dict], bool] | None = None,
                context: dict[str, Any] | None = None) -> dict[str, Any]:
        preset = next((item for item in self.presets() if item.get("id") == preset_id), None)
        if preset is None:
            raise KeyError(f"등록되지 않은 워크플로입니다: {preset_id}")
        run_id, started = uuid.uuid4().hex[:12], datetime.now().astimezone().isoformat()
        merged = {**self.context_provider(), **(context or {})}
        results: list[WorkflowStepResult] = []
        get_event_bus().publish(Event("workflow.started", "workflow_runtime",
                                      data={"run_id": run_id, "preset": preset_id}))
        self._continue_steps(preset, merged, results, 0, approve)
        return self._finish_run(run_id, preset, started, merged, results)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM workflow_runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def resume(self, run_id: str, *, approve: bool,
               context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resume the exact persisted approval boundary, never restart from step zero."""
        payload = self.get_run(run_id)
        if payload is None:
            raise KeyError(f"워크플로 실행 기록을 찾지 못했습니다: {run_id}")
        if payload.get("status") != "awaiting_approval":
            raise ValueError("승인 대기 중인 워크플로만 재개할 수 있습니다.")
        preset = next(
            (item for item in self.presets() if item.get("id") == payload.get("preset")), None
        )
        if preset is None:
            raise KeyError(f"워크플로 설정이 삭제되었습니다: {payload.get('preset')}")
        if not approve:
            payload["status"] = "cancelled"
            payload["finished_at"] = datetime.now().astimezone().isoformat()
            payload["results"][-1]["status"] = "cancelled"
            payload["results"][-1]["error"] = "사용자가 승인하지 않았습니다."
            self._persist_run(payload, payload["started_at"])
            get_event_bus().publish(Event("workflow.cancelled", "workflow_runtime", data={
                "run_id": run_id, "preset": payload.get("preset"), "status": "cancelled",
            }))
            return payload
        merged = {**dict(payload.get("context") or {}), **(context or {})}
        previous = [WorkflowStepResult(**item) for item in payload.get("results", [])]
        awaiting_index = next(
            (index for index, item in enumerate(previous) if item.status == "awaiting_approval"), -1
        )
        if awaiting_index < 0:
            raise ValueError("승인 경계 정보를 찾지 못했습니다.")
        results = previous[:awaiting_index]
        self._continue_steps(
            preset, merged, results, awaiting_index,
            approve=lambda _step: True, approve_only_first=True,
        )
        return self._finish_run(
            run_id, preset, payload["started_at"], merged, results
        )

    def _continue_steps(self, preset: dict[str, Any], merged: dict[str, Any],
                        results: list[WorkflowStepResult], start_index: int,
                        approve: Callable[[dict], bool] | None,
                        approve_only_first: bool = False) -> None:
        for relative_index, step in enumerate(preset.get("steps", [])[start_index:]):
            if step.get("condition") and not self._condition(step["condition"], merged, results):
                results.append(WorkflowStepResult(step["id"], step["action"], "skipped",
                                                  error="조건이 충족되지 않았습니다."))
                continue
            approved_boundary = approve_only_first and relative_index == 0
            if step.get("requires_approval") and not approved_boundary and (
                    approve is None or not approve(step)):
                results.append(WorkflowStepResult(step["id"], step["action"], "awaiting_approval"))
                break
            results.append(self._execute_step(step, merged))
            if results[-1].status == "failed" and step.get("on_failure", "stop") == "stop":
                break
    def _finish_run(self, run_id: str, preset: dict[str, Any], started: str,
                    merged: dict[str, Any], results: list[WorkflowStepResult]) -> dict[str, Any]:
        status = ("awaiting_approval" if any(item.status == "awaiting_approval" for item in results)
                  else "failed" if any(item.status == "failed" for item in results) else "completed")
        preset_id = str(preset.get("id", ""))
        payload = {"run_id": run_id, "preset": preset_id, "label": preset.get("label", preset_id),
                   "status": status, "started_at": started,
                   "finished_at": datetime.now().astimezone().isoformat(),
                   "context": self._safe_context(merged),
                   "results": [asdict(item) for item in results]}
        self._persist_run(payload, started)
        get_event_bus().publish(Event(f"workflow.{status}", "workflow_runtime", data={
            "run_id": run_id, "preset": preset_id, "status": status,
        }))
        return payload

    def _persist_run(self, payload: dict[str, Any], started: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("""INSERT INTO workflow_runs VALUES(?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,
                payload=excluded.payload,finished_at=excluded.finished_at""",
                (payload["run_id"], payload["preset"], payload["status"],
                 json.dumps(payload, ensure_ascii=False, default=str), started,
                 payload["finished_at"]))

    @classmethod
    def _safe_context(cls, value: Any, key: str = "") -> Any:
        if any(secret in key.casefold() for secret in ("secret", "token", "password", "api_key")):
            return "[REDACTED]"
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(name): cls._safe_context(item, str(name)) for name, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._safe_context(item, key) for item in value]
        return str(value)

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT payload FROM workflow_runs ORDER BY started_at DESC LIMIT ?",
                                (max(1, min(int(limit), 100)),)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _execute_step(self, step: dict[str, Any], context: dict[str, Any]) -> WorkflowStepResult:
        started = time.perf_counter()
        action, output = step["action"], None
        try:
            if action == "morning_brief":
                if self.brief_service is None:
                    raise RuntimeError("MorningBriefService가 연결되지 않았습니다.")
                output = self.brief_service.build(
                    session_id=str(context.get("session_id", "")),
                    workspace_path=str(context.get("workspace_path", "")),
                    location=str(context.get("location", "")),
                )
            elif action == "diagnostics":
                if self.diagnostics is None:
                    raise RuntimeError("진단 런타임이 연결되지 않았습니다.")
                output = self.diagnostics.run(scope=step.get("scope", "core"), live=False)
            elif action == "tool":
                if self.tool_executor is None:
                    raise RuntimeError("도구 실행기가 연결되지 않았습니다.")
                tool_input = self._resolve(step.get("input", {}), context)
                result = self.tool_executor.execute_tool(str(step["tool"]), tool_input)
                if not getattr(result, "succeeded", False):
                    raise RuntimeError(str(getattr(result, "error", result)))
                output = {"raw_output": result.raw_output,
                          "artifacts": [asdict(item) for item in result.artifacts],
                          "evidence": [asdict(item) for item in result.evidence]}
            elif action == "memory_maintenance":
                if self.memory_maintenance is None:
                    raise RuntimeError("기억 통합 콜백이 연결되지 않았습니다.")
                output = self.memory_maintenance()
            elif action == "workspace_summary":
                path = Path(str(context.get("workspace_path", "")))
                if not path.is_dir():
                    raise RuntimeError("선택된 Workspace가 없습니다.")
                output = {"path": str(path), "files": sum(1 for item in path.rglob("*") if item.is_file())}
            elif action == "plugin_group_check":
                registry = getattr(self.brief_service, "plugin_registry", None)
                if registry is None:
                    raise RuntimeError("PluginRegistry가 연결되지 않았습니다.")
                prefixes = tuple(step.get("prefixes", ()))
                output = [asdict(item) for item in registry.get_plugin_statuses()
                          if not prefixes or item.name.startswith(prefixes)]
            else:
                raise ValueError(f"지원하지 않는 워크플로 action입니다: {action}")
            return WorkflowStepResult(step["id"], action, "completed", output=output,
                                      evidence=[{"kind": "runtime_result", "source": action}],
                                      duration_ms=round((time.perf_counter() - started) * 1000, 2))
        except Exception as exc:
            return WorkflowStepResult(step["id"], action, "failed", error=f"{type(exc).__name__}: {exc}",
                                      duration_ms=round((time.perf_counter() - started) * 1000, 2))

    @staticmethod
    def _resolve(value: Any, context: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: WorkflowRuntime._resolve(item, context) for key, item in value.items()}
        if isinstance(value, list):
            return [WorkflowRuntime._resolve(item, context) for item in value]
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            return context.get(value[2:-1], "")
        return value

    @staticmethod
    def _condition(condition: str, context: dict[str, Any], results: list[WorkflowStepResult]) -> bool:
        if condition == "workspace_selected":
            return bool(context.get("workspace_path"))
        if condition == "previous_success":
            return not results or results[-1].status == "completed"
        return False
