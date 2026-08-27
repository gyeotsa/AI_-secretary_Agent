"""Shared document path/title normalization for file-producing plugins."""
from pathlib import Path
from typing import Any, Dict
import re


_LABEL_ALIASES = {
    "title": ("제목",),
    "paragraphs": ("본문", "내용", "문구"),
}


def _labelled_values(text: str, labels: tuple[str, ...]) -> list[str]:
    """Extract natural-language document values without requiring one quote style."""
    label = "|".join(re.escape(item) for item in labels)
    marker = rf"(?:{label})\s*(?:은|는|을|를|[:：=])\s*"
    values: list[str] = []
    quoted_patterns = (
        marker + r'"([^"\r\n]+)"',
        marker + r"'([^'\r\n]+)'",
        marker + r"“([^”\r\n]+)”",
        marker + r"‘([^’\r\n]+)’",
    )
    for pattern in quoted_patterns:
        values.extend(
            value.strip() for value in re.findall(pattern, text, re.IGNORECASE)
            if value.strip()
        )
    if values:
        return list(dict.fromkeys(values))

    # Unquoted values are accepted up to a sentence boundary or another labelled
    # field. Common trailing creation verbs are removed, while the user payload
    # itself remains untouched.
    boundary = rf"(?=(?:[,;\n]|\s+(?:제목|본문|내용|문구)\s*(?:은|는|[:：=]))|$)"
    for value in re.findall(marker + r"(.+?)" + boundary, text, re.IGNORECASE):
        cleaned = re.sub(
            r"\s*(?:(?:으로|라고|이라고|라는)\s*)?"
            r"(?:작성|넣어|추가|써|적어|만들어|생성).*$",
            "", value, flags=re.IGNORECASE,
        ).strip(" \t.,")
        if cleaned:
            values.append(cleaned)
    return list(dict.fromkeys(values))


def extract_document_slots(text: str, current: Dict[str, Any], extension: str,
                           default_title: str) -> Dict[str, Any]:
    slots = dict(current)
    title_match = re.search(r"([^\s]+?)(?:이라는|라는)\s*이름", text)
    if title_match:
        slots["title"] = title_match.group(1)
    labelled_title = _labelled_values(text, _LABEL_ALIASES["title"])
    if labelled_title:
        slots["title"] = labelled_title[-1]
    paragraphs = _labelled_values(text, _LABEL_ALIASES["paragraphs"])
    if paragraphs and extension.casefold() in {".docx", ".hwpx", ".pdf"}:
        slots["paragraphs"] = paragraphs
    explicit = re.search(rf"([A-Za-z]:\\[^\r\n]+?\{extension}|/[^\r\n]+?\{extension})", text, re.I)
    if explicit:
        slots["path"] = explicit.group(1)
    elif "바탕화면" in text:
        title = str(slots.get("title") or default_title)
        safe = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "_", title).strip("_") or default_title
        slots["path"] = str(Path.home() / "Desktop" / f"{safe}{extension}")
    return slots
