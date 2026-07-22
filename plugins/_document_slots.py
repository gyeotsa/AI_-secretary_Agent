"""Shared document path/title normalization for file-producing plugins."""
from pathlib import Path
from typing import Any, Dict
import re


def extract_document_slots(text: str, current: Dict[str, Any], extension: str,
                           default_title: str) -> Dict[str, Any]:
    slots = dict(current)
    title_match = re.search(r"([^\s]+?)(?:이라는|라는)\s*이름", text)
    if title_match:
        slots["title"] = title_match.group(1)
    explicit = re.search(rf"([A-Za-z]:\\[^\r\n]+?\{extension}|/[^\r\n]+?\{extension})", text, re.I)
    if explicit:
        slots["path"] = explicit.group(1)
    elif "바탕화면" in text:
        title = str(slots.get("title") or default_title)
        safe = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "_", title).strip("_") or default_title
        slots["path"] = str(Path.home() / "Desktop" / f"{safe}{extension}")
    return slots
