"""Plain text summaries for structured records shown in user-facing screens."""
from __future__ import annotations

import json
import re
from datetime import datetime

from core.productization import SensitiveDataRedactor
from core.assistant_settings import SETTING_DEFINITIONS


_LABELS = {
    "title": "제목", "name": "이름", "description": "내용", "summary": "요약",
    "text": "내용", "content": "내용", "body": "내용", "goal": "목표",
    "status": "상태", "state": "상태", "reported_status": "기록된 상태",
    "verification": "확인 방법", "verification_status": "확인 상태",
    "kind": "기록 종류", "type": "유형", "role": "작성자", "provider": "프로그램",
    "timestamp": "기록 시각", "created_at": "생성 시각", "updated_at": "최근 변경",
    "due_at": "마감", "completed_at": "완료 시각", "synced_at": "최근 수집",
    "snoozed_until": "다시 보기", "last_seen_at": "마지막 확인",
    "source": "출처", "source_ref": "출처", "source_path": "원본 위치",
    "workspace_path": "작업 폴더", "file_path": "파일", "path": "위치", "line": "줄",
    "url": "주소", "receipts": "확인한 근거", "evidence": "진행 근거",
    "data": "세부 내용", "payload": "세부 내용", "result": "결과",
    "steps": "계획 단계", "versions": "계획 이력", "version": "계획 버전",
    "patch": "변경 내용", "priority": "우선순위", "dependencies": "먼저 할 일",
    "waiting_reason": "대기 이유", "pending_question": "확인이 필요한 내용",
    "last_tool": "최근 사용 도구", "tool_name": "사용 도구", "phase": "진행 상태",
    "artifacts": "결과물", "artifact": "결과물", "output": "결과", "error": "문제",
    "errors": "확인이 필요한 문제", "last_error": "최근 수집 문제",
    "message": "안내", "reason": "이유", "action": "작업", "key": "항목",
    "value": "값", "email": "이메일", "success": "성공", "enabled": "사용 중",
    "count": "개수", "events": "새 기록", "notes": "메모", "hint": "도움말",
    "importance": "중요도", "confidence": "신뢰도", "epistemic_status": "정보 확인 상태",
    "topics": "주제", "source_label": "출처", "source_uri": "원본 위치",
    "created": "생성 시각", "updated": "최근 변경", "session_count": "세션 수",
}
_VALUES = {
    "pending": "할 일", "confirmed": "확인됨", "in_progress": "진행 중",
    "blocked": "대기 중", "done": "완료", "completed": "완료", "cancelled": "취소",
    "suggested": "확인할 제안", "reported": "AI 기록 (직접 확인 필요)",
    "verified": "실행 결과 확인", "user_confirmed": "사용자 직접 확인",
    "not_started": "첫 수집 대기", "running": "진행 중", "syncing": "수집 중",
    "ok": "정상", "success": "성공", "failed": "실패", "error": "문제 발생",
    "partial": "일부 확인 필요", "idle": "대기 중", "ready": "준비됨",
    "profile_database": "사용자 프로필 조회", "tool_result": "도구 실행 결과",
    "tool_call": "도구 실행", "message": "대화", "plan": "작업 계획", "task": "할 일",
    "session": "세션", "user": "사용자", "assistant": "아니스", "system": "시스템",
    "internal": "아니스", "codex": "Codex", "claude_code": "Claude Code",
    "requested": "실행 요청", "returned": "실행 결과 수신", "email": "이메일",
    "preference": "선호", "fact": "사실", "case": "사례", "project": "프로젝트",
    "conversation_pattern": "대화 패턴", "user_claim": "사용자가 알려준 정보",
    "reference": "참고 자료", "inferred": "추론한 정보", "unknown": "확인되지 않음",
    "awaiting_user": "사용자 답변 대기", "awaiting_approval": "승인 대기", "queued": "실행 대기",
    "paused": "일시정지", "interrupted": "중단됨", "unverified": "직접 확인 필요",
    "expired": "만료됨", "unchecked": "확인 전", "not_applicable": "해당 없음",
    "verifying": "결과 확인 중", "stopped": "중지됨", "enabled": "사용 중",
    "disabled": "사용 안 함", "not_run": "실행 전", "healthy": "정상", "degraded": "일부 확인 필요",
    "actions_map": "작업별 기억 목록", "topics_map": "주제별 기억 목록",
    "actions_index": "작업 목록", "topics_index": "주제 목록", "index": "지식 목록",
}
_VALUES.update({key: definition.label for key, definition in SETTING_DEFINITIONS.items()})
_HIDDEN = {"id", "event_key", "session_key", "hash", "fingerprint", "offset", "position"}
_LITERAL_TEXT = {"text", "content", "body", "code", "document", "description", "summary", "goal", "message"}


def _plain(value):
    return str(SensitiveDataRedactor.redact(str(value)))


def _source(value):
    if not isinstance(value, str):
        return value
    if ".db#" in value:
        for name, label in (("dialogue_state", "아니스 작업 기록"), ("agent_tasks", "아니스 작업 기록"),
                            ("plan_runtime", "아니스 계획 기록"), ("action_journal", "아니스 실행 기록")):
            if name in value:
                return label
        return "아니스 내부 기록"
    if value == "user:manual_plan":
        return "사용자가 작성한 계획"
    match = re.fullmatch(r"(.+\.(?:jsonl|json|log|txt)):(\d+)", value, re.I)
    return f"{match[1]} ({match[2]}번째 줄)" if match else value


def _format(value, key=""):
    if value is None or value == "":
        return ""
    if isinstance(value, str) and key not in _LITERAL_TEXT:
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                pass
            else:
                if isinstance(parsed, (dict, list)):
                    return _format(parsed, key)
        if re.fullmatch(r"[0-9a-f]{24,}", value, re.I):
            return ""
        if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y년 %m월 %d일 %H:%M")
            except ValueError:
                pass
        return _plain(_VALUES.get(value, value))
    if isinstance(value, dict):
        lines = []
        for field, item in value.items():
            field = str(field)
            if (field in _HIDDEN or field.endswith(("_id", "_json"))
                    or SensitiveDataRedactor.SENSITIVE_KEYS.search(field)):
                continue
            if field == "source_ref":
                item = _source(item)
            text = _format(item, field)
            if not text:
                continue
            label = _LABELS.get(field, field.replace("_", " "))
            if field == "priority" and isinstance(item, int) and 0 <= item <= 3:
                text = ("보통", "관심", "중요", "긴급")[item]
            if "\n" in text:
                lines.append(label + "\n" + "\n".join("  " + line for line in text.splitlines()))
            else:
                lines.append(f"{label}: {text}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        return "\n\n".join("• " + text.replace("\n", "\n  ") for item in value if (text := _format(item, key)))
    if isinstance(value, bool):
        return "예" if value else "아니요"
    return _plain(value)


def format_details(value) -> str:
    """Show labels and meaningful values, omitting technical IDs and secrets.

    Text/body fields keep their original formatting; structured JSON outside
    those fields is expanded into readable plain text. Escape this result before
    inserting it into HTML.
    """
    return _format(value)
