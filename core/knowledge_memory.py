"""Typed long-term memory contract for facts, preferences, projects and cases."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from contextlib import contextmanager

from config import Config


class MemoryKind(str, Enum):
    CONVERSATION = "conversation"
    TASK = "task"
    PROJECT = "project"
    PREFERENCE = "preference"
    FACT = "fact"
    CASE = "case"


class EpistemicStatus(str, Enum):
    FACT = "fact"
    INFERENCE = "inference"
    USER_CLAIM = "user_claim"


class MemoryPolicyError(ValueError):
    pass


class FreshnessPolicy:
    WEB_TTL_SECONDS = {
        "weather": 30 * 60,
        "traffic": 15 * 60,
        "finance": 15 * 60,
        "news": 6 * 60 * 60,
        "software": 24 * 60 * 60,
        "general": 24 * 60 * 60,
    }

    @classmethod
    def expires_at(cls, source_type: str, topic: str = "general", recorded_at: Optional[float] = None):
        if source_type != "web": return None
        recorded_at = recorded_at or time.time()
        return recorded_at + cls.WEB_TTL_SECONDS.get(topic, cls.WEB_TTL_SECONDS["general"])


@dataclass
class KnowledgeRecord:
    content: str
    kind: MemoryKind
    subject: str
    predicate: str = "describes"
    epistemic_status: EpistemicStatus = EpistemicStatus.USER_CLAIM
    source_uri: str = ""
    source_label: str = ""
    workspace_namespace: str = "global"
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    recorded_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    supersedes_id: str = ""
    status: str = "active"

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["kind"] = self.kind.value
        result["epistemic_status"] = self.epistemic_status.value
        return result


class SensitiveMemoryPolicy:
    """Blocks automatic persistence of credentials and high-risk personal identifiers."""

    KEY_PATTERNS = re.compile(
        r"(?i)(password|passwd|비밀번호|api[ _-]?key|secret|access[ _-]?token|refresh[ _-]?token|"
        r"주민등록번호|계좌번호|카드번호|cvv|인증번호|otp|private[ _-]?key)"
    )
    VALUE_PATTERNS = [
        re.compile(r"\b\d{6}-?[1-4]\d{6}\b"),
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"(?i)\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}\b"),
    ]

    @classmethod
    def detect(cls, content: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        serialized = content + " " + json.dumps(metadata or {}, ensure_ascii=False)
        reasons = []
        if cls.KEY_PATTERNS.search(serialized): reasons.append("민감한 자격증명 또는 개인식별자 키워드")
        if any(pattern.search(serialized) for pattern in cls.VALUE_PATTERNS): reasons.append("민감정보 형식")
        return reasons


class KnowledgeMemoryStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or Path(Config.DB_PATH).with_name("knowledge_memory.db"))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _session(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._session() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_records (
                    record_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    predicate TEXT NOT NULL, content TEXT NOT NULL, epistemic_status TEXT NOT NULL,
                    source_uri TEXT NOT NULL DEFAULT '', source_label TEXT NOT NULL DEFAULT '',
                    workspace_namespace TEXT NOT NULL DEFAULT 'global', confidence REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}', recorded_at REAL NOT NULL,
                    expires_at REAL, supersedes_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_subject
                    ON knowledge_records(workspace_namespace, subject, predicate, status);
                CREATE INDEX IF NOT EXISTS idx_knowledge_kind
                    ON knowledge_records(workspace_namespace, kind, status);
            """)

    def remember(self, record: KnowledgeRecord, *, automatic: bool = True) -> str:
        if not record.content.strip() or not record.subject.strip():
            raise MemoryPolicyError("메모리의 subject와 content는 비어 있을 수 없습니다.")
        if automatic:
            reasons = SensitiveMemoryPolicy.detect(record.content, record.metadata)
            if reasons:
                raise MemoryPolicyError("민감 정보는 자동으로 기억하지 않습니다: " + ", ".join(reasons))
        record.confidence = max(0.0, min(float(record.confidence), 1.0))
        if record.epistemic_status in {EpistemicStatus.FACT, EpistemicStatus.INFERENCE} and not (record.source_uri or record.source_label):
            raise MemoryPolicyError("사실·추측 메모리에는 출처가 필요합니다.")
        with self._session() as conn:
            conflicts = conn.execute("""
                SELECT record_id, metadata FROM knowledge_records
                WHERE workspace_namespace=? AND subject=? AND predicate=?
                  AND status='active' AND content<>?
            """, (record.workspace_namespace, record.subject, record.predicate, record.content)).fetchall()
            conflict_ids = [row["record_id"] for row in conflicts]
            if conflict_ids:
                record.metadata = {**record.metadata, "conflicts_with": conflict_ids}
                for row in conflicts:
                    previous_metadata = json.loads(row["metadata"] or "{}")
                    previous_metadata["conflicts_with"] = list(dict.fromkeys(
                        [*previous_metadata.get("conflicts_with", []), record.record_id]
                    ))
                    conn.execute("UPDATE knowledge_records SET metadata=? WHERE record_id=?",
                                 (json.dumps(previous_metadata, ensure_ascii=False), row["record_id"]))
            conn.execute("""
                INSERT INTO knowledge_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                record.record_id, record.kind.value, record.subject, record.predicate,
                record.content, record.epistemic_status.value, record.source_uri,
                record.source_label, record.workspace_namespace, record.confidence,
                json.dumps(record.metadata, ensure_ascii=False), record.recorded_at,
                record.expires_at, record.supersedes_id, record.status,
            ))
        return record.record_id

    def correct(self, *, subject: str, predicate: str, content: str,
                workspace_namespace: str = "global", source_label: str = "사용자 정정") -> str:
        with self._session() as conn:
            previous = conn.execute("""
                SELECT record_id FROM knowledge_records
                WHERE workspace_namespace=? AND subject=? AND predicate=? AND status='active'
                ORDER BY recorded_at DESC
            """, (workspace_namespace, subject, predicate)).fetchall()
            previous_ids = [row["record_id"] for row in previous]
            if previous_ids:
                placeholders = ",".join("?" for _ in previous_ids)
                conn.execute(f"UPDATE knowledge_records SET status='superseded' WHERE record_id IN ({placeholders})", previous_ids)
        record = KnowledgeRecord(
            content=content, kind=MemoryKind.FACT, subject=subject, predicate=predicate,
            epistemic_status=EpistemicStatus.USER_CLAIM, source_label=source_label,
            workspace_namespace=workspace_namespace,
            supersedes_id=previous_ids[0] if previous_ids else "",
            metadata={"correction": True, "superseded_ids": previous_ids},
        )
        return self.remember(record, automatic=True)

    def search(self, query: str = "", *, kinds: Optional[Iterable[MemoryKind | str]] = None,
               workspace_namespace: str = "global", metadata_filter: Optional[Dict[str, Any]] = None,
               include_expired: bool = False, limit: int = 10) -> List[KnowledgeRecord]:
        clauses, args = ["workspace_namespace=?", "status='active'"], [workspace_namespace]
        if query:
            clauses.append("(subject LIKE ? OR predicate LIKE ? OR content LIKE ?)")
            args.extend([f"%{query}%"] * 3)
        if kinds:
            values = [item.value if isinstance(item, MemoryKind) else str(item) for item in kinds]
            clauses.append("kind IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at>?)")
            args.append(time.time())
        with self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_records WHERE " + " AND ".join(clauses) + " ORDER BY recorded_at DESC",
                args,
            ).fetchall()
        records = [self._from_row(row) for row in rows]
        if metadata_filter:
            records = [r for r in records if all(r.metadata.get(k) == v for k, v in metadata_filter.items())]
        tokens = set(re.findall(r"\w+", query.casefold()))
        records.sort(key=lambda r: (
            len(tokens & set(re.findall(r"\w+", f"{r.subject} {r.predicate} {r.content}".casefold()))),
            r.confidence, r.recorded_at,
        ), reverse=True)
        return records[:max(0, limit)]

    def get(self, record_id: str) -> Optional[KnowledgeRecord]:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM knowledge_records WHERE record_id=?", (record_id,)).fetchone()
        return self._from_row(row) if row else None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> KnowledgeRecord:
        return KnowledgeRecord(
            record_id=row["record_id"], kind=MemoryKind(row["kind"]), subject=row["subject"],
            predicate=row["predicate"], content=row["content"],
            epistemic_status=EpistemicStatus(row["epistemic_status"]), source_uri=row["source_uri"],
            source_label=row["source_label"], workspace_namespace=row["workspace_namespace"],
            confidence=row["confidence"], metadata=json.loads(row["metadata"] or "{}"),
            recorded_at=row["recorded_at"], expires_at=row["expires_at"],
            supersedes_id=row["supersedes_id"], status=row["status"],
        )


_store: Optional[KnowledgeMemoryStore] = None


def get_knowledge_memory() -> KnowledgeMemoryStore:
    global _store
    if _store is None: _store = KnowledgeMemoryStore()
    return _store
