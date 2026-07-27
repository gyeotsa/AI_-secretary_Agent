from plugins.calendar import CalendarPlugin
from plugins.mail import MailPlugin
from core.verifier import ToolVerifier
from core.tool_result import ToolRunResult


def test_calendar_creates_standard_ics(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    target = tmp_path / "meeting.ics"
    result = CalendarPlugin().execute_tool("calendar_create_event", {
        "path": str(target), "title": "주간 회의", "start": "2026-07-23T10:00:00",
        "end": "2026-07-23T11:00:00", "description": "진행 상황 검토",
    })
    assert isinstance(result, ToolRunResult)
    assert result.succeeded
    assert result.evidence[0].kind == "icalendar_structure"
    assert result.raw_output.startswith("캘린더 이벤트 생성 성공:")
    content = target.read_text(encoding="utf-8")
    assert "BEGIN:VCALENDAR" in content and "SUMMARY:주간 회의" in content
    verification = ToolVerifier().verify("calendar_create_event", {"path": str(target)}, result)
    assert verification.success


def test_path_denial_is_never_verified_as_success():
    verification = ToolVerifier().verify(
        "mail_create_draft", {"path": "C:/blocked/test.ics"},
        "경로 'C:\\blocked\\test.ics'는 허용되지 않습니다.",
    )
    assert not verification.success


def test_mail_draft_uses_configured_sender(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    monkeypatch.setenv("MAIL_FROM", "sender@example.com")
    target = tmp_path / "draft.eml"
    result = MailPlugin().execute_tool("mail_create_draft", {
        "to": "receiver@example.com", "subject": "테스트", "body": "본문", "path": str(target),
    })
    assert isinstance(result, ToolRunResult)
    assert result.succeeded
    assert result.evidence[0].kind == "rfc822_message"
    assert result.raw_output.startswith("메일 초안 저장 성공:")
    assert b"receiver@example.com" in target.read_bytes()


def test_mail_draft_does_not_require_smtp_account(tmp_path, monkeypatch):
    monkeypatch.setattr("config.Config.API_CONFIG.ALLOWED_PATHS", [str(tmp_path)])
    monkeypatch.delenv("MAIL_FROM", raising=False)
    monkeypatch.delenv("MAIL_SMTP_USERNAME", raising=False)
    target = tmp_path / "offline-draft.eml"
    result = MailPlugin().execute_tool("mail_create_draft", {
        "to": "receiver@example.com", "subject": "오프라인 초안", "body": "본문", "path": str(target),
    })
    assert isinstance(result, ToolRunResult)
    assert result.succeeded
    assert result.raw_output.startswith("메일 초안 저장 성공:")


def test_mail_send_requires_environment_credentials(monkeypatch):
    monkeypatch.setenv("MAIL_FROM", "sender@example.com")
    monkeypatch.delenv("MAIL_SMTP_HOST", raising=False)
    result = MailPlugin().execute_tool("mail_send_smtp", {
        "to": "receiver@example.com", "subject": "테스트", "body": "본문",
    })
    assert isinstance(result, ToolRunResult)
    assert not result.succeeded
    assert result.raw_output.startswith("오류:")
    assert "MAIL_SMTP_HOST" in result.raw_output


def test_mail_send_records_smtp_acceptance_without_credentials_in_evidence(monkeypatch):
    monkeypatch.setenv("MAIL_FROM", "sender@example.com")
    monkeypatch.setenv("MAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MAIL_SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("MAIL_SMTP_PASSWORD", "secret")

    class SMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def login(self, *_args):
            return None

        def send_message(self, _message):
            return {}

    monkeypatch.setattr("plugins.mail.smtplib.SMTP_SSL", SMTP)
    result = MailPlugin().execute_tool("mail_send_smtp", {
        "to": "receiver@example.com", "subject": "테스트", "body": "본문",
    })
    assert isinstance(result, ToolRunResult)
    assert result.succeeded
    assert result.evidence[0].kind == "smtp_delivery"
    assert "secret" not in str(result.to_dict())
