"""Current-Chrome Naver calendar tools; never substitute local ICS creation."""
import json
import hashlib

from core.naver_calendar import NaverCalendarService
from core.plugin import BasePlugin, PluginStateProbe, ToolSchema
from core.tool_result import Evidence, ToolRunResult


class NaverCalendarPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "naver_calendar"
        self.description = "현재 Chrome의 공식 확장으로 네이버 캘린더 화면 조회"
        self.dependencies = ["mcp"]
        self.auth_required = True
        self.auth_type = "사용자가 선택한 Chrome 캘린더 로그인"
        self.service = NaverCalendarService()

    def get_tools(self):
        return [ToolSchema("naver_calendar_read_view",
            "사용자가 연결한 네이버 캘린더의 현재 표시 화면과 캘린더 목록을 읽습니다. 전체 기간/계정 동기화가 아닙니다.",
            {"type":"object", "properties":{}, "additionalProperties":False}, ["browser"],
            side_effect="read", max_retries=0, cancellable=True, timeout_seconds=100)]

    def probe_connection(self):
        return PluginStateProbe("confirmed" if self.service.status()["connected"] else "unchecked",
                               "선택한 캘린더 탭 연결 상태입니다. 일정 등록 성공 상태가 아닙니다.")

    def is_authenticated(self):
        return True if self.service.status()["connected"] else None

    def on_unload(self):
        self.service.disconnect()

    def execute_tool(self, tool_name, tool_input):
        if tool_name != "naver_calendar_read_view":
            raise ValueError("알 수 없는 네이버 캘린더 도구입니다.")
        context = self.get_execution_context()
        from core.turn_context import check_turn_cancelled
        def checkpoint():
            check_turn_cancelled()
            if context:
                context.raise_if_cancelled()
        value = self.service.observe(checkpoint=checkpoint)
        NaverCalendarService.validate_view(value)
        output = json.dumps(value, ensure_ascii=False)
        return ToolRunResult.successful(tool_name=tool_name,
            raw_output=output,
            evidence=[Evidence("calendar_visible_view", "연결된 캘린더 탭의 현재 본문을 실제 읽었습니다.",
                {"scope":"visible_view", "account_fingerprint":hashlib.sha256(value["account"].encode()).hexdigest(),
                 "payload_sha256":hashlib.sha256(output.encode()).hexdigest(), "complete_account":False})])
