"""Render verified tool results as natural dialogue without changing their facts."""
from __future__ import annotations

import json
import re
from pathlib import PurePath
from typing import Iterable

from core.agent_services import _apply_requested_style, _sanitize_response


_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_MEASURED = re.compile(
    r"(?:\d{1,4}(?:[./:-]\d{1,4})+(?::\d{1,2})?|"
    r"\d+(?:\.\d+)?\s*(?:°C|도|퍼센트|%|초|분|시간|개|건))",
    re.IGNORECASE,
)
_QUOTED = re.compile(r"['\"]([^'\"\r\n]{1,120})['\"]")
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\r\n()]+|/(?:[^\s/]+/)*[^\s()]+)")
_FAILURE = re.compile(r"(?:실패|오류|못했|완료하지 못|실행하지 못)")


def protected_facts(canonical: str) -> list[str]:
    """Extract values that a conversational rewrite must preserve exactly."""
    value = str(canonical or "")
    facts: list[str] = []
    facts.extend(match.group(0) for match in _EMAIL.finditer(value))
    facts.extend(match.group(0).strip() for match in _MEASURED.finditer(value))
    facts.extend(match.group(1).strip() for match in _QUOTED.finditer(value))
    for match in _PATH.finditer(value):
        raw = match.group(0).rstrip(".,!?;:。")
        name = PurePath(raw.replace("\\", "/")).name
        if "." in name:
            facts.append(name)
    return list(dict.fromkeys(fact for fact in facts if fact))


class ResponseRealizer:
    """Use an LLM only as a wording layer over an already verified result."""

    EXCLUDED_TOOLS = {
        "repeat_text", "speak_text", "listen", "browser_learning_status",
        "browser_learn_video_preference",
    }

    def __init__(self, llm):
        self.llm = llm

    def realize(
        self,
        canonical: str,
        *,
        tool_name: str,
        user_request: str,
        assistant_name: str,
        address: str,
        style: str = "",
        required_facts: Iterable[str] = (),
    ) -> str:
        fallback = str(canonical or "").strip()
        if not fallback or tool_name in self.EXCLUDED_TOOLS or self.llm is None:
            return fallback

        facts = list(dict.fromkeys([*protected_facts(fallback), *required_facts]))
        payload = {
            "tool": tool_name,
            "user_request": user_request,
            "verified_result": fallback,
            "must_preserve_exactly": facts,
        }
        system = (
            f"당신은 {assistant_name}의 응답 표현기입니다. 도구 실행이나 판단을 다시 하지 말고, "
            "검증 완료된 결과를 자연스러운 한국어 1~2문장으로만 표현하세요. "
            "매번 같은 고정 문장을 반복하지 말되 성공/실패 상태와 의미를 바꾸지 마세요. "
            "must_preserve_exactly의 값은 철자와 숫자를 그대로 포함하고 새 사실을 만들지 마세요. "
            "사용자가 상세 경로나 PID를 요구하지 않았다면 긴 절대 경로와 내부 식별자는 생략하세요. "
            f"사용자 호칭은 '{address}'이고 필요할 때만 최대 한 번 사용하세요. "
            f"응답 말투 설정: {style or '간결하고 자연스러운 존댓말'}. "
            "설명, 머리말, JSON, 역할 표시는 출력하지 마세요."
        )
        try:
            candidate = self.llm.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ])
            candidate = _sanitize_response(candidate, user_request)
            candidate = _apply_requested_style(candidate, style).strip()
        except Exception:
            return fallback
        return candidate if self._valid(candidate, fallback, facts) else fallback

    @staticmethod
    def _valid(candidate: str, canonical: str, facts: Iterable[str]) -> bool:
        if not candidate or _FAILURE.search(candidate):
            return False
        if len(candidate) > max(240, len(canonical) * 3):
            return False
        normalized = re.sub(r"\s+", "", candidate).casefold()
        return all(re.sub(r"\s+", "", fact).casefold() in normalized for fact in facts)
