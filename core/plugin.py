"""Jarvis Plugin SDK and the single validated tool runtime boundary."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import platform
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from core.runtime.event_bus import Event, get_event_bus
from core.tool_result import ToolRunResult

PluginToolOutput = Union[str, Dict[str, Any], List[Any], ToolRunResult]


@dataclass
class ToolSchema:
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    required_permissions: List[str] = field(default_factory=list)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    side_effect: str = "auto"
    verification_required: bool = True
    execution_mode: str = "sync"
    timeout_seconds: float = 120.0
    max_retries: int = 0
    cancellable: bool = False

    def __post_init__(self):
        actions = set(self.name.casefold().split("_"))
        if self.side_effect == "auto":
            if actions & {"get", "read", "list", "find", "search", "status", "diff", "log"}:
                self.side_effect = "read"
            elif "mail_send" in self.required_permissions or actions & {"send", "push", "publish"}:
                self.side_effect = "external_send"
            elif actions & {"create", "write", "update", "delete", "remove", "add", "commit", "set"}:
                self.side_effect = "change"
            elif any(p in {"filesystem_write", "git_commit", "git_push"} for p in self.required_permissions):
                self.side_effect = "change"
            elif "windows_api" in self.required_permissions or actions & {"launch", "close", "focus", "execute", "run", "speak", "repeat"}:
                self.side_effect = "execute"
            else:
                self.side_effect = "read"
        if not self.output_schema:
            self.output_schema = {
                "type": "object",
                "required": ["status", "raw_output", "evidence", "artifacts"],
                "properties": {
                    "status": {"type": "string"},
                    "raw_output": {"type": "string"},
                    "evidence": {"type": "array"},
                    "artifacts": {"type": "array"},
                },
            }


@dataclass(frozen=True)
class CapabilityContract:
    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    side_effect: str
    required_permissions: List[str]
    verification_required: bool
    execution_mode: str
    timeout_seconds: float
    max_retries: int
    cancellable: bool


@dataclass
class SlotSchema:
    name: str
    description: str
    question: str
    required: bool = True
    role: str = "parameter"


@dataclass
class IntentSchema:
    name: str
    description: str
    tool_name: str
    utterance_hints: List[str]
    slots: List[SlotSchema]
    execution_hints: List[str] = field(default_factory=lambda: ["생성", "만들", "작성", "저장", "실행"])
    follow_up_hints: List[str] = field(default_factory=list)
    capability_response: str = ""
    utterance_patterns: List[str] = field(default_factory=list)
    domain: str = ""
    action: str = ""
    request_type: str = "auto"
    target_slot: str = ""
    constraint_slots: List[str] = field(default_factory=list)
    reference_slots: List[str] = field(default_factory=list)
    freshness: str = "static"
    requires_sources: bool = False

    def __post_init__(self):
        parts = self.name.split(".", 1)
        self.domain = self.domain or parts[0]
        self.action = self.action or (parts[1] if len(parts) > 1 else self.name)
        if self.request_type == "auto":
            actions = set(self.action.casefold().split("_"))
            if actions & {"get", "read", "list", "find", "search", "status", "current", "time", "date", "weather"}:
                self.request_type = "query"
            elif actions & {"send", "push", "publish"}:
                self.request_type = "external_send"
            elif actions & {"create", "write", "update", "delete", "remove", "add", "commit", "set"}:
                self.request_type = "change"
            else:
                self.request_type = "execute"
        if not self.target_slot:
            self.target_slot = next((slot.name for slot in self.slots if slot.role == "target"), "")
            if not self.target_slot:
                self.target_slot = next((slot.name for slot in self.slots if slot.name in {"target", "path", "filename", "query", "name", "location"}), "")


class PluginContractError(RuntimeError):
    """A plugin contract is unsafe or internally inconsistent."""


class ToolCancelledError(RuntimeError):
    pass


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ToolCancelledError("도구 실행이 취소되었습니다.")


@dataclass(frozen=True)
class PluginStatus:
    name: str
    version: str
    installed: bool
    connected: bool
    authenticated: bool
    verified: bool
    enabled: bool
    diagnostics: List[str] = field(default_factory=list)


class BasePlugin(ABC):
    def __init__(self):
        self.name = ""
        self.description = ""
        self.version = "1.0.0"
        self.author = ""
        self.enabled = True
        self.dependencies: List[str] = []
        self.supported_os: List[str] = []
        self.auth_required = False
        self.auth_type = "none"
        self.auto_discover = True
        self.registry: Optional["PluginRegistry"] = None

    @abstractmethod
    def get_tools(self) -> List[ToolSchema]: ...

    @abstractmethod
    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> PluginToolOutput: ...

    def on_load(self): pass
    def on_unload(self): pass
    def on_event(self, event: Event): pass
    def get_intents(self) -> List[IntentSchema]: return []
    def extract_slots(self, intent_name: str, text: str, current_slots: Dict[str, Any]) -> Dict[str, Any]: return dict(current_slots)
    def present_result(self, tool_name: str, result: PluginToolOutput) -> str: return str(result)
    def is_connected(self) -> bool: return True
    def is_authenticated(self) -> bool: return not self.auth_required

    def diagnose(self) -> List[str]:
        issues = []
        if self.supported_os and platform.system().casefold() not in {item.casefold() for item in self.supported_os}:
            issues.append(f"지원하지 않는 OS: {platform.system()}")
        for dependency in self.dependencies:
            try:
                importlib.import_module(dependency)
            except ImportError:
                issues.append(f"의존성 누락: {dependency}")
        if not self.is_connected(): issues.append("연결되지 않음")
        if self.auth_required and not self.is_authenticated(): issues.append(f"인증 필요: {self.auth_type}")
        return issues


class PluginRegistry:
    def __init__(self):
        self.plugins: Dict[str, BasePlugin] = {}
        self._tools: Dict[str, tuple[BasePlugin, ToolSchema]] = {}
        self._intents: Dict[str, tuple[BasePlugin, IntentSchema]] = {}
        self._event_bus = get_event_bus()
        self._loaded_directories: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="jarvis-tool")
        self._cancellations: Dict[str, CancellationToken] = {}

    def register_plugin(self, plugin: BasePlugin):
        if not plugin.name:
            raise PluginContractError("Plugin 이름은 비어 있을 수 없습니다.")
        if plugin.name in self.plugins:
            raise PluginContractError(f"중복 Plugin 이름: {plugin.name}")
        tools, intents = list(plugin.get_tools()), list(plugin.get_intents())
        names, intent_names = [x.name for x in tools], [x.name for x in intents]
        collisions = {x for x in names if names.count(x) > 1} | (set(names) & set(self._tools))
        intent_collisions = {x for x in intent_names if intent_names.count(x) > 1} | (set(intent_names) & set(self._intents))
        if collisions: raise PluginContractError(f"중복 Tool 이름: {', '.join(sorted(collisions))}")
        if intent_collisions: raise PluginContractError(f"중복 Intent 이름: {', '.join(sorted(intent_collisions))}")
        for tool in tools: self._validate_schema_definition(tool)
        plugin.registry = self
        plugin.on_load()
        self.plugins[plugin.name] = plugin
        self._tools.update({tool.name: (plugin, tool) for tool in tools})
        self._intents.update({intent.name: (plugin, intent) for intent in intents})
        self._event_bus.subscribe("*", plugin.on_event)
        print(f"[Plugin] Registered: {plugin.name} v{plugin.version}")

    def unregister_plugin(self, plugin_name: str):
        plugin = self.plugins.get(plugin_name)
        if plugin is None: return
        plugin.on_unload()
        plugin.registry = None
        self._event_bus.unsubscribe("*", plugin.on_event)
        del self.plugins[plugin_name]
        self._tools = {k: v for k, v in self._tools.items() if v[0] is not plugin}
        self._intents = {k: v for k, v in self._intents.items() if v[0] is not plugin}

    def get_plugin(self, plugin_name: str) -> Optional[BasePlugin]: return self.plugins.get(plugin_name)
    def get_all_tools(self) -> List[ToolSchema]: return [t for p, t in self._tools.values() if p.enabled]
    def get_all_intents(self) -> List[tuple[BasePlugin, IntentSchema]]: return [(p, i) for p, i in self._intents.values() if p.enabled]

    def get_capability(self, tool_name: str) -> Optional[CapabilityContract]:
        entry = self._tools.get(tool_name)
        if not entry or not entry[0].enabled: return None
        tool = entry[1]
        return CapabilityContract(tool.name, tool.description, tool.input_schema, tool.output_schema,
                                  tool.side_effect, list(tool.required_permissions), tool.verification_required,
                                  tool.execution_mode, tool.timeout_seconds, tool.max_retries, tool.cancellable)

    def get_capabilities(self) -> List[CapabilityContract]:
        return [contract for name in self._tools if (contract := self.get_capability(name))]

    @staticmethod
    def _validate_schema_definition(tool: ToolSchema) -> None:
        if not tool.name: raise PluginContractError("Tool 이름은 비어 있을 수 없습니다.")
        if tool.execution_mode not in {"sync", "async"}: raise PluginContractError(f"{tool.name}: 잘못된 execution_mode")
        if tool.timeout_seconds <= 0 or tool.max_retries < 0: raise PluginContractError(f"{tool.name}: timeout/retry 정책이 잘못되었습니다.")
        try:
            Draft202012Validator.check_schema(tool.input_schema)
            Draft202012Validator.check_schema(tool.output_schema)
        except SchemaError as exc:
            raise PluginContractError(f"{tool.name}: 잘못된 JSON Schema: {exc.message}") from exc

    @staticmethod
    def _validate_instance(value: Any, schema: Dict[str, Any], label: str) -> List[str]:
        return [f"{label} JSON Schema 오류: {error.message}" for error in Draft202012Validator(schema).iter_errors(value)]

    def validate_tool_call(self, tool_name: str, tool_input: Dict[str, Any], request_type: str = "") -> List[str]:
        contract = self.get_capability(tool_name)
        if contract is None: return [f"등록되지 않은 도구: {tool_name}"]
        if not isinstance(tool_input, dict): return ["도구 입력은 객체여야 합니다."]
        errors = []
        expected = {"query": "read", "execute": "execute", "change": "change", "external_send": "external_send"}.get(request_type, request_type)
        if request_type and expected != contract.side_effect:
            errors.append(f"요청 종류({request_type})와 도구 부작용({contract.side_effect})이 다릅니다.")
        missing = [name for name in contract.input_schema.get("required", [])
                   if tool_input.get(name) in (None, "")]
        errors.extend(f"필수 입력이 없습니다: {name}" for name in missing)
        schema_errors = self._validate_instance(tool_input, contract.input_schema, "입력")
        errors.extend(error for error in schema_errors if "is a required property" not in error)
        return errors

    def validate_contracts(self) -> Dict[str, List[str]]:
        issues: Dict[str, List[str]] = {}
        valid_effects = {"read", "execute", "change", "external_send"}
        for tool in self.get_all_tools():
            current = []
            if tool.side_effect not in valid_effects: current.append(f"알 수 없는 side_effect: {tool.side_effect}")
            if current: issues[tool.name] = current
        for _plugin, intent in self.get_all_intents():
            current = []
            contract = self.get_capability(intent.tool_name)
            if contract is None: current.append(f"등록되지 않은 Tool 참조: {intent.tool_name}")
            if intent.request_type not in {"query", "execute", "change", "external_send"}: current.append(f"알 수 없는 request_type: {intent.request_type}")
            if intent.freshness not in {"static", "session", "live"}: current.append(f"알 수 없는 freshness: {intent.freshness}")
            if intent.freshness == "live" and not intent.requires_sources: current.append("live Intent는 requires_sources=True여야 합니다.")
            expected = {"query": "read", "execute": "execute", "change": "change", "external_send": "external_send"}.get(intent.request_type)
            if contract and expected != contract.side_effect: current.append(f"request_type({intent.request_type})과 side_effect({contract.side_effect}) 불일치")
            if current: issues[f"intent:{intent.name}"] = current
        return issues

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> PluginToolOutput:
        entry = self._tools.get(tool_name)
        if entry is None or not entry[0].enabled: return ToolRunResult.failed(tool_name=tool_name, error=f"등록되지 않은 도구: {tool_name}")
        plugin, tool = entry
        errors = self.validate_tool_call(tool_name, tool_input)
        if errors: return ToolRunResult.failed(tool_name=tool_name, error="; ".join(errors))
        token, started = CancellationToken(), time.perf_counter()
        self._cancellations[tool_name] = token
        try:
            for attempt in range(tool.max_retries + 1):
                if attempt:
                    from core.productization import METRICS
                    METRICS.increment(f"plugin.{tool_name}.retry")
                token.raise_if_cancelled()
                future = self._executor.submit(self._invoke, plugin, tool, tool_input, token)
                try:
                    deadline = time.monotonic() + tool.timeout_seconds
                    while True:
                        token.raise_if_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise FutureTimeoutError()
                        try:
                            result = future.result(timeout=min(0.05, remaining))
                            break
                        except FutureTimeoutError:
                            if future.done():
                                raise
                            continue
                    output = result.to_dict() if isinstance(result, ToolRunResult) else result
                    if (tool.output_schema.get("required") == ["status", "raw_output", "evidence", "artifacts"]
                            and not isinstance(result, ToolRunResult)):
                        output = {
                            "status": "unverified", "raw_output": str(result),
                            "evidence": [], "artifacts": [],
                        }
                    output_errors = self._validate_instance(output, tool.output_schema, "출력")
                    if output_errors: return ToolRunResult.failed(tool_name=tool_name, error="; ".join(output_errors), duration_ms=(time.perf_counter()-started)*1000)
                    return result
                except FutureTimeoutError:
                    future.cancel()
                    if attempt >= tool.max_retries: return ToolRunResult.failed(tool_name=tool_name, error=f"도구 실행 시간 초과: {tool.timeout_seconds:g}초", duration_ms=(time.perf_counter()-started)*1000)
                except ToolCancelledError as exc:
                    return ToolRunResult.failed(tool_name=tool_name, error=str(exc), duration_ms=(time.perf_counter()-started)*1000)
                except Exception as exc:
                    if attempt >= tool.max_retries: return ToolRunResult.failed(tool_name=tool_name, error=f"Plugin 실행 오류: {exc}", duration_ms=(time.perf_counter()-started)*1000)
        finally:
            self._cancellations.pop(tool_name, None)

    @staticmethod
    def _invoke(plugin: BasePlugin, tool: ToolSchema, tool_input: Dict[str, Any], token: CancellationToken):
        token.raise_if_cancelled()
        result = plugin.execute_tool(tool.name, dict(tool_input))
        result = asyncio.run(result) if inspect.isawaitable(result) else result
        token.raise_if_cancelled()
        return result

    def cancel_tool(self, tool_name: str) -> bool:
        token = self._cancellations.get(tool_name)
        if token is None: return False
        token.cancel()
        return True

    def get_plugin_statuses(self) -> List[PluginStatus]:
        contract_issues = self.validate_contracts()
        statuses = []
        for plugin in self.plugins.values():
            diagnostics = plugin.diagnose()
            for tool in plugin.get_tools(): diagnostics.extend(contract_issues.get(tool.name, []))
            statuses.append(PluginStatus(plugin.name, plugin.version, True, plugin.is_connected(),
                                         plugin.is_authenticated(), not diagnostics, plugin.enabled, diagnostics))
        return statuses

    def present_result(self, tool_name: str, result: PluginToolOutput) -> str:
        entry = self._tools.get(tool_name)
        presentation_value = result.raw_output if isinstance(result, ToolRunResult) else result
        return (entry[0].present_result(tool_name, presentation_value)
                if entry and entry[0].enabled else str(presentation_value))

    def load_plugins_from_directory(self, directory: Optional[str] = None):
        path = Path(directory).resolve() if directory else Path(__file__).resolve().parent.parent / "plugins"
        if not path.exists() or str(path) in self._loaded_directories: return
        for item in path.iterdir():
            if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
                try:
                    module = importlib.import_module(f"plugins.{item.stem}")
                    for _name, obj in inspect.getmembers(module):
                        if inspect.isclass(obj) and issubclass(obj, BasePlugin) and obj is not BasePlugin:
                            instance = obj()
                            if getattr(instance, "auto_discover", True): self.register_plugin(instance)
                except Exception as exc:
                    raise PluginContractError(f"Plugin 로드 실패 ({item.name}): {exc}") from exc
        self._loaded_directories.add(str(path))


_plugin_registry: Optional[PluginRegistry] = None


def get_plugin_registry() -> PluginRegistry:
    global _plugin_registry
    if _plugin_registry is None: _plugin_registry = PluginRegistry()
    return _plugin_registry
