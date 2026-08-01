"""
Plugin SDK for Jarvis
- BasePlugin: 모든 플러그인의 기본 클래스
- PluginRegistry: 플러그인 등록 및 관리
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable, Union
import importlib
import inspect
from pathlib import Path
import json
from core.runtime.event_bus import get_event_bus, Event
from core.tool_result import ToolRunResult

PluginToolOutput = Union[str, ToolRunResult]

@dataclass
class ToolSchema:
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)  # JSON Schema
    required_permissions: List[str] = field(default_factory=list)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    side_effect: str = "auto"
    verification_required: bool = True

    def __post_init__(self):
        actions = set(self.name.casefold().split("_"))
        if self.side_effect == "auto":
            if actions & {"get", "read", "list", "find", "search", "status", "diff", "log"}:
                self.side_effect = "read"
            elif "mail_send" in self.required_permissions or actions & {"send", "push", "publish"}:
                self.side_effect = "external_send"
            elif actions & {"create", "write", "update", "delete", "remove", "add", "commit", "set"}:
                self.side_effect = "change"
            elif any(permission in {
                "filesystem_write", "git_commit", "git_push"
            } for permission in self.required_permissions):
                self.side_effect = "change"
            elif "windows_api" in self.required_permissions or actions & {
                "launch", "close", "focus", "execute", "run", "speak", "repeat"
            }:
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
    """Registry-owned execution contract exposed to every router/executor."""
    name: str
    description: str
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    side_effect: str
    required_permissions: List[str]
    verification_required: bool


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

    def __post_init__(self):
        parts = self.name.split(".", 1)
        if not self.domain:
            self.domain = parts[0]
        if not self.action:
            self.action = parts[1] if len(parts) > 1 else self.name
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
            target = next((slot.name for slot in self.slots if slot.role == "target"), "")
            if not target:
                target = next(
                    (slot.name for slot in self.slots
                     if slot.name in {"target", "path", "filename", "query", "name", "location"}),
                    "",
                )
            self.target_slot = target

class BasePlugin(ABC):
    """플러그인 기본 클래스"""
    
    def __init__(self):
        self.name = ""
        self.description = ""
        self.version = "1.0.0"
        self.author = ""
        self.enabled = True
        
    @abstractmethod
    def get_tools(self) -> List[ToolSchema]:
        """플러그인이 제공하는 툴 목록 반환"""
        pass
    
    @abstractmethod
    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> PluginToolOutput:
        """툴 실행"""
        pass
    
    def on_load(self):
        """플러그인이 로드될 때 호출"""
        pass
    
    def on_unload(self):
        """플러그인이 언로드될 때 호출"""
        pass
    
    def on_event(self, event: Event):
        """이벤트가 발생했을 때 호출"""
        pass

    def get_intents(self) -> List[IntentSchema]:
        """플러그인이 처리할 수 있는 사용자 intent 계약."""
        return []

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        """플러그인 도메인에 맞게 새 발화의 slot을 누적한다."""
        return dict(current_slots)

    def present_result(self, tool_name: str, result: PluginToolOutput) -> str:
        """검증된 원문 결과를 사용자용 문장으로 변환한다."""
        return str(result)

class PluginRegistry:
    """플러그인 레지스트리"""
    
    def __init__(self):
        self.plugins: Dict[str, BasePlugin] = {}
        self._event_bus = get_event_bus()
        self._loaded_directories: set[str] = set()
    
    def register_plugin(self, plugin: BasePlugin):
        """플러그인 등록"""
        if plugin.name in self.plugins:
            return
        plugin.on_load()
        self.plugins[plugin.name] = plugin
        # 이벤트 구독
        self._event_bus.subscribe("*", plugin.on_event)
        print(f"[Plugin] Registered: {plugin.name} v{plugin.version}")
    
    def unregister_plugin(self, plugin_name: str):
        """플러그인 등록 해제"""
        if plugin_name not in self.plugins:
            return
        plugin = self.plugins[plugin_name]
        plugin.on_unload()
        # 이벤트 구독 해제
        self._event_bus.unsubscribe("*", plugin.on_event)
        del self.plugins[plugin_name]
        print(f"[Plugin] Unregistered: {plugin_name}")
    
    def get_plugin(self, plugin_name: str) -> Optional[BasePlugin]:
        """플러그인 가져오기"""
        return self.plugins.get(plugin_name)
    
    def get_all_tools(self) -> List[ToolSchema]:
        """모든 플러그인의 툴 목록 가져오기"""
        tools = []
        for plugin in self.plugins.values():
            if plugin.enabled:
                tools.extend(plugin.get_tools())
        return tools

    def get_all_intents(self) -> List[tuple[BasePlugin, IntentSchema]]:
        intents = []
        for plugin in self.plugins.values():
            if plugin.enabled:
                intents.extend((plugin, intent) for intent in plugin.get_intents())
        return intents

    def get_capability(self, tool_name: str) -> Optional[CapabilityContract]:
        for tool in self.get_all_tools():
            if tool.name == tool_name:
                return CapabilityContract(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                    side_effect=tool.side_effect,
                    required_permissions=list(tool.required_permissions),
                    verification_required=tool.verification_required,
                )
        return None

    def get_capabilities(self) -> List[CapabilityContract]:
        return [
            contract for tool in self.get_all_tools()
            if (contract := self.get_capability(tool.name)) is not None
        ]

    def validate_tool_call(self, tool_name: str, tool_input: Dict[str, Any],
                           request_type: str = "") -> List[str]:
        """Validate required input fields at the Registry boundary before execution."""
        contract = self.get_capability(tool_name)
        if contract is None:
            return [f"등록되지 않은 도구입니다: {tool_name}"]
        if not isinstance(tool_input, dict):
            return ["도구 입력은 객체여야 합니다."]
        errors = []
        expected_effect = {
            "query": "read", "execute": "execute", "change": "change",
            "external_send": "external_send",
        }.get(request_type, request_type)
        if request_type and expected_effect != contract.side_effect:
            errors.append(
                f"요청 종류({request_type})와 도구 부작용({contract.side_effect})이 다릅니다."
            )
        required = contract.input_schema.get("required", [])
        errors.extend(
            f"필수 입력이 없습니다: {name}"
            for name in required if tool_input.get(name) in (None, "")
        )
        return errors

    def validate_contracts(self) -> Dict[str, List[str]]:
        """Return all incomplete or internally inconsistent Plugin contracts."""
        issues: Dict[str, List[str]] = {}
        tool_names = {tool.name for tool in self.get_all_tools()}
        valid_effects = {"read", "execute", "change", "external_send"}
        for tool in self.get_all_tools():
            current = []
            if tool.side_effect not in valid_effects:
                current.append(f"알 수 없는 side_effect: {tool.side_effect}")
            if not tool.output_schema:
                current.append("output_schema 누락")
            if current:
                issues[tool.name] = current
        for _plugin, intent in self.get_all_intents():
            current = []
            if intent.tool_name not in tool_names:
                current.append(f"등록되지 않은 Tool 참조: {intent.tool_name}")
            if intent.request_type not in {"query", "execute", "change", "external_send"}:
                current.append(f"알 수 없는 request_type: {intent.request_type}")
            contract = self.get_capability(intent.tool_name)
            expected_effect = {
                "query": "read", "execute": "execute", "change": "change",
                "external_send": "external_send",
            }.get(intent.request_type, intent.request_type)
            if contract and expected_effect != contract.side_effect:
                current.append(
                    f"request_type({intent.request_type})과 side_effect({contract.side_effect}) 불일치"
                )
            if current:
                issues[f"intent:{intent.name}"] = current
        return issues
    
    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> PluginToolOutput:
        """툴 실행 (어떤 플러그인의 툴인지 찾아서 실행)"""
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            for tool in plugin.get_tools():
                if tool.name == tool_name:
                    # 권한은 ToolExecutor가 내장/플러그인 도구 모두에 대해 한 번만 검사합니다.
                    return plugin.execute_tool(tool_name, tool_input)
        return f"오류: 툴 '{tool_name}'을 찾을 수 없습니다"

    def present_result(self, tool_name: str, result: PluginToolOutput) -> str:
        for plugin in self.plugins.values():
            if plugin.enabled and any(tool.name == tool_name for tool in plugin.get_tools()):
                return plugin.present_result(tool_name, str(result))
        return str(result)
    
    def load_plugins_from_directory(self, directory: Optional[str] = None):
        """지정된 디렉토리에서 플러그인 로드"""
        dir_path = Path(directory).resolve() if directory else Path(__file__).resolve().parent.parent / "plugins"
        if not dir_path.exists():
            return
        directory_key = str(dir_path)
        if directory_key in self._loaded_directories:
            return
        # __init__.py와 .py 파일 로드
        for item in dir_path.iterdir():
            if item.is_file() and item.suffix == ".py" and item.name != "__init__.py":
                # 모듈 이름: plugins.filename
                module_name = f"plugins.{item.stem}"
                try:
                    module = importlib.import_module(module_name)
                    # 모듈에서 BasePlugin 상속 클래스 찾기
                    for name, obj in inspect.getmembers(module):
                        if (inspect.isclass(obj) and 
                            issubclass(obj, BasePlugin) and 
                            obj != BasePlugin):
                            self.register_plugin(obj())
                except Exception as e:
                    print(f"[Plugin] Failed to load {item.name}: {e}")
        self._loaded_directories.add(directory_key)

# Singleton
_plugin_registry = None

def get_plugin_registry() -> PluginRegistry:
    global _plugin_registry
    if _plugin_registry is None:
        _plugin_registry = PluginRegistry()
    return _plugin_registry
