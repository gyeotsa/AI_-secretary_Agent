"""Live runtime snapshot used by the integrated Command Center UI."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import json
import threading
import time
import urllib.request

from config import Config
from core.gpu_scheduler import get_gpu_resource_queue
from core.model_registry import get_model_registry
from core.productization import METRICS
from core.quality_metrics import AcceptanceScenarioEvaluator, get_quality_metric_store
from core.runtime.action_journal import get_action_journal
from core.runtime.event_bus import Event, get_event_bus


def _value(item: Any) -> Any:
    if is_dataclass(item):
        return asdict(item)
    if isinstance(item, dict):
        return {key: _value(value) for key, value in item.items()}
    if isinstance(item, (list, tuple)):
        return [_value(value) for value in item]
    return item


class CommandCenterRuntime:
    """Aggregates truth from existing runtimes; it owns no duplicate task state."""

    def __init__(self, *, supervisor=None, specialist_team=None, permission_manager=None,
                 plugin_registry=None, automation_engine=None, observer=None,
                 workspace_manager=None, workflow_runtime=None, diagnostics=None,
                 dialogue_state=None, session_provider=None):
        self.supervisor = supervisor
        self.specialist_team = specialist_team
        self.permission_manager = permission_manager
        self.plugin_registry = plugin_registry
        self.automation_engine = automation_engine
        self.observer = observer
        self.workspace_manager = workspace_manager
        self.workflow_runtime = workflow_runtime
        self.diagnostics = diagnostics
        self.dialogue_state = dialogue_state
        self.session_provider = session_provider or (lambda: "")
        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._event_lock = threading.RLock()
        self._ollama_cache: tuple[float, dict[str, dict[str, Any]], str] = (0.0, {}, "not_checked")
        self._ollama_lock = threading.RLock()
        get_event_bus().subscribe("*", self._on_event)

    def _on_event(self, event: Event) -> None:
        with self._event_lock:
            self._events.appendleft({
                "type": event.type, "source": event.source,
                "timestamp": event.timestamp.isoformat(), "data": _value(event.data),
            })

    def snapshot(self) -> dict[str, Any]:
        contracts = self.supervisor.snapshot(50) if self.supervisor else []
        teams = self.specialist_team.snapshot(20) if self.specialist_team else []
        actions = []
        try:
            actions = [_value(item) for item in get_action_journal().get_recent(30)]
        except Exception as exc:
            actions = [{"success": False, "error": f"ActionJournal: {exc}"}]
        permissions = []
        if self.permission_manager:
            permissions = [{
                "id": item.id, "name": item.name, "level": item.level.value,
                "decision": item.decision.value,
            } for item in self.permission_manager.get_all_permissions()]
        plugins = []
        if self.plugin_registry:
            plugins = [_value(item) for item in self.plugin_registry.get_plugin_statuses()]
        automation = self._automation_snapshot()
        scheduler = automation["scheduler"]
        observer = self._observer_snapshot()
        workspace = _value(self.workspace_manager.get_info()) if self.workspace_manager else {}
        workflow_runs = self.workflow_runtime.recent_runs(10) if self.workflow_runtime else []
        diagnostics = self.diagnostics.last_report() if self.diagnostics else {
            "summary": {"status": "not_run"}, "probes": [],
        }
        pending_tasks = self._dialogue_tasks()
        gpu = get_gpu_resource_queue().snapshot()
        models = self._model_snapshot()
        quality = get_quality_metric_store().snapshot()
        acceptance = AcceptanceScenarioEvaluator(get_quality_metric_store()).evaluate()
        with self._event_lock:
            events = list(self._events)
        return {
            "updated_at": datetime.now().astimezone().isoformat(),
            "system": self._system_snapshot(), "workspace": workspace,
            "contracts": contracts, "dialogue_tasks": pending_tasks, "teams": teams,
            "plan_steps": self._plan_steps(pending_tasks),
            "gpu": gpu, "models": models, "actions": actions, "permissions": permissions,
            "plugins": plugins, "scheduler": scheduler, "observer": observer,
            "automation": automation,
            "workflow_runs": workflow_runs, "diagnostics": diagnostics,
            "quality": quality, "acceptance": acceptance, "events": events,
            "artifacts": self._artifacts(contracts),
        }

    def _dialogue_tasks(self) -> list[dict[str, Any]]:
        session_id = str(self.session_provider() or "")
        if not self.dialogue_state or not session_id:
            return []
        try:
            return [_value(item) for item in self.dialogue_state.list_tasks(session_id, include_finished=False)]
        except Exception:
            return []

    def _observer_snapshot(self) -> dict[str, Any]:
        if self.observer is None:
            return {"status": "disconnected"}
        return {
            "status": "running" if bool(getattr(self.observer, "_running", False)) else "stopped",
            "watched_paths": sorted(str(item) for item in getattr(self.observer, "watched_paths", ())
                                      or getattr(self.observer, "_watched_paths", ())),
            "recent_events": [_value(item) for item in self.observer.get_event_history(20)]
            if hasattr(self.observer, "get_event_history") else [],
        }

    def _automation_snapshot(self) -> dict[str, Any]:
        if self.automation_engine is None:
            return {"scheduler": {"status": "disconnected"}, "jobs": [],
                    "workflow_runs": [], "gpu_requests": {}}
        try:
            scheduler = self.automation_engine.runtime_diagnostics()
        except Exception as exc:
            scheduler = {"status": "error", "error": str(exc)}
        try:
            jobs = self.automation_engine.get_job_records()
            jobs = [{key: item.get(key) for key in (
                "id", "description", "schedule_type", "schedule_value",
                "action_type", "enabled", "last_run", "created_at",
            )} for item in jobs]
        except Exception as exc:
            jobs = [{"description": f"작업 조회 실패: {exc}", "enabled": False}]
        return {
            "scheduler": scheduler,
            "jobs": jobs,
            "workflow_runs": self.workflow_runtime.recent_runs(20) if self.workflow_runtime else [],
            "gpu_requests": get_gpu_resource_queue().snapshot(),
        }

    @staticmethod
    def _plan_steps(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in tasks:
            plan = task.get("plan") or []
            if isinstance(plan, dict):
                plan = plan.get("steps", [])
            for index, step in enumerate(plan if isinstance(plan, list) else []):
                if not isinstance(step, dict):
                    continue
                rows.append({
                    "task_id": task.get("task_id") or task.get("id"),
                    "step_id": step.get("id") or index,
                    "description": step.get("description", ""),
                    "status": step.get("status", "pending"),
                    "dependencies": step.get("dependencies") or step.get("depends_on") or [],
                    "tool": step.get("tool_name") or step.get("tool") or "",
                    "attempts": step.get("attempts", 0),
                })
        return rows

    def _model_snapshot(self) -> list[dict[str, Any]]:
        loaded, error = self._ollama_loaded_models()
        values = []
        for profile in get_model_registry().profiles().values():
            item = _value(profile)
            name = str(item.get("model", ""))
            live = loaded.get(name) or loaded.get(name.split(":", 1)[0])
            item.update({
                "loaded": bool(live),
                "loaded_vram_mb": round(float((live or {}).get("size_vram", 0)) / 1024 ** 2),
                "expires_at": (live or {}).get("expires_at", ""),
                "runtime_status": "loaded" if live else ("unavailable" if error else "configured"),
                "runtime_error": error,
            })
            values.append(item)
        return values

    def _ollama_loaded_models(self) -> tuple[dict[str, dict[str, Any]], str]:
        now = time.monotonic()
        with self._ollama_lock:
            checked, cached, error = self._ollama_cache
            if now - checked < 5.0:
                return cached, "" if error == "ok" else error
        models: dict[str, dict[str, Any]] = {}
        status = "ok"
        try:
            url = Config.OLLAMA_BASE_URL.rstrip("/") + "/api/ps"
            with urllib.request.urlopen(url, timeout=0.7) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for item in payload.get("models", []):
                name = str(item.get("name") or item.get("model") or "")
                if name:
                    models[name] = item
                    models.setdefault(name.split(":", 1)[0], item)
        except Exception as exc:
            status = f"{type(exc).__name__}: {exc}"
        with self._ollama_lock:
            self._ollama_cache = (now, models, status)
        return models, "" if status == "ok" else status

    @staticmethod
    def _system_snapshot() -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            import psutil
            vm = psutil.virtual_memory()
            result.update(ram_percent=vm.percent, ram_used_mb=round(vm.used / 1024 ** 2),
                          ram_total_mb=round(vm.total / 1024 ** 2))
        except Exception:
            result["ram_percent"] = None
        try:
            import torch
            if torch.cuda.is_available():
                free, total = torch.cuda.mem_get_info()
                result.update(gpu=torch.cuda.get_device_name(0), vram_used_mb=round((total - free) / 1024 ** 2),
                              vram_total_mb=round(total / 1024 ** 2))
        except Exception:
            pass
        result["runtime_metrics"] = METRICS.snapshot()
        return result

    @staticmethod
    def _artifacts(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        values = []
        for contract in contracts:
            for item in contract.get("artifacts", []):
                values.append({"contract_id": contract.get("contract_id"), **item})
        return values[:50]
