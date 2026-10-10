"""Reusable Chrome mail capabilities for Anis and its startup workflow."""
import json

from jsonschema import Draft202012Validator

from core.browser_mail import BrowserMailError, get_browser_mail_service
from core.mail_analysis import MailAnalysisError
from core.plugin import BasePlugin, IntentSchema, ToolCancelledError, ToolSchema
from core.tool_result import Evidence, ToolRunResult
from core.turn_context import check_turn_cancelled


class BrowserMailPlugin(BasePlugin):
    def __init__(self, service=None, brief_service=None):
        super().__init__()
        self.name = "browser_mail"
        self.description = "Gmail·네이버 메일 현황·수신·회신·초안 집계, 받은편지함 조회와 로컬 요약·분류·카테고리 설정"
        self.auth_required = True
        self.auth_type = "Chrome 메일 계정"
        self._service = service
        self._brief_service = brief_service

    @property
    def service(self):
        if self._service is None:
            self._service = get_browser_mail_service()
        return self._service

    def is_connected(self):
        return None

    def is_authenticated(self):
        return None

    def get_tools(self):
        provider = {"type": "string", "enum": ["gmail", "naver"]}
        return [
            ToolSchema("browser_mail_collect_summary", "Gmail·네이버 메일의 현재 현황을 새로 수집합니다. Chrome 기존 로그인·메일 조회·로컬 요약 분류를 함께 사용해 신규 수신, 회신 검토(AI 추정), 실제 임시보관함 초안을 따로 집계합니다. 원문은 반환하지 않습니다.", {
                "type": "object", "properties": {"providers": {"type": "array", "items": provider,
                    "minItems": 1, "maxItems": 2, "uniqueItems": True}}, "additionalProperties": False,
            }, ["cloud_read"], side_effect="read", timeout_seconds=21600, cancellable=True, max_retries=0),
            ToolSchema("browser_mail_list_inbox", "Chrome의 기존 계정으로 받은편지함 최신 화면 최대50건을 조회합니다. 같은 도구의 message_ref로 메일을 읽을 수 있습니다.", {
                "type": "object", "properties": {"provider": provider,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                "required": ["provider"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="read", timeout_seconds=180, cancellable=True, max_retries=0),
            ToolSchema("browser_mail_read_message", "받은편지함 조회에서 얻은 message_ref의 본문을 로컬에서 읽고 요약·분류·회신 검토 결과만 반환합니다. 원문·첨부파일·HTML은 모델 도구 결과에 포함하지 않습니다. 읽음 처리될 수 있습니다.", {
                "type": "object", "properties": {"provider": provider,
                    "message_ref": {"type": "string", "pattern": "^[a-f0-9-]{36}:[0-9]{1,2}$"}},
                "required": ["provider", "message_ref"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="read", timeout_seconds=240, cancellable=True, max_retries=0),
            ToolSchema("browser_mail_category_settings", "메일 자동 분석 설정과 사용자 카테고리·분류 기준·알림 대상을 조회하거나 저장합니다. 저장 시 전체 카테고리를 전달합니다. 실제 알림 전송은 하지 않습니다.", {
                "type": "object", "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]}, "enabled": {"type": "boolean"},
                    "categories": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                        "type": "object", "properties": {"id": {"type": "string"},
                            "name": {"type": "string", "maxLength": 40},
                            "description": {"type": "string", "maxLength": 300}, "notify": {"type": "boolean"}},
                        "required": ["id", "name", "description", "notify"], "additionalProperties": False}},
                }, "additionalProperties": False,
            }, ["filesystem_write"], side_effect="change", timeout_seconds=30, cancellable=True, max_retries=0),
        ]

    def get_intents(self):
        return [IntentSchema("mail.brief", "Gmail·네이버 수신·회신 검토·임시보관함 초안 건수 요약",
            "browser_mail_collect_summary", ["메일 현황", "새 메일 건수", "메일 요약", "메일 몇 건", "메일 확인"], [],
            execution_hints=["알려", "확인", "요약", "조회"],
            utterance_patterns=[r"(?:메일|이메일).{0,25}(?:현황|건수|몇\s*건|요약)",
                                r"(?:새|신규|받은).{0,12}(?:메일|이메일).{0,20}(?:알려|확인|있어|왔|건)"],
            request_type="query", freshness="live", requires_sources=True)]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if intent_name == "mail.brief":
            lowered = text.casefold()
            providers = [name for name, words in (("naver", ("네이버", "naver")),
                         ("gmail", ("구글", "지메일", "gmail", "google"))) if any(word in lowered for word in words)]
            if providers:
                slots["providers"] = providers
        return slots

    def present_result(self, tool_name, result):
        if tool_name == "browser_mail_collect_summary":
            try:
                data = json.loads(str(result))
                if isinstance(data, dict) and isinstance(data.get("summary"), str) and data["summary"].strip():
                    return data["summary"]
            except (TypeError, ValueError):
                pass
            return "메일 현황의 표시 결과를 확인하지 못했습니다."
        return super().present_result(tool_name, result)

    def execute_tool(self, tool_name, tool_input):
        try:
            tool = next((tool for tool in self.get_tools() if tool.name == tool_name), None)
            if tool is None:
                return ToolRunResult.failed(tool_name=tool_name, error="지원하지 않는 Chrome 메일 도구입니다.")
            if not Draft202012Validator(tool.input_schema).is_valid(tool_input):
                return ToolRunResult.failed(tool_name=tool_name, error="메일 도구 입력 형식을 확인하세요.")

            def checkpoint():
                check_turn_cancelled()
                context = self.get_execution_context()
                if context is not None:
                    context.raise_if_cancelled()

            checkpoint()
            if tool_name == "browser_mail_collect_summary":
                if self._brief_service is None:
                    from core.mail_brief import get_mail_brief_service
                    self._brief_service = get_mail_brief_service()
                data = self._brief_service.collect(providers=tool_input.get("providers", ("naver", "gmail")), checkpoint=checkpoint)
                result = (ToolRunResult.unverified if all(row.get("list_count") is None for row in data.get("providers", []))
                          else ToolRunResult.successful)
                return result(tool_name=tool_name, raw_output=json.dumps(data, ensure_ascii=False),
                    evidence=[Evidence("browser_mail_summary", "확인된 건수와 확인 불가 항목을 구분한 수집 결과입니다.")])
            if tool_name == "browser_mail_category_settings":
                action = tool_input.get("action", "get")
                if action == "set":
                    if "enabled" not in tool_input or "categories" not in tool_input:
                        raise MailAnalysisError("설정 저장에는 자동 분석 여부와 전체 카테고리가 필요합니다.")
                    data = self.service.analysis.save_settings(tool_input["enabled"], tool_input["categories"])
                else:
                    if "enabled" in tool_input or "categories" in tool_input:
                        raise MailAnalysisError("설정을 변경하려면 action=set을 사용하세요.")
                    data = self.service.analysis.settings()
            else:
                if not self.service.workflow_lock.acquire(blocking=False):
                    raise BrowserMailError("다른 메일 수집이 진행 중입니다. 완료 후 다시 요청하세요.")
                try:
                    if tool_name == "browser_mail_list_inbox":
                        data = self.service.list_inbox(tool_input["provider"], tool_input.get("limit", 50), checkpoint=checkpoint)
                    else:
                        detail = self.service.read_message(tool_input["provider"], tool_input["message_ref"], checkpoint=checkpoint)
                        record = self.service.analysis.analyze_message(detail, checkpoint=checkpoint)
                        data = {key: record[key] for key in ("status", "summary", "category_id", "reply_required", "partial")}
                finally:
                    self.service.workflow_lock.release()
            checkpoint()
            return ToolRunResult.successful(tool_name=tool_name, raw_output=json.dumps(data, ensure_ascii=False),
                evidence=[Evidence("browser_mail_verified", "Chrome 메일 화면 또는 암호화 저장 설정에서 결과를 확인했습니다.")])
        except ToolCancelledError:
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, (BrowserMailError, MailAnalysisError)) else "메일 작업을 확인하지 못했습니다. 연결과 로컬 모델 상태를 확인하세요."
            return ToolRunResult.failed(tool_name=tool_name, error=error)
