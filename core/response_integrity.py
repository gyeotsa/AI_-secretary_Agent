"""Keep source material out of stylistic rewrites and presentation filters."""
from __future__ import annotations

import re
from typing import Callable

# Protect whole code blocks first, then quoted payloads and source references.
# These are lexical boundaries, not instructions extracted from their contents.
PROTECTED = re.compile(
    r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|`[^`\n]+`|"
    r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)\x27[^\x27\n]+\x27(?!\w)|'
    r"https?://[^\s<>]+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"[A-Za-z]:[\\/][^\r\n]+|"
    r"(?m:^[ \t]*(?:내용|본문|원문|대상|수신자|파일명)\s*[:：][^\r\n]*)|"
    r"(?m:^[ \t]*(?:[A-Za-z_]\w*\s*=|def\s+|class\s+|import\s+|from\s+|\{|\[).*$)",
    re.IGNORECASE,
)


def protected_segments(text: str) -> tuple[str, ...]:
    return tuple(m.group(0) for m in PROTECTED.finditer(str(text or "")))


def map_narrative(text: str, transform: Callable[[str], str]) -> str:
    """Transform prose only. Rejoin untouched substrings without placeholders."""
    value = str(text or "")
    pieces: list[str] = []
    offset = 0
    for match in PROTECTED.finditer(value):
        pieces.append(transform(value[offset:match.start()]))
        pieces.append(match.group(0))
        offset = match.end()
    pieces.append(transform(value[offset:]))
    return "".join(pieces)


def preserves_sources(original: str, candidate: str) -> bool:
    """Every protected occurrence must survive, in order and byte-for-byte."""
    position = 0
    for segment in protected_segments(original):
        found = candidate.find(segment, position)
        if found < 0:
            return False
        position = found + len(segment)
    return True
