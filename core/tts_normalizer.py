"""Convert display text into natural Korean-oriented TTS input."""
import re

try:
    from g2pk import G2p
    from g2pk.english import convert_eng
except ImportError:
    G2p = None
    convert_eng = None


_G2P = None
_DIGITS = "영일이삼사오육칠팔구"
_SMALL_UNITS = ("", "십", "백", "천")
_LARGE_UNITS = ("", "만", "억", "조", "경")


def _sino_number(value: str) -> str:
    value = value.lstrip("0") or "0"
    if value == "0":
        return "영"
    groups = []
    while value:
        groups.append(value[-4:])
        value = value[:-4]
    spoken = []
    for group_index, group in reversed(list(enumerate(groups))):
        part = []
        padded = group.zfill(4)
        for index, char in enumerate(padded):
            digit = int(char)
            if not digit:
                continue
            unit_index = 3 - index
            if digit != 1 or unit_index == 0:
                part.append(_DIGITS[digit])
            part.append(_SMALL_UNITS[unit_index])
        if part:
            spoken.extend(part)
            if group_index < len(_LARGE_UNITS):
                spoken.append(_LARGE_UNITS[group_index])
    return "".join(spoken)


def _clock(match: re.Match) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    period = "오전" if hour < 12 else "오후"
    spoken_hour = hour % 12 or 12
    native_hours = {
        1: "한", 2: "두", 3: "세", 4: "네", 5: "다섯", 6: "여섯",
        7: "일곱", 8: "여덟", 9: "아홉", 10: "열", 11: "열한", 12: "열두",
    }
    minute_text = "" if minute == 0 else f" {_sino_number(str(minute))} 분"
    return f"{period} {native_hours[spoken_hour]} 시{minute_text}"


def _english_to_hangul(match: re.Match) -> str:
    global _G2P
    token = match.group(0)
    if convert_eng is None or G2p is None:
        return token
    if _G2P is None:
        _G2P = G2p()
    return convert_eng(token, _G2P.cmu)


def normalize_for_tts(text: str) -> str:
    """Normalize clocks, units, numbers and English words without changing UI text."""
    value = str(text or "")
    value = re.sub(
        r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?!\d)",
        _clock,
        value,
    )
    value = re.sub(
        r"(-?\d+)\.(\d+)\s*(?:°\s*)?[cC]\b",
        lambda m: f"{'마이너스 ' if m.group(1).startswith('-') else ''}"
                  f"{_sino_number(m.group(1).lstrip('-'))} 점 "
                  f"{' '.join(_DIGITS[int(ch)] for ch in m.group(2))} 도",
        value,
    )
    value = re.sub(
        r"(-?\d+(?:\.\d+)?)\s*%",
        lambda m: (
            ("마이너스 " if m.group(1).startswith("-") else "")
            + " 점 ".join(
                _sino_number(part) if index == 0
                else " ".join(_DIGITS[int(ch)] for ch in part)
                for index, part in enumerate(m.group(1).lstrip("-").split("."))
            )
            + " 퍼센트"
        ),
        value,
    )
    value = re.sub(
        r"(?<![\d.])(-?\d+)\.(\d+)(?![\d.])",
        lambda m: f"{'마이너스 ' if m.group(1).startswith('-') else ''}"
                  f"{_sino_number(m.group(1).lstrip('-'))} 점 "
                  f"{' '.join(_DIGITS[int(ch)] for ch in m.group(2))}",
        value,
    )
    value = re.sub(r"\b[A-Za-z][A-Za-z'-]*\b", _english_to_hangul, value)
    value = re.sub(r"(?<!\d)\d+(?!\d)", lambda m: _sino_number(m.group(0)), value)
    return re.sub(r"\s+", " ", value).strip()
