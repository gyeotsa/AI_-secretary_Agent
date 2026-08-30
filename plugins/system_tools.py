"""
System Tools Plugin for Jarvis
- get_time: 현재 시간 가져오기
- get_date: 현재 날짜 가져오기
"""
from core.plugin import BasePlugin, ToolSchema, IntentSchema
from core.tool_result import Evidence, ToolRunResult
from datetime import datetime
import re

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
            ),
            ToolSchema(
                name="repeat_text",
                description="사용자가 지정한 문자열을 변경하지 않고 그대로 반환해 읽습니다",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "읽을 문자열"}},
                    "required": ["text"],
                },
                required_permissions=[],
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
            IntentSchema(
                "speech.repeat_text", "지정 문자열 그대로 읽기", "repeat_text",
                [],
                [],
                execution_hints=["읽어", "발음", "말해"],
                utterance_patterns=[
                    r"[\"'“”‘’][^\"'“”‘’]+[\"'“”‘’](?:을|를|라고)?\s*(?:읽어|발음해|말해)",
                    r"^[A-Za-z0-9_.+\-]+(?:을|를)?\s*(?:읽어|발음해)(?:\s*봐|\s*줘)?[?!.]*$",
                ],
            ),
        ]

    def extract_slots(self, intent_name: str, text: str, current_slots: dict) -> dict:
        slots = dict(current_slots)
        if intent_name != "speech.repeat_text":
            return slots
        value = re.sub(
            r"(?:을|를)?\s*(?:한번\s*)?(?:읽어\s*봐|읽어\s*줘|발음해\s*봐|"
            r"발음해\s*줘|말해\s*봐|말해\s*줘)\s*[?!.]*$",
            "",
            text.strip(),
            flags=re.IGNORECASE,
        ).strip(" \"'“”‘’")
        if value:
            slots["text"] = value
        return slots
    
    def execute_tool(self, tool_name: str, tool_input: dict):
        if tool_name == "get_time":
            now = datetime.now().astimezone()
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=now.isoformat(timespec="seconds"),
                evidence=[Evidence(
                    "system_clock",
                    "운영체제 로컬 시계와 타임존을 조회했습니다.",
                    {
                        "timezone": str(now.tzinfo),
                        "utc_offset": now.strftime("%z"),
                        "captured_at": now.isoformat(timespec="seconds"),
                    },
                )],
            )
        elif tool_name == "get_date":
            now = datetime.now().astimezone()
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=now.date().isoformat(),
                evidence=[Evidence(
                    "system_clock",
                    "운영체제 로컬 날짜와 타임존을 조회했습니다.",
                    {
                        "timezone": str(now.tzinfo),
                        "utc_offset": now.strftime("%z"),
                        "captured_at": now.isoformat(timespec="seconds"),
                    },
                )],
            )
        elif tool_name == "list_plugins":
            from core.plugin import get_plugin_registry
            registry = get_plugin_registry()
            enabled = [
                {
                    "name": plugin.name,
                    "tools": [tool.name for tool in plugin.get_tools()],
                }
                for plugin in registry.plugins.values() if plugin.enabled
            ]
            output = "\n".join(
                f"{plugin.name}: {', '.join(tool.name for tool in plugin.get_tools())}"
                for plugin in registry.plugins.values() if plugin.enabled
            )
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=output,
                evidence=[Evidence(
                    "plugin_registry",
                    f"활성 Plugin {len(enabled)}개의 Registry 상태를 조회했습니다.",
                    {"plugins": enabled, "count": len(enabled)},
                )],
            )
        elif tool_name == "repeat_text":
            value = str(tool_input.get("text", "")).strip()
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=value,
                evidence=[Evidence(
                    "input_echo",
                    "사용자가 지정한 문자열과 반환 문자열의 동일성을 확인했습니다.",
                    {"characters": len(value)},
                )],
            )
        return ToolRunResult.failed(
            tool_name=tool_name, error=f"알 수 없는 툴 '{tool_name}'"
        )

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name == "get_time":
            value = datetime.fromisoformat(result)
            return f"현재 시간은 {value.strftime('%H:%M:%S')}입니다, 보스."
        if tool_name == "get_date":
            value = datetime.fromisoformat(result)
            return f"오늘은 {value.year}년 {value.month}월 {value.day}일입니다, 보스."
        return result
