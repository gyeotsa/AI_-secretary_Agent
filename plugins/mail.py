"""SMTP 메일 전송과 RFC 822 초안 저장 플러그인."""
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema


class MailPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "mail"
        self.description = "환경변수 기반 SMTP 전송과 로컬 메일 초안 저장"

    def get_tools(self) -> List[ToolSchema]:
        message = {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"},
        }
        return [
            ToolSchema("mail_create_draft", "RFC 822 형식의 메일 초안을 저장합니다", {
                "type": "object", "properties": {**message, "path": {"type": "string"}},
                "required": ["to", "subject", "body", "path"],
            }, ["filesystem_write"]),
            ToolSchema("mail_send_smtp", "설정된 SMTP 계정으로 메일을 전송합니다", {
                "type": "object", "properties": message,
                "required": ["to", "subject", "body"],
            }, ["mail_send"]),
        ]

    @staticmethod
    def _message(data: Dict[str, Any], require_sender: bool = False) -> EmailMessage:
        sender = os.getenv("MAIL_FROM") or os.getenv("MAIL_SMTP_USERNAME")
        if require_sender and not sender:
            raise ValueError("MAIL_FROM 또는 MAIL_SMTP_USERNAME 설정이 필요합니다.")
        message = EmailMessage()
        if sender:
            message["From"] = sender
        message["To"] = str(data["to"])
        message["Subject"] = str(data["subject"])
        message.set_content(str(data["body"]))
        return message

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        try:
            if tool_name == "mail_create_draft":
                message = self._message(tool_input)
                path = Path(str(tool_input["path"])).expanduser().resolve()
                ok, error = SafetyLayer.validate_path(str(path))
                if not ok:
                    return error
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(message.as_bytes())
                return f"메일 초안 저장 성공: {path}"
            if tool_name == "mail_send_smtp":
                message = self._message(tool_input, require_sender=True)
                host = os.getenv("MAIL_SMTP_HOST")
                username = os.getenv("MAIL_SMTP_USERNAME")
                password = os.getenv("MAIL_SMTP_PASSWORD")
                if not all((host, username, password)):
                    raise ValueError("MAIL_SMTP_HOST/USERNAME/PASSWORD 설정이 필요합니다.")
                port = int(os.getenv("MAIL_SMTP_PORT", "465"))
                with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
                    smtp.login(username, password)
                    smtp.send_message(message)
                return "메일 전송 성공"
            return f"오류: 알 수 없는 툴 '{tool_name}'"
        except Exception as exc:
            return f"오류: {exc}"
