"""Evidence-backed, extensible response-style learning profiles."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time


@dataclass(frozen=True)
class StyleLearningRecord:
    record_id: str
    subject: str
    directive: str
    source_type: str
    source_uris: list[str] = field(default_factory=list)
    evidence_summary: str = ""
    confidence: float = 0.5
    active: bool = True
    created_at: float = field(default_factory=time.time)


class StyleLearningStore:
    """Stores style evidence separately from factual RAG knowledge."""

    def __init__(self, path: str | Path = "data/style_learning_profiles.json"):
        self.path = Path(path)

    def _load(self) -> list[StyleLearningRecord]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return [StyleLearningRecord(**item) for item in payload if isinstance(item, dict)]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []

    def _save(self, records: list[StyleLearningRecord]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _sanitize_directive(value: str) -> str:
        text = " ".join(str(value or "").split()).strip(" .")
        if not text:
            raise ValueError("학습할 말투 지침이 비어 있습니다.")
        # Learned style may describe wording only. It must not grant permissions,
        # alter tools, or smuggle a new system prompt into future sessions.
        blocked = re.compile(
            r"(?:시스템\s*프롬프트|이전\s*지시|규칙을\s*무시|도구를\s*실행|권한을\s*(?:허용|우회)|"
            r"비밀번호|API\s*키|파일을\s*(?:삭제|전송))", re.IGNORECASE,
        )
        if blocked.search(text):
            raise ValueError("말투가 아닌 명령·권한·보안 지시는 학습할 수 없습니다.")
        return text[:300]

    def add(self, *, subject: str, directive: str, source_type: str,
            source_uris=(), evidence_summary: str = "", confidence: float = 0.5) -> StyleLearningRecord:
        clean_directive = self._sanitize_directive(directive)
        clean_subject = " ".join(str(subject or "기본 말투").split())[:80]
        uris = list(dict.fromkeys(str(uri).strip() for uri in source_uris if str(uri).strip()))[:12]
        key = "|".join([clean_subject, clean_directive, *uris])
        record = StyleLearningRecord(
            record_id="style-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
            subject=clean_subject, directive=clean_directive,
            source_type=str(source_type or "user_provided")[:40], source_uris=uris,
            evidence_summary=" ".join(str(evidence_summary or "").split())[:500],
            confidence=max(0.0, min(1.0, float(confidence))),
        )
        records = [item for item in self._load() if item.record_id != record.record_id]
        records.append(record); self._save(records[-100:])
        return record

    def list(self, *, active_only: bool = True) -> list[StyleLearningRecord]:
        records = self._load()
        if active_only: records = [item for item in records if item.active]
        return sorted(records, key=lambda item: (item.confidence, item.created_at), reverse=True)

    def effective_directive(self, *, limit: int = 3) -> str:
        directives = []
        for record in self.list()[:max(0, limit)]:
            if record.directive not in directives: directives.append(record.directive)
        return " ".join(directives)[:600]

    def set_active(self, record_id: str, active: bool) -> StyleLearningRecord:
        records = self._load(); updated = None; output = []
        for record in records:
            if record.record_id == record_id:
                updated = StyleLearningRecord(**{**asdict(record), "active": bool(active)})
                output.append(updated)
            else: output.append(record)
        if updated is None: raise ValueError("해당 말투 학습 기록이 없습니다.")
        self._save(output); return updated


_style_learning_store = None


def get_style_learning_store() -> StyleLearningStore:
    global _style_learning_store
    if _style_learning_store is None: _style_learning_store = StyleLearningStore()
    return _style_learning_store
