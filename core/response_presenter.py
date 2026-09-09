"""내부 도구 결과를 화면·음성에 적합한 사용자 응답으로 변환한다."""

import re
from dataclasses import dataclass
from core.response_integrity import map_narrative, protected_segments


_DETAIL_REQUEST_TERMS = (
    "상세", "자세히", "세부", "원문", "로그", "경로", "위치",
    "pid", "프로세스 id", "프로세스 아이디", "실행 파일",
)

_PROGRAM_LAUNCH_RESULT = re.compile(
    r"^(?:프로그램 실행 성공|Windows 시작 메뉴 앱 실행 요청 성공):"
    r"\s*.+?(?:\s*\(PID:\s*\d+\))?\s*$",
    re.IGNORECASE,
)
_CJK_OR_KANA = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_NON_KOREAN_REQUEST_TERMS = ("중국어", "일본어", "한자", "번역", "원문")
_INTERNAL_PENDING_ID = re.compile(r"^대기\s*작업\s*ID\s*:\s*[0-9a-f]+$", re.IGNORECASE)


@dataclass(frozen=True)
class PresentedResponse:
    technical_text: str
    screen_text: str
    speech_text: str


def requests_technical_details(user_request: str) -> bool:
    normalized = (user_request or "").strip().casefold()
    return any(term in normalized for term in _DETAIL_REQUEST_TERMS)


def present_response(response_text: str, user_request: str = "") -> str:
    """기술 세부정보를 기본 응답에서 감추되 명시적으로 요청하면 보존한다."""
    text = (response_text or "").strip()
    if not text or requests_technical_details(user_request):
        return text

    allow_cjk = any(term in (user_request or "").casefold() for term in _NON_KOREAN_REQUEST_TERMS)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if _INTERNAL_PENDING_ID.fullmatch(stripped):
            continue
        if not allow_cjk and _CJK_OR_KANA.search(stripped):
            stripped = _CJK_OR_KANA.split(stripped, maxsplit=1)[0].rstrip(" :：,，")
            completed_sentence = max(
                stripped.rfind("."), stripped.rfind("!"), stripped.rfind("?")
            )
            if completed_sentence >= 0:
                stripped = stripped[:completed_sentence + 1]
            if not stripped:
                continue
        if _PROGRAM_LAUNCH_RESULT.fullmatch(stripped):
            lines.append("프로그램을 실행했습니다, 보스.")
            continue
        # 다른 결과 문장에 PID만 부가된 경우에도 대화에서는 제거한다.
        stripped = re.sub(r"\s*\(PID:\s*\d+\)", "", stripped, flags=re.IGNORECASE)
        lines.append(stripped)
    return "\n".join(lines).strip()


def present_channels(response_text: str, user_request: str = "") -> PresentedResponse:
    """원문 기술 로그와 화면/TTS용 본문을 명시적으로 분리한다."""
    technical = (response_text or "").strip()
    # Source-bearing output is already presentation content, not a technical log.
    # Do not line-strip code or remove foreign-language text inside quotations.
    source_output = bool(protected_segments(technical)) and not _PROGRAM_LAUNCH_RESULT.fullmatch(technical)
    screen = technical if source_output else present_response(technical, user_request)
    speech = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~", "코드는 화면에서 확인할 수 있어요.", screen)
    return PresentedResponse(
        technical_text=technical,
        screen_text=screen,
        speech_text=speech,
    )
