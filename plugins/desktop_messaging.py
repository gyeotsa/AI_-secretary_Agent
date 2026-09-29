"""카카오톡 등 Windows 데스크톱 메신저의 승인 기반 실제 전송 Plugin."""
from __future__ import annotations

from typing import Any, Dict, List
import json
import re

from core.desktop_messaging import DesktopMessagingRuntime
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Evidence, ToolRunResult
from core.semantic_request import explicit_control, literal_reply


class DesktopMessagingPlugin(BasePlugin):
    # ``톡``은 단독으로 쓰였을 때만 카카오톡 별칭으로 취급한다. 틱톡처럼 다른
    # 단어 안에 포함된 경우까지 매칭하면 전혀 무관한 요청을 외부 전송 Intent로
    # 오인할 수 있으므로 한글/영문/숫자 경계를 명시한다.
    _PROVIDER_PATTERN = re.compile(
        r"(?:카카오\s*톡|카톡|kakaotalk|(?<![0-9A-Za-z가-힣])톡(?![0-9A-Za-z가-힣]))",
        re.I,
    )
    # 실행 의도를 특정 종결형 하나에 묶지 않는다. 사용자는 ``보내줘``뿐
    # 아니라 ``전달해줄 수 있어?``/``전송해 주세요``처럼 같은 의미를
    # 다양한 높임말과 가능형으로 표현한다. 동사의 의미는 Registry가
    # 담당하고, 아래 패턴은 그 동사가 끝나는 위치만 찾는다.
    _SEND_PATTERN = re.compile(
        r"(?:보내|전송|전달)\s*(?:해)?\s*"
        r"(?:줘|주세요|줄래|줄\s*수\s*있(?:어|을까|나요|니)?|"
        r"주실\s*수\s*있(?:나요|을까요)?|할\s*수\s*있(?:어|을까|나요|니)?|해)?",
        re.I,
    )
    _RECIPIENT_PATTERN = re.compile(
        r"(?P<recipient>[^,!?\n]+?)(?:에게|한테|께|이게)\s*(?P<message>.*)$",
        re.I | re.S,
    )

    def __init__(self):
        super().__init__()
        self.name = "desktop_messaging"
        self.description = "수신자 대화창을 검증한 Windows 데스크톱 메신저 전송"
        self.supported_os = ["Windows"]
        self.runtime = DesktopMessagingRuntime()

    def get_tools(self) -> List[ToolSchema]:
        return [ToolSchema(
            "desktop_send_message", "카카오톡 수신자 대화창을 확인한 뒤 메시지를 실제 전송합니다",
            {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["kakaotalk"], "default": "kakaotalk"},
                    "recipient": {"type": "string", "minLength": 1,
                                  "description": "카카오톡에 표시된 상대방 또는 대화방 이름. 전화번호나 카카오 계정 ID가 아님"},
                    "message": {"type": "string", "minLength": 1,
                                "description": "사용자가 전하라고 지정한 메시지 원문. 작업 요청 문장 자체를 본문으로 쓰지 않음"},
                },
                "required": ["provider", "recipient", "message"],
                "additionalProperties": False,
            },
            ["windows_api", "external_send"], side_effect="external_send",
            verification_required=True, timeout_seconds=30, max_retries=0,
        )]

    def get_intents(self) -> List[IntentSchema]:
        return [IntentSchema(
            "messaging.send", "카카오톡 메시지 전송", "desktop_send_message",
            [
                "카카오톡으로", "카카오 톡으로", "카톡으로", "카톡 보내",
                "카카오톡 보내", "카카오톡 메시지", "카톡 메시지",
                "톡 하나 보내", "톡 보내",
            ],
            [
                SlotSchema("provider", "사용할 메신저", "어떤 메신저로 보낼까요, 보스?", role="constraint"),
                SlotSchema("recipient", "메시지를 받을 사람", "누구에게 보낼까요, 보스?", role="target"),
                SlotSchema("message", "보낼 메시지", "어떤 내용을 보낼까요, 보스?", role="parameter"),
            ],
            execution_hints=[
                "보내", "전송", "전달", "보내줘", "보내줄래",
                "전송해줘", "전달해줘", "전달해줄 수 있어",
            ],
            utterance_patterns=[
                # 메신저를 먼저 말하는 형태: "카톡으로 형택이한테 ... 보내줘"
                r"(?:카카오\s*톡|카톡|kakaotalk|(?<![0-9A-Za-z가-힣])톡(?![0-9A-Za-z가-힣]))"
                r".*(?:에게|한테|께|이게).*(?:보내|전송|전달)",
                # 자연스러운 한국어 어순: "형택이한테 ... 톡 하나 보내줘"
                r"(?:에게|한테|께|이게).*"
                r"(?:카카오\s*톡|카톡|kakaotalk|(?<![0-9A-Za-z가-힣])톡(?![0-9A-Za-z가-힣]))"
                r".*(?:보내|전송|전달)",
            ],
            constraint_slots=["provider"], request_type="external_send",
        )]

    def extract_slots(self, intent_name: str, text: str,
                      current_slots: Dict[str, Any]) -> Dict[str, Any]:
        slots = dict(current_slots)
        if intent_name != "messaging.send":
            return slots
        # Preserve original whitespace and punctuation inside message content.
        # Routing may normalize a separate view, but payload extraction cannot.
        normalized = str(text or "").strip()
        if explicit_control(normalized):
            return slots
        if self._PROVIDER_PATTERN.search(normalized):
            slots["provider"] = "kakaotalk"

        # Work from the full utterance.  The old parser discarded everything before
        # "카톡", so natural Korean word order such as "형택이에게 ... 카톡 보내줘"
        # lost both the recipient and the message.
        actions = list(self._SEND_PATTERN.finditer(normalized))
        action = actions[-1] if actions else None
        before_action = normalized[:action.start()] if action else normalized
        # "테스트라고 톡 하나 보내줘"의 ``하나``는 메시지 본문이 아니라
        # 메신저 단위를 세는 말이다. provider와 붙어 있는 경우에만 제거해 실제
        # 본문인 "테스트"를 보존한다.
        before_action = re.sub(
            r"(?:카카오\s*톡|카톡|kakaotalk|(?<![0-9A-Za-z가-힣])톡(?![0-9A-Za-z가-힣]))"
            r"\s*(?:메시지\s*)?하나\s*$",
            " ", before_action, flags=re.I,
        )
        # Remove particles only when attached to the provider token.  The old
        # unanchored substitution deleted e.g. '에서' from '학교에서 만나'.
        provider_boundary = self._PROVIDER_PATTERN.pattern + r"(?:으로|에서|을|를)?"
        before_action = re.sub(r"^\s*" + provider_boundary + r"\s*", "", before_action, flags=re.I)
        before_action = re.sub(r"\s*" + provider_boundary + r"\s*$", "", before_action, flags=re.I).strip(" ,")
        match = self._RECIPIENT_PATTERN.search(before_action)
        if match:
            recipient = re.sub(r"^(?:혹시|그러면|그럼|이번에는)\s+", "", match.group("recipient").strip())
            message = self._clean_message(match.group("message"))
            if recipient:
                slots["recipient"] = recipient
            if message:
                slots["message"] = message
        elif slots.get("recipient") and not slots.get("message") and action:
            # A supplied body followed by a quotative/send boundary is a reply
            # to the pending body question, not a second full task requiring a
            # recipient. Never remove arbitrary suffixes inside the body.
            body = literal_reply(normalized)
            if body is None and re.search(r"(?:이라고|라고)\s*$", before_action):
                body = self._clean_message(before_action)
            if body:
                slots["message"] = body
        elif self._is_short_slot_answer(normalized):
            # A pending clarification may contain just a name or just the body.
            # Fill only the first missing slot; never reinterpret a complete sentence.
            value = normalized.strip(" \t\r\n\"'“”‘’")
            if slots.get("provider") and not slots.get("recipient"):
                slots["recipient"] = value
            elif slots.get("recipient") and not slots.get("message"):
                slots["message"] = value
        return slots

    @staticmethod
    def _clean_message(value: str) -> str:
        message = str(value or "").strip()
        # A label needs an explicit delimiter/particle. A body beginning with
        # '메시지 확인해줘' is actual content, not a label to erase.
        message = re.sub(r"^(?:메시지|내용)(?:은|는|[:：])\s*", "", message)
        message = re.sub(r"\s*(?:이라고|라고)\s*$", "", message)
        return message.strip(" \t\r\n\"'“”‘’")

    @staticmethod
    def _is_short_slot_answer(text: str) -> bool:
        value = str(text or "").strip()
        if not value or len(value) > 80 or "\n" in value:
            return False
        if explicit_control(value):
            return False
        if re.search(r"(?:앞으로|이제부터|기억|잊지|뜻|의미|설정|변경|말하면)", value):
            return False
        if re.search(r"(?:보내|전송|전달|카카오\s*톡|카톡|kakaotalk)", value, re.I):
            return False
        return True

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        if tool_name != "desktop_send_message":
            return ToolRunResult.failed(tool_name=tool_name, error=f"알 수 없는 메신저 도구: {tool_name}")
        try:
            result = self.runtime.send(
                str(tool_input.get("provider", "kakaotalk")),
                str(tool_input.get("recipient", "")),
                str(tool_input.get("message", "")),
            )
            if not (result.get("send_accepted_verified")
                    and result.get("outgoing_message_verified")):
                reason = str(result.get("unverified_reason") or
                             "새 보낸 메시지 말풍선을 검증하지 못했습니다.")
                return ToolRunResult.unverified(
                    tool_name=tool_name,
                    raw_output=json.dumps(result, ensure_ascii=False),
                    evidence=[Evidence(
                        "desktop_message_dispatch_unverified",
                        "키 입력 이후 동일 본문의 새 보낸 메시지 말풍선을 확인하지 못해 완료로 처리하지 않았습니다.",
                        {
                            "provider": result.get("provider"),
                            "recipient": result.get("recipient"),
                            "window_title": result.get("window_title"),
                            "send_verification": result.get("send_verification"),
                            "reason": reason,
                            "delivery_receipt_verified": False,
                        },
                    )],
                )
            return ToolRunResult.successful(
                tool_name=tool_name,
                raw_output=json.dumps(result, ensure_ascii=False),
                evidence=[Evidence(
                    "desktop_message_dispatch",
                    "수신자와 정확히 일치하는 대화창에서 동일 입력창 포커스와 본문을 재검증하고, Enter 뒤 동일 본문의 새 보낸 메시지 말풍선을 확인했습니다.",
                    {
                        "provider": result["provider"], "recipient": result["recipient"],
                        "window_title": result["window_title"], "window_handle": result["window_handle"],
                        "process_id": result["process_id"],
                        "input_dispatched_at": result["input_dispatched_at"],
                        "send_accepted_verified": result["send_accepted_verified"],
                        "outgoing_message_verified": result["outgoing_message_verified"],
                        "send_verification": result["send_verification"],
                        "delivery_receipt_verified": False,
                    },
                )],
            )
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=f"메시지 전송 중단: {exc}")

    def present_result(self, tool_name: str, result: str) -> str:
        if tool_name != "desktop_send_message" or str(result).startswith("오류:"):
            return str(result)
        data = json.loads(str(result))
        if not (data.get("send_accepted_verified") and data.get("outgoing_message_verified")):
            return (
                f"카카오톡에서 {data.get('recipient', '수신자')}님의 대화창에 전송 입력은 했지만, "
                "동일 본문의 새 보낸 메시지 말풍선을 확인하지 못해 완료로 처리하지 않았습니다, 보스."
            )
        return (
            f"카카오톡에서 {data['recipient']}님의 대화창에 동일한 본문의 새 보낸 메시지 "
            "말풍선이 나타난 것까지 확인했습니다. 상대방의 읽음 여부는 확인하지 못했습니다, 보스."
        )
