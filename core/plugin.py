"""
Plugin SDK for Jarvis
- BasePlugin: 모든 플러그인의 기본 클래스
- PluginRegistry: 플러그인 등록 및 관리
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
import importlib
import inspect
from pathlib import Path
import json
from core.runtime.event_bus import get_event_bus, Event

@dataclass
class ToolSchema:
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)  # JSON Schema
    required_permissions: List[str] = field(default_factory=list)

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
    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
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
    
    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """툴 실행 (어떤 플러그인의 툴인지 찾아서 실행)"""
        for plugin in self.plugins.values():
            if not plugin.enabled:
                continue
            for tool in plugin.get_tools():
                if tool.name == tool_name:
                    # 권한은 ToolExecutor가 내장/플러그인 도구 모두에 대해 한 번만 검사합니다.
                    return plugin.execute_tool(tool_name, tool_input)
        return f"오류: 툴 '{tool_name}'을 찾을 수 없습니다"
    
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
