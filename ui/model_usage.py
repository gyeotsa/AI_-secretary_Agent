"""Usage labels and bars; missing metrics never mean zero usage."""
from datetime import datetime
import math


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _time(value):
    value = _number(value)
    if value is not None:
        try:
            return datetime.fromtimestamp(value).strftime("%m/%d %H:%M")
        except (ValueError, OSError, OverflowError):
            pass
    return "미제공"


def _count(value):
    value = _number(value)
    return int(value) if value is not None and value == int(value) else None


def quota_rows(data):
    rows = []
    buckets = data.get("rate_limits")
    buckets = buckets if isinstance(buckets, (list, tuple)) else []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        name = bucket.get("limit_name") or bucket.get("limit_id") or "사용 한도"
        name = name[:80] if isinstance(name, str) else "사용 한도"
        for key, fallback in (("primary", "단기 한도"), ("secondary", "장기 한도")):
            window = bucket.get(key)
            if not isinstance(window, dict):
                continue
            minutes = _number(window.get("window_duration_mins"))
            if minutes == 0:
                minutes = None
            duration = (f"{minutes / 1440:g}일" if minutes is not None and minutes >= 1440
                        else f"{minutes / 60:g}시간" if minutes is not None and minutes >= 60
                        else f"{minutes:g}분" if minutes is not None else fallback)
            used = _number(window.get("used_percent"))
            if used is not None:
                used = min(100, used)
            rows.append({"title": f"{name} · {duration}", "used": used,
                         "remaining": 100 - used if used is not None else None,
                         "reset": _time(window.get("resets_at"))})
    tokens = data.get("token_usage")
    if isinstance(tokens, dict):
        used, left = _count(tokens.get("used_tokens")), _count(tokens.get("remaining_tokens"))
        if used is not None and left is not None and used + left > 0:
            rows.insert(0, {"title": "토큰 한도", "used": used / (used + left) * 100,
                            "remaining": left / (used + left) * 100, "reset": None})
    return rows


def format_usage(data, *, show_windows=True):
    lines = []
    tokens = data.get("token_usage")
    tokens = tokens if isinstance(tokens, dict) else {}
    for key, label in (("used_tokens", "사용 토큰"), ("lifetime_tokens", "누적 사용 토큰"),
                       ("today_tokens", "오늘 사용 토큰"), ("remaining_tokens", "남은 토큰")):
        value = _count(tokens.get(key))
        if value is not None:
            lines.append(f"{label}: {value:,}")
    if not lines:
        lines.append("사용 토큰: 조회 불가")
    if _count(tokens.get("remaining_tokens")) is None and data.get("quota_applicable") is not False:
        lines.append("남은 토큰 수: 미제공")
    if show_windows:
        for row in quota_rows(data):
            metric = (f"사용 {row['used']:g}% · 남음 {row['remaining']:g}%"
                      if row["used"] is not None else "사용/잔여 비율 미제공")
            lines.append(f"{row['title']}: {metric}")
            if row["reset"] is not None:
                lines.append(f"  초기화: {row['reset']}")
    buckets = data.get("rate_limits")
    buckets = buckets if isinstance(buckets, (list, tuple)) else []
    if data.get("quota_applicable") is False:
        lines.append("계정 사용 한도: 해당 없음 (로컬 실행)")
    elif not buckets:
        lines.append("사용 한도: 제공된 한도 없음" if data.get("rate_limits") == [] else "사용 한도: 조회 불가")
    notes = {"busy": "응답 생성 중 · 최근 조회값입니다. 완료 후 새로고침하세요.",
             "authentication": "ChatGPT 연결 확인 후 조회할 수 있습니다.",
             "unsupported": "제공되지 않는 항목은 조회 불가로 표시합니다."}
    error = data.get("error_code")
    if error:
        error = error if isinstance(error, str) else "unavailable"
        lines.append(notes.get(error, "사용량 조회 실패 · 새로고침해 주세요."))
    if data.get("fetched_at") is not None:
        lines.append(f"최근 확인: {_time(data['fetched_at'])}")
    return "\n".join(lines)
