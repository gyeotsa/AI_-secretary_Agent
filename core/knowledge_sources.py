"""Keep Anis implementation files separate from the user's accumulated knowledge."""
from __future__ import annotations

import sys
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from config import Config


def is_internal_path(value: str | Path) -> bool:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    if not text:
        return False
    try:
        if text.casefold().startswith("file://"):
            uri = urlsplit(text)
            text = unquote(uri.path)
            if uri.netloc:
                text = f"//{uri.netloc}{text}"
            elif len(text) > 2 and text[0] == "/" and text[2] == ":":
                text = text[1:]
        elif "://" in text:
            return False
        candidate = Path(text).expanduser()
        if not candidate.is_absolute() and not any(mark in text for mark in ("/", "\\")) and not candidate.exists():
            return False
        path = candidate.resolve()
        roots = {Path(__file__).resolve().parent.parent}
        if getattr(sys, "frozen", False):
            roots.add(Path(sys.executable).resolve().parent)
        data = Path(Config.DB_PATH).resolve().parent
        if path.is_relative_to(data):
            parts = path.relative_to(data).parts
            if parts and parts[0].casefold() in {
                    "models", "logs", "diagnostics", "acceptance", "qa", "cache", "tools",
                    "browser_profiles", "oauth_tokens"}:
                return True
        # These runtime folders contain user knowledge/results, also in source runs.
        if any(path.is_relative_to(data / folder) for folder in (
                "AnisKnowledgeVault", "research_reports", "mockup_outputs", "mockup_styles")):
            return False
        return any(path.is_relative_to(root) for root in roots)
    except (OSError, ValueError):
        return True


def is_internal_knowledge(source: str = "", metadata: dict | None = None) -> bool:
    if is_internal_path(source):
        return True
    for key, value in (metadata or {}).items():
        if key in {"source_type", "knowledge_origin"} and str(value).casefold() in {
                "anis_development", "development", "diagnostic", "diagnostics", "qa", "test_fixture"}:
            return True
        if key in {"source", "source_uri", "source_path", "file_path", "path", "project_root",
                   "workspace_path", "coding_workspace_path", "vault"} and isinstance(value, str):
            if is_internal_path(value):
                return True
        if isinstance(value, dict) and is_internal_knowledge(metadata=value):
            return True
        if isinstance(value, (list, tuple)) and any(
                is_internal_knowledge(metadata=item) for item in value if isinstance(item, dict)):
            return True
    details = metadata or {}
    nested = details.get("metadata")
    source_type = details.get("source_type") or (nested.get("source_type") if isinstance(nested, dict) else None)
    if source_type == "obsidian":
        # Old vault sync kept provenance only in the note's embedded frontmatter.
        chunks = details.get("chunks", [])
        texts = [details.get("content", ""), *(chunks if isinstance(chunks, (list, tuple)) else [])]
        headers = [text.split("---", 2)[1] for text in texts
                   if isinstance(text, str) and text.startswith("---") and len(text.split("---", 2)) == 3]
        source_text = str(source or "")
        if not headers and source_text and "://" not in source_text:
            try:
                with Path(source_text).open(encoding="utf-8") as handle:
                    text = handle.read(65536)
                if text.startswith("---") and len(text.split("---", 2)) == 3:
                    headers.append(text.split("---", 2)[1])
            except (OSError, ValueError):
                pass
        for header in headers:
            provenance = {}
            for line in header.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                try:
                    provenance[key.strip()] = json.loads(value.strip())
                except ValueError:
                    provenance[key.strip()] = value.strip().strip("'")
            if is_internal_knowledge(metadata=provenance):
                return True
    return False
