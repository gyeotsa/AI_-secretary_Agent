"""Jarvis Plugin SDK and the single validated tool runtime boundary."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import inspect
import json
import math
import platform
import threading
import time
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from core.runtime.event_bus import Event, get_event_bus
from core.tool_result import Evidence, ToolRunResult

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
    idempotency: str = "auto"
    execution_isolation: str = "thread"

    def __post_init__(self):
        actions = set(self.name.casefold().split("_"))
        if self.side_effect == "auto":
            if actions & {"get", "read", "list", "find", "search", "status", "diff", "log"}:
                self.side_effect = "read"
            elif "mail_send" in self.required_permissions or actions & {
                "send", "push", "publish", "pay", "payment", "purchase",
                "checkout", "charge", "transfer", "buy", "sell", "order",
                "refund",
            }:
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
        if self.idempotency == "auto":
            # Read operations are intrinsically safe to repeat.  Mutating and
            # externally visible operations need a caller-provided key so the
            # registry can collapse duplicate submissions without executing
            # the plugin twice.
            self.idempotency = "intrinsic" if self.side_effect == "read" else "registry"


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
    idempotency: str = "none"
    automatic_retry_allowed: bool = False
    execution_isolation: str = "thread"


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
    negation_is_constraint: bool = False

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
        self._publication_lock = threading.RLock()

    def cancel(self) -> None:
        # A cancellation request must never block on native/filesystem work
        # that already entered publication. Such an in-flight effect remains
        # uncertain; cancellation is not a rollback or a guaranteed undo.
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ToolCancelledError("도구 실행이 취소되었습니다.")

    @contextmanager
    def publication_guard(self):
        """Check cancellation before entering a short publication phase.

        Preparation/copying and native rendering must occur outside the guard.
        It is not a rollback; a publish already started before cancellation
        may complete. Check cancellation before each atomic write as well.
        """
        with self._publication_lock:
            self.raise_if_cancelled()
            yield


@dataclass(frozen=True)
class ToolExecutionContext:
    """Immutable identity and cooperative-cancellation context for one run."""

    execution_id: str
    tool_name: str
    idempotency_key: str
    attempt: int
    started_at: float
    cancellation_token: CancellationToken = field(compare=False, repr=False)
    staging_directory: str = ""
    deadline: float = 0.0

    @property
    def cancelled(self) -> bool:
        return self.cancellation_token.cancelled

    def raise_if_cancelled(self) -> None:
        self.cancellation_token.raise_if_cancelled()
        if self.deadline and time.monotonic() >= self.deadline:
            raise TimeoutError("도구 실행 제한시간을 초과했습니다.")

    @contextmanager
    def publication_guard(self):
        with self.cancellation_token.publication_guard():
            self.raise_if_cancelled()
            yield


@dataclass
class _ExecutionState:
    execution_id: str
    tool_name: str
    token: CancellationToken
    started_at: float
    future: Any = None
    worker: Any = None


@dataclass
class _DeduplicationRecord:
    execution_id: str
    tool_name: str
    idempotency_key: str = ""
    request_fingerprint: str = ""
    execution_ids: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)
    completed_at: float = 0.0
    result: Optional[PluginToolOutput] = None
    done: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class PluginStatus:
    name: str
    version: str
    installed: Optional[bool]
    connected: Optional[bool]
    authenticated: Optional[bool]
    verified: Optional[bool]
    enabled: bool
    diagnostics: List[str] = field(default_factory=list)
    registered: bool = True
    installation_state: str = "unchecked"
    connection_state: str = "not_applicable"
    authentication_state: str = "not_applicable"
    contract_state: str = "unchecked"
    verification_state: str = "unchecked"
    runtime_state: str = "unchecked"
    runtime_summary: str = "실제 도구 실행 이력이 없습니다."
    runtime_evidence: List[str] = field(default_factory=list)
    last_tool: str = ""
    last_execution_at: Optional[float] = None


@dataclass(frozen=True)
class PluginStateProbe:
    """One independently evidenced plugin status axis.

    ``confirmed`` and ``failed`` are the only conclusive states.  A missing
    probe is represented as ``not_applicable``; configured-but-not-tested
    integrations use ``unchecked``.  This prevents an importable package from
    being presented as a working network/account integration.
    """

    state: str
    summary: str = ""
    evidence: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.state not in {
            "confirmed", "failed", "unchecked", "not_applicable", "partial",
        }:
            raise ValueError(f"알 수 없는 Plugin 상태: {self.state}")


def _state_as_optional_bool(state: str) -> Optional[bool]:
    if state == "confirmed":
        return True
    if state == "failed":
        return False
    return None


class BasePlugin(ABC):
    # Opt in only for no-argument, importable factories whose isolated tools
    # do not depend on parent-process objects, UI sessions or mutable state.
    supports_process_isolation = False

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
    def is_connected(self) -> Optional[bool]:
        """Legacy connection probe.

        ``None`` means that this plugin has no connection probe.  Subclasses
        must return ``True`` only after checking a real local/remote endpoint,
        not merely because a Python dependency imported successfully.
        """
        return None

    def is_authenticated(self) -> Optional[bool]:
        """Legacy authentication probe; ``None`` means not yet verified."""
        return None

    def probe_connection(self) -> PluginStateProbe:
        if type(self).is_connected is BasePlugin.is_connected:
            return PluginStateProbe("not_applicable", "연결 상태를 요구하지 않는 로컬 Plugin입니다.")
        try:
            value = self.is_connected()
        except Exception as exc:
            return PluginStateProbe(
                "failed", f"연결 상태 확인 실패: {type(exc).__name__}: {exc}",
            )
        if value is True:
            return PluginStateProbe("confirmed", "Plugin이 실제 연결 상태를 확인했습니다.")
        if value is False:
            return PluginStateProbe("failed", "Plugin 연결 확인에 실패했습니다.")
        return PluginStateProbe("unchecked", "연결 설정은 있으나 실제 연결은 아직 확인하지 않았습니다.")

    def probe_authentication(self) -> PluginStateProbe:
        if not self.auth_required:
            return PluginStateProbe("not_applicable", "인증이 필요하지 않습니다.")
        if type(self).is_authenticated is BasePlugin.is_authenticated:
            return PluginStateProbe(
                "unchecked", f"{self.auth_type} 인증을 확인하는 probe가 구현되지 않았습니다.",
            )
        try:
            value = self.is_authenticated()
        except Exception as exc:
            return PluginStateProbe(
                "failed", f"인증 상태 확인 실패: {type(exc).__name__}: {exc}",
            )
        if value is True:
            return PluginStateProbe("confirmed", f"{self.auth_type} 인증을 실제 확인했습니다.")
        if value is False:
            return PluginStateProbe("failed", f"{self.auth_type} 인증이 없거나 유효하지 않습니다.")
        return PluginStateProbe(
            "unchecked", f"{self.auth_type} 설정은 있으나 실제 인증은 아직 확인하지 않았습니다.",
        )

    def get_execution_context(self) -> Optional[ToolExecutionContext]:
        """Return this worker's context without changing legacy tool inputs.

        Plugins that support cooperative cancellation can call
        ``context.raise_if_cancelled()`` during long-running work.  The
        context is thread-local, so concurrent runs of the same tool never
        share cancellation state.
        """
        return self.registry.current_execution_context() if self.registry else None

    def prepare_isolated_input(
        self, tool_name: str, tool_input: Dict[str, Any], staging_directory: str,
    ) -> Dict[str, Any]:
        """Parent-side preflight; redirect outputs into the private workspace.

        Hooks must be bounded local validation/publication, never a native
        call or recursive tool execution. The child receives only JSON data.
        """
        return dict(tool_input)

    def finalize_isolated_result(
        self, tool_name: str, original_input: Dict[str, Any],
        isolated_input: Dict[str, Any], result: PluginToolOutput,
    ) -> PluginToolOutput:
        """Publish staged output only after timely, valid worker completion."""
        return result

    def diagnose(self) -> List[str]:
        issues = []
        if self.supported_os and platform.system().casefold() not in {item.casefold() for item in self.supported_os}:
            issues.append(f"지원하지 않는 OS: {platform.system()}")
        for dependency in self.dependencies:
            try:
                importlib.import_module(dependency)
            except ImportError:
                issues.append(f"의존성 누락: {dependency}")
            except OSError as exc:
                # Native packages (torch/OpenCV/COM wrappers, etc.) can import
                # the Python module but fail while loading a DLL.  Treat this
                # as an isolated plugin installation failure, never a process
                # startup crash.
                issues.append(f"의존성 DLL 로드 실패: {dependency} ({exc})")
        return issues


class PluginRegistry:
    _NON_RETRYABLE_EFFECTS = frozenset({"execute", "change", "external_send"})
    _IRREVERSIBLE_ACTIONS = frozenset({
        "send", "push", "publish", "delete", "remove", "commit", "write",
        "create", "update", "set", "launch", "close", "execute", "run",
        "pay", "payment", "purchase", "checkout", "charge", "transfer",
        "buy", "sell", "order", "refund",
    })
    _IRREVERSIBLE_PERMISSIONS = frozenset({
        "filesystem_write", "git_commit", "git_push", "mail_send",
        "cloud_account", "windows_api",
    })

    def __init__(self):
        self.plugins: Dict[str, BasePlugin] = {}
        self._tools: Dict[str, tuple[BasePlugin, ToolSchema]] = {}
        self._intents: Dict[str, tuple[BasePlugin, IntentSchema]] = {}
        self._event_bus = get_event_bus()
        self._loaded_directories: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="jarvis-tool")
        self._state_lock = threading.RLock()
        self._execution_local = threading.local()
        self._executions: Dict[str, _ExecutionState] = {}
        self._active_by_tool: Dict[str, set[str]] = {}
        # Kept as a compatibility alias for callers/tests that inspected the
        # old mapping.  It is now keyed by execution_id, never by tool name.
        self._cancellations: Dict[str, CancellationToken] = {}
        self._execution_records: Dict[str, _DeduplicationRecord] = {}
        self._idempotency_records: Dict[tuple[str, str], _DeduplicationRecord] = {}
        self._deduplication_ttl_seconds = 3600.0
        self._deduplication_max_records = 1024
        self._load_failures: Dict[str, Dict[str, Any]] = {}
        self._runtime_by_plugin: Dict[str, Dict[str, Any]] = {}
        self._runtime_by_tool: Dict[str, Dict[str, Any]] = {}
        self._shutting_down = False
        self._process_workspace_root: Optional[str] = None
        self._process_slots = threading.BoundedSemaphore(2)

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
        for tool in tools:
            self._validate_schema_definition(tool)
            if tool.execution_isolation == "process":
                factory = type(plugin)
                if (not factory.supports_process_isolation or factory.__module__ == "__main__"
                        or "<" in factory.__qualname__):
                    raise PluginContractError(f"{tool.name}: 격리 실행은 import 가능한 stateless Plugin만 지원합니다.")
                try:
                    inspect.signature(factory).bind()
                except TypeError as exc:
                    raise PluginContractError(f"{tool.name}: 격리 Plugin factory는 인자 없이 생성 가능해야 합니다.") from exc
        plugin.registry = self
        plugin.on_load()
        self.plugins[plugin.name] = plugin
        self._tools.update({tool.name: (plugin, tool) for tool in tools})
        self._intents.update({intent.name: (plugin, intent) for intent in intents})
        self._event_bus.subscribe("*", plugin.on_event)
        self._runtime_by_plugin.setdefault(plugin.name, {
            "state": "unchecked",
            "summary": "실제 도구 실행 이력이 없습니다.",
            "evidence": [],
            "last_tool": "",
            "last_execution_at": None,
        })
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
        retry_allowed = self._automatic_retry_allowed(tool)
        return CapabilityContract(tool.name, tool.description, tool.input_schema, tool.output_schema,
                                  tool.side_effect, list(tool.required_permissions), tool.verification_required,
                                  tool.execution_mode, tool.timeout_seconds,
                                  tool.max_retries if retry_allowed else 0, tool.cancellable,
                                  tool.idempotency, retry_allowed, tool.execution_isolation)

    def get_capabilities(self) -> List[CapabilityContract]:
        return [contract for name in self._tools if (contract := self.get_capability(name))]

    @staticmethod
    def _validate_schema_definition(tool: ToolSchema) -> None:
        if not tool.name: raise PluginContractError("Tool 이름은 비어 있을 수 없습니다.")
        if tool.execution_mode not in {"sync", "async"}: raise PluginContractError(f"{tool.name}: 잘못된 execution_mode")
        if tool.execution_isolation not in {"thread", "process"}:
            raise PluginContractError(f"{tool.name}: 잘못된 execution_isolation")
        if (isinstance(tool.timeout_seconds, bool) or not isinstance(tool.timeout_seconds, (int, float))
                or not math.isfinite(tool.timeout_seconds) or tool.timeout_seconds <= 0
                or type(tool.max_retries) is not int or tool.max_retries < 0):
            raise PluginContractError(f"{tool.name}: timeout/retry 정책이 잘못되었습니다.")
        if tool.idempotency not in {"intrinsic", "registry", "none"}:
            raise PluginContractError(f"{tool.name}: 잘못된 idempotency 정책")
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

    @classmethod
    def _automatic_retry_allowed(cls, tool: ToolSchema) -> bool:
        """Only demonstrably read-only tools may be retried automatically."""
        actions = set(tool.name.casefold().split("_"))
        permissions = {item.casefold() for item in tool.required_permissions}
        if tool.side_effect in cls._NON_RETRYABLE_EFFECTS:
            return False
        if actions & cls._IRREVERSIBLE_ACTIONS:
            return False
        if permissions & cls._IRREVERSIBLE_PERMISSIONS:
            return False
        return tool.side_effect == "read" and tool.idempotency == "intrinsic"

    @staticmethod
    def _clone_output(value: PluginToolOutput) -> PluginToolOutput:
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    @staticmethod
    def _cancelled_result(
        tool_name: str,
        message: str,
        duration_ms: float = 0.0,
    ) -> ToolRunResult:
        # Keep the legacy ``result.error`` inspection contract while exposing
        # cancellation as its own typed terminal state.
        result = ToolRunResult.cancelled(
            tool_name=tool_name,
            message=message,
            duration_ms=duration_ms,
        )
        result.error = str(message)
        return result

    def _prune_deduplication_records_locked(self) -> None:
        now = time.monotonic()
        def finished(record: _DeduplicationRecord) -> bool:
            # A timeout response is terminal to its caller, not proof that a
            # noncooperative thread has stopped performing side effects.
            return (record.done.is_set()
                    and not (record.execution_ids or {record.execution_id}) & self._executions.keys())
        expired_execution_ids = [
            key for key, record in self._execution_records.items()
            if finished(record)
            and record.completed_at
            and now - record.completed_at > self._deduplication_ttl_seconds
        ]
        for key in expired_execution_ids:
            self._execution_records.pop(key, None)
        expired_idempotency_keys = [
            key for key, record in self._idempotency_records.items()
            if finished(record)
            and record.completed_at
            and now - record.completed_at > self._deduplication_ttl_seconds
        ]
        for key in expired_idempotency_keys:
            self._idempotency_records.pop(key, None)

        # Bound completed history while preserving all active executions.
        completed = sorted(
            {id(record): record for record in self._execution_records.values()
             if finished(record)}.values(),
            key=lambda item: item.completed_at,
        )
        overflow = max(0, len(completed) - self._deduplication_max_records)
        for record in completed[:overflow]:
            for execution_id in record.execution_ids or {record.execution_id}:
                self._execution_records.pop(execution_id, None)
            if record.idempotency_key:
                self._idempotency_records.pop((record.tool_name, record.idempotency_key), None)

    def _claim_execution(
        self,
        tool_name: str,
        execution_id: str,
        idempotency_key: str,
        idempotency_mode: str,
        request_fingerprint: str,
    ) -> tuple[_DeduplicationRecord, bool, Optional[str]]:
        with self._state_lock:
            self._prune_deduplication_records_locked()
            existing = self._execution_records.get(execution_id)
            if existing is not None:
                if existing.tool_name != tool_name:
                    return existing, False, (
                        f"execution_id '{execution_id}'은 이미 "
                        f"{existing.tool_name} 도구에 사용되었습니다."
                    )
                if existing.request_fingerprint != request_fingerprint:
                    return existing, False, (
                        f"execution_id '{execution_id}'을 서로 다른 입력에 "
                        "재사용할 수 없습니다."
                    )
                return existing, False, None
            if idempotency_key and idempotency_mode != "none":
                existing = self._idempotency_records.get((tool_name, idempotency_key))
                if existing is not None:
                    if existing.request_fingerprint != request_fingerprint:
                        return existing, False, (
                            f"멱등 키 '{idempotency_key}'를 서로 다른 입력에 "
                            "재사용할 수 없습니다."
                        )
                    existing.execution_ids.add(execution_id)
                    self._execution_records[execution_id] = existing
                    return existing, False, None
            record = _DeduplicationRecord(
                execution_id=execution_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                execution_ids={execution_id},
            )
            self._execution_records[execution_id] = record
            if idempotency_key and idempotency_mode != "none":
                self._idempotency_records[(tool_name, idempotency_key)] = record
            return record, True, None

    def _await_existing_execution(
        self,
        record: _DeduplicationRecord,
        token: CancellationToken,
        timeout_seconds: float,
    ) -> PluginToolOutput:
        deadline = time.monotonic() + timeout_seconds
        while not record.done.wait(timeout=0.05):
            token.raise_if_cancelled()
            if time.monotonic() >= deadline:
                return ToolRunResult.failed(
                    tool_name=record.tool_name,
                    error=(
                        "동일한 멱등 요청이 이미 실행 중입니다. "
                        "중복 실행을 방지하기 위해 새 작업을 시작하지 않았습니다."
                    ),
                )
        if record.result is None:
            return ToolRunResult.failed(
                tool_name=record.tool_name,
                error="중복 요청의 기존 실행 결과를 확인할 수 없습니다.",
            )
        return self._clone_output(record.result)

    def _register_active_execution(
        self,
        execution_id: str,
        tool_name: str,
        token: CancellationToken,
        started_at: float,
    ) -> _ExecutionState:
        state = _ExecutionState(execution_id, tool_name, token, started_at)
        with self._state_lock:
            if self._shutting_down:
                token.cancel()
                raise ToolCancelledError("종료 중에는 새 도구를 실행하지 않습니다.")
            self._executions[execution_id] = state
            self._cancellations[execution_id] = token
            self._active_by_tool.setdefault(tool_name, set()).add(execution_id)
        return state

    def _cleanup_active_execution(self, execution_id: str) -> None:
        with self._state_lock:
            state = self._executions.pop(execution_id, None)
            self._cancellations.pop(execution_id, None)
            if state is None:
                return
            active = self._active_by_tool.get(state.tool_name)
            if active is not None:
                active.discard(execution_id)
                if not active:
                    self._active_by_tool.pop(state.tool_name, None)

    def _complete_execution_record(
        self,
        record: _DeduplicationRecord,
        result: PluginToolOutput,
    ) -> None:
        with self._state_lock:
            record.result = self._clone_output(result)
            record.completed_at = time.monotonic()
            record.done.set()

    def _record_runtime_result(
        self,
        plugin: BasePlugin,
        tool: ToolSchema,
        result: PluginToolOutput,
    ) -> None:
        """Persist only evidence-backed runtime truth for diagnostics.

        A schema-valid ``dict`` or a legacy string is not proof that the
        requested action happened.  Such results remain ``unchecked`` until a
        plugin returns the typed ToolRunResult evidence contract.
        """
        state = "unchecked"
        summary = "도구가 형식상 응답했지만 실행 증거를 제공하지 않았습니다."
        evidence: List[str] = []
        if isinstance(result, ToolRunResult):
            status = str(getattr(result.status, "value", result.status))
            evidence = [
                f"{item.kind}: {item.summary}" for item in (result.evidence or [])
                if getattr(item, "summary", "")
            ]
            if status == "succeeded" and evidence:
                state = "confirmed"
                summary = "증거가 포함된 실제 도구 실행이 성공했습니다."
            elif status == "failed":
                state = "failed"
                summary = str(result.error or result.raw_output or "도구 실행 실패")
            elif status == "partial":
                state = "partial"
                summary = str(result.raw_output or "도구가 일부만 완료되었습니다.")
            elif status == "cancelled":
                state = "failed"
                summary = str(result.error or result.raw_output or "도구 실행이 취소되었습니다.")
            else:
                summary = str(result.raw_output or "도구 결과가 검증되지 않았습니다.")
        snapshot = {
            "state": state,
            "summary": summary,
            "evidence": evidence[:10],
            "last_tool": tool.name,
            "last_execution_at": time.time(),
        }
        with self._state_lock:
            self._runtime_by_tool[tool.name] = dict(snapshot)
            self._runtime_by_plugin[plugin.name] = dict(snapshot)

    def execute_tool(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        *,
        execution_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> PluginToolOutput:
        with self._state_lock:
            if self._shutting_down:
                return self._cancelled_result(tool_name, "종료 중에는 새 도구를 실행하지 않습니다.")
        entry = self._tools.get(tool_name)
        if entry is None or not entry[0].enabled: return ToolRunResult.failed(tool_name=tool_name, error=f"등록되지 않은 도구: {tool_name}")
        plugin, tool = entry
        errors = self.validate_tool_call(tool_name, tool_input)
        if errors: return ToolRunResult.failed(tool_name=tool_name, error="; ".join(errors))
        run_id = str(execution_id or uuid.uuid4().hex).strip()
        if not run_id:
            return ToolRunResult.failed(tool_name=tool_name, error="execution_id는 비어 있을 수 없습니다.")
        key = str(idempotency_key or "").strip()
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {"tool": tool_name, "input": tool_input},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        record, owner, claim_error = self._claim_execution(
            tool_name, run_id, key, tool.idempotency, request_fingerprint,
        )
        if claim_error:
            return ToolRunResult.failed(tool_name=tool_name, error=claim_error)
        duplicate_token = cancellation_token or CancellationToken()
        if not owner:
            try:
                return self._await_existing_execution(
                    record, duplicate_token, tool.timeout_seconds,
                )
            except ToolCancelledError as exc:
                return self._cancelled_result(tool_name, str(exc))

        token = cancellation_token or CancellationToken()
        started = time.perf_counter()
        state = None
        result: PluginToolOutput
        lingering_future = None
        try:
            state = self._register_active_execution(run_id, tool_name, token, started)
            max_retries = tool.max_retries if self._automatic_retry_allowed(tool) else 0
            for attempt in range(max_retries + 1):
                if attempt:
                    from core.productization import METRICS
                    METRICS.increment(f"plugin.{tool_name}.retry")
                token.raise_if_cancelled()
                context = ToolExecutionContext(
                    execution_id=run_id,
                    tool_name=tool_name,
                    idempotency_key=key,
                    attempt=attempt,
                    started_at=started,
                    cancellation_token=token,
                    deadline=(time.monotonic() + tool.timeout_seconds if tool.execution_isolation == "process" else 0.0),
                )
                future = None
                try:
                    if tool.execution_isolation == "process":
                        # No ThreadPool wrapper: a native hang must not leave
                        # an unjoinable Python thread during application exit.
                        result = self._invoke_isolated(plugin, tool, tool_input, context, state)
                        break
                    else:
                        future = self._executor.submit(self._invoke, plugin, tool, tool_input, context)
                        with self._state_lock:
                            state.future = future
                        deadline = time.monotonic() + tool.timeout_seconds
                        while True:
                            token.raise_if_cancelled()
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise FutureTimeoutError()
                            try:
                                plugin_result = future.result(timeout=min(0.05, remaining))
                                break
                            except FutureTimeoutError:
                                if future.done():
                                    raise
                                continue
                    output_errors = self._validate_plugin_output(plugin_result, tool)
                    if output_errors:
                        result = ToolRunResult.failed(tool_name=tool_name, error="; ".join(output_errors), duration_ms=(time.perf_counter()-started)*1000)
                    else:
                        result = plugin_result
                    break
                except FutureTimeoutError:
                    # A running Python thread cannot be killed safely.  Signal
                    # cooperative cancellation and *never* launch a second
                    # attempt after an uncertain timeout.
                    token.cancel()
                    if future is not None:
                        future.cancel()
                        lingering_future = future
                    retry_note = (
                        " 부작용이 있는 도구는 중복 실행 위험으로 "
                        "자동 재시도하지 않았습니다."
                        if not self._automatic_retry_allowed(tool) else
                        " 기존 실행이 완전히 종료되지 않아 자동 재시도하지 않았습니다."
                    )
                    result = ToolRunResult.failed(
                        tool_name=tool_name,
                        error=f"도구 실행 시간 초과: {tool.timeout_seconds:g}초.{retry_note}",
                        duration_ms=(time.perf_counter()-started)*1000,
                    )
                    break
                except ToolCancelledError as exc:
                    if future is not None:
                        future.cancel()
                        lingering_future = future
                    result = self._cancelled_result(
                        tool_name,
                        str(exc),
                        (time.perf_counter()-started)*1000,
                    )
                    break
                except Exception as exc:
                    if attempt >= max_retries:
                        result = ToolRunResult.failed(tool_name=tool_name, error=f"Plugin 실행 오류: {exc}", duration_ms=(time.perf_counter()-started)*1000)
                        break
            else:  # pragma: no cover - the bounded loop always breaks/returns
                result = ToolRunResult.failed(tool_name=tool_name, error="Plugin 실행 결과가 없습니다.")
        except ToolCancelledError as exc:
            result = self._cancelled_result(
                tool_name,
                str(exc),
                (time.perf_counter()-started)*1000,
            )
        except Exception as exc:
            result = ToolRunResult.failed(
                tool_name=tool_name,
                error=f"Plugin 실행 오류: {exc}",
                duration_ms=(time.perf_counter()-started)*1000,
            )
        finally:
            # The deduplication result becomes visible atomically before a
            # duplicate caller can submit another side effect.
            if "result" in locals():
                self._record_runtime_result(plugin, tool, result)
                self._complete_execution_record(record, result)
            if lingering_future is not None and not lingering_future.done():
                lingering_future.add_done_callback(
                    lambda _future, current_id=run_id: self._cleanup_active_execution(current_id)
                )
            elif state is None or state.worker is None or not state.worker.running:
                self._cleanup_active_execution(run_id)
        return result

    def _validate_plugin_output(self, plugin_result: PluginToolOutput, tool: ToolSchema) -> List[str]:
        output = plugin_result.to_dict() if isinstance(plugin_result, ToolRunResult) else plugin_result
        if (tool.output_schema.get("required") == ["status", "raw_output", "evidence", "artifacts"]
                and not isinstance(plugin_result, ToolRunResult)):
            output = {"status": "unverified", "raw_output": str(plugin_result), "evidence": [], "artifacts": []}
        return self._validate_instance(output, tool.output_schema, "출력")

    def _invoke_isolated(
        self, plugin: BasePlugin, tool: ToolSchema, tool_input: Dict[str, Any],
        context: ToolExecutionContext, state: _ExecutionState,
    ) -> PluginToolOutput:
        from config import Config
        from core.plugin_worker import ProcessToolWorker

        worker = None
        acquired = False
        publication_attempted = False
        failure: Optional[Exception] = None
        result: PluginToolOutput = ToolRunResult.failed(tool_name=tool.name, error="격리 작업을 시작하지 못했습니다.")
        previous_context = self.current_execution_context()
        try:
            while not acquired:
                context.raise_if_cancelled()
                acquired = self._process_slots.acquire(timeout=min(0.03, max(0.0, context.deadline - time.monotonic())))
            worker = ProcessToolWorker(context.execution_id, tool.name, root=self._process_workspace_root)
            with self._state_lock:
                state.worker = worker
            context = replace(context, staging_directory=str(worker.workspace))
            self._execution_local.context = context
            context.raise_if_cancelled()
            isolated_input = plugin.prepare_isolated_input(tool.name, dict(tool_input), str(worker.workspace))
            if not isinstance(isolated_input, dict):
                raise PluginContractError("격리 입력 준비 결과는 JSON 객체여야 합니다.")
            context.raise_if_cancelled()
            worker.start({
                "module": type(plugin).__module__, "class_name": type(plugin).__qualname__,
                "input": isolated_input, "deadline": context.deadline,
                "idempotency_key": context.idempotency_key, "attempt": context.attempt,
                "started_at": context.started_at,
                "allowed_paths": list(Config.API_CONFIG.ALLOWED_PATHS or [str(Path(__file__).resolve().parent.parent)]),
            })
            plugin_result = worker.wait(context.deadline, context.cancellation_token)
            context.raise_if_cancelled()
            output_errors = self._validate_plugin_output(plugin_result, tool)
            if output_errors:
                raise PluginContractError("; ".join(output_errors))
            # A late, corrupt or wrong-run response never reaches publication.
            publication_attempted = True
            result = plugin.finalize_isolated_result(tool.name, dict(tool_input), isolated_input, plugin_result)
            context.raise_if_cancelled()
            output_errors = self._validate_plugin_output(result, tool)
            if output_errors:
                raise PluginContractError("; ".join(output_errors))
        except Exception as exc:
            failure = exc
            if isinstance(exc, (TimeoutError, ToolCancelledError)):
                context.cancellation_token.cancel()
        finally:
            if worker is not None:
                if failure is not None or worker.running:
                    worker.stop()
                worker.cleanup()
            self._execution_local.context = previous_context
            if acquired:
                self._process_slots.release()

        duration = (time.perf_counter() - context.started_at) * 1000
        if failure is not None:
            if worker is not None and worker.pid is not None:
                reason = (f"도구 실행 시간 초과: {tool.timeout_seconds:g}초" if isinstance(failure, TimeoutError)
                          else str(failure))
                application_note = ("산출물 저장을 끝까지 확인하지 못했습니다. " if publication_attempted
                                    else "격리 작업 결과를 적용하지 않았습니다. ")
                message = (f"{reason}. {application_note}"
                           "이미 발생한 외부 효과는 되돌려졌다고 확인할 수 없어 자동 재시도하지 않습니다.")
                result = ToolRunResult.unverified(tool_name=tool.name, raw_output=message, duration_ms=duration)
                result.error = message
            elif isinstance(failure, ToolCancelledError):
                result = self._cancelled_result(tool.name, str(failure), duration)
            else:
                result = ToolRunResult.failed(tool_name=tool.name, error=f"격리 작업 준비 오류: {failure}", duration_ms=duration)
        if isinstance(result, ToolRunResult) and worker is not None:
            result.duration_ms = max(0.0, duration)
            details = worker.evidence()
            details["external_effects_uncertain"] = bool(failure is not None and worker.pid is not None)
            details["result_applied"] = None if failure is not None and publication_attempted else failure is None
            result.evidence.append(Evidence(
                "process_isolation", "전용 worker의 종료와 임시 산출물 정리 상태를 확인했습니다.", details,
            ))
        return result

    def _invoke(
        self,
        plugin: BasePlugin,
        tool: ToolSchema,
        tool_input: Dict[str, Any],
        context: ToolExecutionContext,
    ):
        self._execution_local.context = context
        try:
            context.raise_if_cancelled()
            result = plugin.execute_tool(tool.name, dict(tool_input))
            result = asyncio.run(result) if inspect.isawaitable(result) else result
            context.raise_if_cancelled()
            return result
        finally:
            self._execution_local.context = None

    def current_execution_context(self) -> Optional[ToolExecutionContext]:
        return getattr(self._execution_local, "context", None)

    def get_active_execution_ids(self, tool_name: str = "") -> List[str]:
        with self._state_lock:
            if tool_name:
                return sorted(self._active_by_tool.get(tool_name, set()))
            return sorted(self._executions)

    def get_execution_result(self, execution_id: str) -> Optional[PluginToolOutput]:
        with self._state_lock:
            record = self._execution_records.get(str(execution_id))
            if record is None or not record.done.is_set() or record.result is None:
                return None
            return self._clone_output(record.result)

    def cancel_execution(self, execution_id: str) -> bool:
        with self._state_lock:
            token = self._cancellations.get(str(execution_id))
            state = self._executions.get(str(execution_id))
        if token is None:
            return False
        token.cancel()
        if state is not None and state.worker is not None:
            state.worker.request_cancel()
        return True

    def shutdown(self, timeout_seconds: float = 2.0) -> Dict[str, Any]:
        """Bound shutdown; report legacy native threads instead of faking exit.

        Existing Office/COM servers and unrelated processes are never killed.
        Only registered per-run Python workers are eligible for termination.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._state_lock:
            self._shutting_down = True
            states = list(self._executions.values())
        for state in states:
            state.token.cancel()
            if state.future is not None:
                state.future.cancel()
            if state.worker is not None:
                state.worker.request_cancel()
        for state in states:
            if state.worker is not None:
                remaining = max(0.0, deadline - time.monotonic())
                state.worker.stop(grace=0, kill_wait=remaining / 2)
        self._executor.shutdown(wait=False, cancel_futures=True)
        while time.monotonic() < deadline:
            with self._state_lock:
                active = list(self._executions.values())
                # Clean a retained worker whose first termination attempt was
                # unsuccessful, but leave an invocation still publishing alone.
                for state in active:
                    record = self._execution_records.get(state.execution_id)
                    if (state.worker is not None and not state.worker.running
                            and record is not None and record.done.is_set()):
                        state.worker.cleanup()
                        self._cleanup_active_execution(state.execution_id)
                if not self._executions:
                    break
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        with self._state_lock:
            return {
                "remaining_execution_ids": sorted(self._executions),
                "remaining_worker_pids": [state.worker.pid for state in self._executions.values()
                                          if state.worker is not None and state.worker.running],
                "inprocess_threads_cannot_be_forcibly_stopped": any(
                    state.future is not None and not state.future.done() for state in self._executions.values()),
            }

    def cancel_tool(self, tool_name_or_execution_id: str) -> bool:
        """Cancel one execution ID, or all active runs of a legacy tool name."""
        identifier = str(tool_name_or_execution_id)
        if self.cancel_execution(identifier):
            return True
        with self._state_lock:
            execution_ids = list(self._active_by_tool.get(identifier, set()))
        cancelled = False
        for execution_id in execution_ids:
            cancelled = self.cancel_execution(execution_id) or cancelled
        return cancelled

    def get_plugin_statuses(self) -> List[PluginStatus]:
        contract_issues = self.validate_contracts()
        statuses: List[PluginStatus] = []
        for plugin in self.plugins.values():
            try:
                diagnostics = list(plugin.diagnose())
            except Exception as exc:
                diagnostics = [f"진단 실행 실패: {type(exc).__name__}: {exc}"]
            try:
                tools = list(plugin.get_tools())
            except Exception as exc:
                tools = []
                diagnostics.append(f"도구 목록 확인 실패: {type(exc).__name__}: {exc}")

            current_contract_issues: List[str] = []
            for tool in tools:
                current_contract_issues.extend(contract_issues.get(tool.name, []))
            diagnostics.extend(current_contract_issues)

            install_failures = [
                item for item in diagnostics
                if item.startswith((
                    "지원하지 않는 OS:", "의존성 누락:", "의존성 DLL 로드 실패:",
                    "진단 실행 실패:", "도구 목록 확인 실패:",
                ))
            ]
            installation_state = "failed" if install_failures else "confirmed"
            contract_state = "failed" if current_contract_issues else "confirmed"
            connection = plugin.probe_connection()
            authentication = plugin.probe_authentication()
            for label, probe in (("연결", connection), ("인증", authentication)):
                if probe.state in {"failed", "unchecked"} and probe.summary:
                    diagnostics.append(f"{label} {probe.state}: {probe.summary}")

            with self._state_lock:
                runtime = dict(self._runtime_by_plugin.get(plugin.name, {}))
            runtime_state = str(runtime.get("state") or "unchecked")
            if installation_state == "failed" or contract_state == "failed" or runtime_state == "failed":
                verification_state = "failed"
            elif runtime_state == "confirmed":
                verification_state = "confirmed"
            elif runtime_state == "partial":
                verification_state = "partial"
            else:
                verification_state = "unchecked"
            statuses.append(PluginStatus(
                name=plugin.name,
                version=plugin.version,
                installed=_state_as_optional_bool(installation_state),
                connected=_state_as_optional_bool(connection.state),
                authenticated=_state_as_optional_bool(authentication.state),
                verified=_state_as_optional_bool(verification_state),
                enabled=plugin.enabled,
                diagnostics=list(dict.fromkeys(diagnostics)),
                registered=True,
                installation_state=installation_state,
                connection_state=connection.state,
                authentication_state=authentication.state,
                contract_state=contract_state,
                verification_state=verification_state,
                runtime_state=runtime_state,
                runtime_summary=str(runtime.get("summary") or "실제 도구 실행 이력이 없습니다."),
                runtime_evidence=list(runtime.get("evidence") or []),
                last_tool=str(runtime.get("last_tool") or ""),
                last_execution_at=runtime.get("last_execution_at"),
            ))

        for key, failure in sorted(self._load_failures.items()):
            message = str(failure.get("error") or "Plugin 로드 실패")
            statuses.append(PluginStatus(
                name=key,
                version=str(failure.get("version") or "unknown"),
                installed=False,
                connected=None,
                authenticated=None,
                verified=False,
                enabled=False,
                diagnostics=[message],
                registered=False,
                installation_state="failed",
                connection_state="not_applicable",
                authentication_state="not_applicable",
                contract_state="unchecked",
                verification_state="failed",
                runtime_state="unchecked",
                runtime_summary="Plugin 로드에 실패하여 실행되지 않았습니다.",
            ))
        return statuses

    def present_result(self, tool_name: str, result: PluginToolOutput) -> str:
        entry = self._tools.get(tool_name)
        presentation_value = result.raw_output if isinstance(result, ToolRunResult) else result
        return (entry[0].present_result(tool_name, presentation_value)
                if entry and entry[0].enabled else str(presentation_value))

    def load_plugins_from_directory(self, directory: Optional[str] = None):
        path = Path(directory).resolve() if directory else Path(__file__).resolve().parent.parent / "plugins"
        if not path.exists() or str(path) in self._loaded_directories: return
        for item in sorted(path.iterdir(), key=lambda candidate: candidate.name.casefold()):
            if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
                try:
                    module = importlib.import_module(f"plugins.{item.stem}")
                except Exception as exc:
                    key = item.stem
                    self._load_failures[key] = {
                        "error": f"Plugin 모듈 로드 실패 ({item.name}): {type(exc).__name__}: {exc}",
                    }
                    print(f"[Plugin] Load failed: {item.name} ({type(exc).__name__}: {exc})")
                    continue
                for class_name, obj in inspect.getmembers(module):
                    if (inspect.isclass(obj) and issubclass(obj, BasePlugin)
                            and obj is not BasePlugin and obj.__module__ == module.__name__):
                        key = f"{item.stem}.{class_name}"
                        try:
                            instance = obj()
                            if getattr(instance, "auto_discover", True):
                                self.register_plugin(instance)
                            self._load_failures.pop(key, None)
                        except Exception as exc:
                            self._load_failures[key] = {
                                "error": (
                                    f"Plugin 클래스 로드 실패 ({item.name}:{class_name}): "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                            }
                            print(
                                f"[Plugin] Load failed: {item.name}:{class_name} "
                                f"({type(exc).__name__}: {exc})"
                            )
        self._loaded_directories.add(str(path))


_plugin_registry: Optional[PluginRegistry] = None


def get_plugin_registry() -> PluginRegistry:
    global _plugin_registry
    if _plugin_registry is None: _plugin_registry = PluginRegistry()
    return _plugin_registry
