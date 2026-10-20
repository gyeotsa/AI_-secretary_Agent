"""Readable summaries keep provenance and source text without internal payloads."""
import json

from ui.readable_details import format_details


def test_nested_structured_details_use_labels_and_omit_ids_and_secrets():
    text = format_details(json.dumps({
        "event_id": "private-event", "session_key": "private-session", "status": "done",
        "updated_at": "2026-10-20T01:20:00Z", "payload_json": "internal serialized duplicate",
        "receipts": [{"kind": "profile_database", "summary": "프로필 조회를 확인했습니다.",
                      "data": {"key": "email", "access_token": "test-secret-value"}}],
        "pending_question": "다음 일정을 확인해주세요.", "success": False,
    }, ensure_ascii=False))
    assert "상태: 완료" in text
    assert "2026년 10월 20일" in text
    assert "프로필 조회를 확인했습니다." in text
    assert "항목: 이메일" in text
    assert "확인이 필요한 내용: 다음 일정을 확인해주세요." in text
    assert "성공: 아니요" in text
    for hidden in ("private-event", "private-session", "test-secret-value", "access_token", "payload_json", "{"):
        assert hidden not in text


def test_source_paths_are_useful_and_original_text_format_is_preserved():
    original = '{"actual": "source code"}\n    <b>문서 원문</b>'
    text = format_details({"content": original, "source_ref": "logs/session.jsonl:42"})
    assert original in text.replace("\n  ", "\n")
    assert "logs/session.jsonl (42번째 줄)" in text
    assert format_details({"source_ref": "data/dialogue_state.db#agent_tasks/private"}) == "출처: 아니스 작업 기록"
    assert "private" not in format_details({"source_ref": "data/action_journal.db#actions/private"})


def test_empty_and_generic_statuses_are_readable():
    assert format_details({"id": "internal", "receipts": [], "description": None}) == ""
    assert format_details("confirmed") == "확인됨"
    assert format_details("awaiting_approval") == "승인 대기"
    assert format_details({"epistemic_status": "user_claim"}) == "정보 확인 상태: 사용자가 알려준 정보"
