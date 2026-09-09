"""Low-latency Korean style guardrails for Jarvis/Anis responses.

The taxonomy is an original, compact adaptation inspired by the MIT-licensed
`epoko77-ai/im-not-ai` project.  It deliberately avoids a second LLM call on
ordinary chat so voice response latency remains predictable.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from core.response_integrity import map_narrative


_PROTECTED = re.compile(
    r"```[\s\S]*?```|`[^`\n]+`|https?://\S+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"[A-Za-z]:\\[^\r\n]+|\b\d+(?:[.,:/-]\d+)*(?:\s*(?:%|MB|GB|초|분|시간|원|개))?\b",
    re.IGNORECASE,
)

_SIGNALS = {
    "translationese": (
        re.compile(r"(?:에 있어서|에 의하여|을 통해서|를 통해서)"),
        re.compile(r"(?:되어진|시켜지)"),
    ),
    "stock_phrases": (
        re.compile(r"(?:결론적으로|시사하는 바가|주목할 만한 점은)"),
        re.compile(r"(?:다양한 측면에서|중요한 역할을 한다)"),
    ),
    "mechanical_structure": (
        re.compile(r"(?:첫째|둘째|셋째)[,.:]?"),
        re.compile(r"(?:또한|따라서|한편|나아가)[, ]"),
    ),
    "overqualification": (
        re.compile(r"(?:할 수 있을 것으로 보|일 가능성이 있을 수 있)"),
        re.compile(r"(?:매우|상당히|굉장히){2,}"),
    ),
}


@dataclass(frozen=True)
class NaturalnessReport:
    score: int
    categories: tuple[str, ...]
    sentence_count: int
    repeated_endings: int

    @property
    def route(self) -> str:
        if self.score <= 1:
            return "light"
        if self.score <= 5:
            return "standard"
        return "heavy"


def _mask_protected(text: str) -> tuple[str, list[str]]:
    protected: list[str] = []

    def replace(match):
        protected.append(match.group(0))
        return f"\uFFF0{len(protected) - 1}\uFFF1"

    return _PROTECTED.sub(replace, text), protected


def _restore_protected(text: str, protected: list[str]) -> str:
    for index, value in enumerate(protected):
        text = text.replace(f"\uFFF0{index}\uFFF1", value)
    return text


def analyze_korean_naturalness(text: str) -> NaturalnessReport:
    value, _ = _mask_protected(str(text or ""))
    categories = []
    score = 0
    for category, patterns in _SIGNALS.items():
        hits = sum(len(pattern.findall(value)) for pattern in patterns)
        if hits:
            categories.append(category)
            score += min(3, hits)
    endings = re.findall(r"(?:습니다|어요|예요|했어|할게|이야|거야)(?=[.!?\n]|$)", value)
    repeated = max(0, len(endings) - len(set(endings)))
    score += min(3, repeated)
    sentences = len([part for part in re.split(r"[.!?\n]+", value) if part.strip()])
    return NaturalnessReport(score, tuple(categories), sentences, repeated)


def light_polish_korean(text: str) -> str:
    """Apply only meaning-neutral cleanup; never rewrite facts or code."""
    return map_narrative(str(text or ""), _polish_narrative)


def _polish_narrative(text: str) -> str:
    original = str(text or "")
    value, protected = _mask_protected(original)
    value = re.sub(r"[ \t]+([,.!?])", r"\1", value)
    value = re.sub(r"([!?])\1{2,}", r"\1\1", value)
    value = re.sub(r"(?m)^(또한|그리고|따라서),?\s+\1,?\s+", r"\1, ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = _restore_protected(value, protected)
    # A formatting pass must never silently erase substantive content.
    return value if len(value) >= int(len(original) * .8) else original


def korean_writing_guidance(style: str = "") -> str:
    """Prompt fragment shared by conversation and verified-result realizers."""
    learned = ""
    try:
        from core.style_learning import get_style_learning_store
        learned = get_style_learning_store().effective_directive()
    except Exception:
        learned = ""
    return (
        "자연스러운 한국어로 답하세요. 영어식 번역투, 같은 종결어미의 연속 반복, "
        "기계적인 첫째·둘째 나열, 불필요한 결론 문구와 문두 접속사 남발을 피하세요. "
        "짧게 답할 수 있는 질문은 짧게 답하고, 사용자가 요청하지 않은 배경 설명을 붙이지 마세요. "
        "사실·수치·날짜·고유명사·직접 인용·파일명·코드·도구 결과는 바꾸지 마세요. "
        f"사용자가 정한 말투와 호칭을 가장 우선하세요: {style or '간결하고 자연스러운 한국어'}. "
        + (f"검증된 말투 학습 프로필도 반영하세요: {learned}." if learned else "")
    )
