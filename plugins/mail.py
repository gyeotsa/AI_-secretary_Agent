"""SMTP 메일 전송과 RFC 822 초안 저장 플러그인."""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


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

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any]):
        try:
            if tool_name == "mail_create_draft":
                message = self._message(tool_input)
                path = Path(str(tool_input["path"])).expanduser().resolve()
                ok, error = SafetyLayer.validate_path(str(path))
                if not ok:
                    return ToolRunResult.failed(tool_name=tool_name, error=error)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(message.as_bytes())
                parsed = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                if parsed.get("To") != str(tool_input["to"]):
                    raise ValueError("저장된 메일 초안의 수신자가 요청과 다릅니다.")
                if parsed.get("Subject") != str(tool_input["subject"]):
                    raise ValueError("저장된 메일 초안의 제목이 요청과 다릅니다.")
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output=f"메일 초안 저장 성공: {path}",
                    evidence=[Evidence(
                        "rfc822_message",
                        "저장된 RFC 822 메일의 수신자·제목·본문 구조를 확인했습니다.",
                        {
                            "path": str(path),
                            "to": str(parsed.get("To")),
                            "subject": str(parsed.get("Subject")),
                            "size": path.stat().st_size,
                        },
                    )],
                    artifacts=[Artifact("email_draft", str(path), {"format": "eml"})],
                )
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
                    refused = smtp.send_message(message)
                if refused:
                    raise RuntimeError(f"일부 수신자 전송이 거부되었습니다: {list(refused)}")
                return ToolRunResult.successful(
                    tool_name=tool_name,
                    raw_output="메일 전송 성공",
                    evidence=[Evidence(
                        "smtp_delivery",
                        "SMTP 서버가 수신자 거부 없이 메시지를 접수했습니다.",
                        {"host": host, "port": port, "to": str(tool_input["to"])},
                    )],
                )
            return ToolRunResult.failed(
                tool_name=tool_name, error=f"알 수 없는 툴 '{tool_name}'"
            )
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
