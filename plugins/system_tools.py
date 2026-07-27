"""
System Tools Plugin for Jarvis
- get_time: 현재 시간 가져오기
- get_date: 현재 날짜 가져오기
"""
from core.plugin import BasePlugin, ToolSchema, IntentSchema
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

    def get_intents(self) -> list[IntentSchema]:
        return [
            IntentSchema(
                "system.current_time", "현재 로컬 시각 조회", "get_time",
                ["현재 시각", "현재 시간", "지금 몇 시", "몇 시야", "몇시야"],
                [], execution_hints=["시간", "시각", "몇 시", "몇시"],
            ),
            IntentSchema(
                "system.current_date", "현재 로컬 날짜 조회", "get_date",
                ["오늘 날짜", "현재 날짜", "오늘 며칠", "몇 일이야", "몇일이야"],
                [], execution_hints=["날짜", "며칠", "몇 일", "몇일"],
            ),
        ]
    
    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_time":
            return datetime.now().astimezone().isoformat(timespec="seconds")
        elif tool_name == "get_date":
            return datetime.now().astimezone().date().isoformat()
        elif tool_name == "list_plugins":
            from core.plugin import get_plugin_registry
            registry = get_plugin_registry()
            return "\n".join(
                f"{plugin.name}: {', '.join(tool.name for tool in plugin.get_tools())}"
                for plugin in registry.plugins.values() if plugin.enabled
            )
        return f"오류: 알 수 없는 툴 '{tool_name}'"

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name == "get_time":
            value = datetime.fromisoformat(result)
            return f"현재 시간은 {value.strftime('%H:%M:%S')}입니다, 보스."
        if tool_name == "get_date":
            value = datetime.fromisoformat(result)
            return f"오늘은 {value.year}년 {value.month}월 {value.day}일입니다, 보스."
        return result
