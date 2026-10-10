"""Chrome login entry point; credentials are confined to the local account UI."""
import re

from core.browser_login import BrowserLoginService, SITES, validate_login_url
from core.interface_control import get_interface_control_bridge
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Evidence, ToolRunResult, ToolRunStatus


class BrowserLoginPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "browser_login"
        self.description = "현재 Chrome의 웹사이트 로그인·암호화 계정 저장·기존 세션 재사용"
        self.dependencies = ["mcp"]
        self.service = BrowserLoginService()

    def get_tools(self):
        return [ToolSchema("browser_login_open",
            "Chrome 로그인 페이지와 계정 저장 창을 엽니다. ID·비밀번호는 채팅이 아닌 이 창에서 저장합니다. 페이지 열기는 로그인 성공이 아닙니다.",
            {"type": "object", "properties": {"url": {"type": "string", "maxLength": 2000}},
             "required": ["url"], "additionalProperties": False}, ["browser"],
            side_effect="execute", max_retries=0)]

    def get_intents(self):
        return [IntentSchema("web.login", "웹사이트 로그인 창 열기", "browser_login_open",
            ["로그인", "로그인해줘", "로그인 해줘", "계정 연결", "사이트 로그인"],
            [SlotSchema("url", "로그인할 웹사이트", "로그인할 서비스 이름 또는 HTTPS 주소를 알려주세요.")],
            execution_hints=["로그인", "연결", "열어", "열", "해줘", "해 줘"],
            utterance_patterns=[r"(?:네이버|구글|카카오|인스타그램|당근|https://\S+).{0,25}로그인",
                                r"로그인.{0,25}(?:네이버|구글|카카오|인스타그램|당근|https://\S+)"],
            request_type="execute")]

    def extract_slots(self, intent_name, text, current_slots):
        from plugins.browser import BrowserPlugin
        slots = dict(current_slots)
        urls = BrowserPlugin._extract_url_literals(text)
        if len(urls) == 1:
            slots["url"] = urls[0]
        elif len(urls) > 1:
            slots.pop("url", None)
        else:
            aliases = {"naver": r"네이버|\bnaver\b", "google": r"구글|\bgoogle\b",
                       "kakao": r"카카오(?:톡)?|\bkakao\b", "instagram": r"인스타(?:그램)?|\binstagram\b",
                       "daangn": r"당근|\bdaangn\b"}
            sites = [key for key, pattern in aliases.items() if re.search(pattern, text, re.I)]
            if len(sites) == 1:
                slots["url"] = SITES[sites[0]]["url"]
            elif len(sites) > 1:
                slots.pop("url", None)
        return slots

    def execute_tool(self, name, data):
        if name != "browser_login_open" or set(data) != {"url"}:
            return ToolRunResult.failed(tool_name=name, error="로그인 도구에는 사이트 주소만 입력할 수 있습니다.")
        try:
            url = validate_login_url(data["url"])
            opened = self.service.open(url)
            bridge = get_interface_control_bridge()
            if bridge.available("open_surface"):
                bridge.call("open_surface", "browser_login:" + url)
            return ToolRunResult(tool_name=name, status=ToolRunStatus.UNVERIFIED,
                raw_output="Chrome에 로그인 페이지를 열었습니다. 계정 저장 창에서 저장 계정으로 로그인하세요. 로그인 성공은 아직 확인하지 않았습니다.",
                evidence=[Evidence("chrome_login_open", "Chrome에 주소 열기를 전달했습니다. 인증은 미확인입니다.", opened)])
        except Exception:
            return ToolRunResult.failed(tool_name=name,
                error="Chrome 로그인 창을 열지 못했습니다. HTTPS 주소와 Chrome 설치 상태를 확인하세요.")

    def on_unload(self):
        self.service.disconnect()
