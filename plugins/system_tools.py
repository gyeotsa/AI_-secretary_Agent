"""
System Tools Plugin for Jarvis
- get_time: 현재 시간 가져오기
- get_date: 현재 날짜 가져오기
"""
from core.plugin import BasePlugin, ToolSchema
from datetime import datetime

class SystemToolsPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "system_tools"
        self.description = "기본 시스템 도구 (시간, 날짜)"
        self.version = "1.0.0"
        self.author = "Jarvis Team"
    
    def get_tools(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name="get_time",
                description="현재 시간을 가져옵니다 (HH:MM:SS 형식)",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": []
                },
                required_permissions=[]
            ),
            ToolSchema(
                name="get_date",
                description="현재 날짜를 가져옵니다 (YYYY-MM-DD 형식)",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": []
                },
                required_permissions=[]
            ),
            ToolSchema(
                name="list_plugins",
                description="현재 실제로 등록된 플러그인과 제공 도구 목록을 조회합니다",
                input_schema={"type": "object", "properties": {}, "required": []},
                required_permissions=[]
            )
        ]
    
    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_time":
            return datetime.now().strftime("%H:%M:%S")
        elif tool_name == "get_date":
            return datetime.now().strftime("%Y-%m-%d")
        elif tool_name == "list_plugins":
            from core.plugin import get_plugin_registry
            registry = get_plugin_registry()
            return "\n".join(
                f"{plugin.name}: {', '.join(tool.name for tool in plugin.get_tools())}"
                for plugin in registry.plugins.values() if plugin.enabled
            )
        return f"오류: 알 수 없는 툴 '{tool_name}'"
