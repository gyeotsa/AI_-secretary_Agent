"""Privacy-aware trajectories, feedback, evaluation cases and training exports.

The runtime deliberately learns *records* first.  It never mutates prompts, skills or
model weights from an unreviewed conversation.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import json
import sqlite3
import time
import uuid

from config import Config
from core.productization import SensitiveDataRedactor


_active_trajectory: ContextVar[str] = ContextVar("jarvis_trajectory", default="")


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    category: str
    prompt: str
    expected: Dict[str, Any]
    tags: tuple[str, ...] = ()


class LearningRuntime:
    """Durable evidence store used by evals and optional offline post-training."""

    def __init__(self, db_path: str | None = None):
        configured = Path(db_path or Config.LEARNING_DB_PATH)
        configured.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = configured
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trajectories (
                    trajectory_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    workspace TEXT NOT NULL, user_input TEXT NOT NULL,
                    status TEXT NOT NULL, response TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL, finished_at REAL,
                    duration_ms REAL NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS trajectory_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trajectory_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id TEXT PRIMARY KEY, trajectory_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '', rating INTEGER,
                    correction TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluation_cases (
                    case_id TEXT PRIMARY KEY, category TEXT NOT NULL, prompt TEXT NOT NULL,
                    expected TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS skill_candidates (
                    candidate_id TEXT PRIMARY KEY, failure_signature TEXT NOT NULL,
                    proposal TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, reviewed_at REAL
                );
            """)

    @staticmethod
    def _safe(value: Any) -> Any:
        return SensitiveDataRedactor.redact(value)

    def begin(self, user_input: str, *, session_id: str = "default",
              workspace: str = "global", metadata: Optional[Dict[str, Any]] = None) -> str:
        trajectory_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO trajectories VALUES (?, ?, ?, ?, 'running', '', ?, NULL, 0, ?)",
                (trajectory_id, session_id or "default", workspace or "global",
                 str(self._safe(user_input)), now,
                 json.dumps(self._safe(metadata or {}), ensure_ascii=False)),
            )
        _active_trajectory.set(trajectory_id)
        return trajectory_id

    def event(self, event_type: str, payload: Dict[str, Any], trajectory_id: str = "") -> None:
        trajectory_id = trajectory_id or _active_trajectory.get()
        if not trajectory_id:
            return
        with self._connect() as db:
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM trajectory_events WHERE trajectory_id = ?",
                (trajectory_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO trajectory_events(trajectory_id, sequence, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (trajectory_id, sequence, event_type,
                 json.dumps(self._safe(payload), ensure_ascii=False, default=str), time.time()),
            )

    def finish(self, trajectory_id: str, *, status: str, response: str,
               metadata: Optional[Dict[str, Any]] = None) -> None:
        now = time.time()
        with self._connect() as db:
            row = db.execute("SELECT started_at, metadata FROM trajectories WHERE trajectory_id = ?", (trajectory_id,)).fetchone()
            if not row:
                return
            combined = json.loads(row["metadata"] or "{}")
            combined.update(self._safe(metadata or {}))
            db.execute(
                "UPDATE trajectories SET status=?, response=?, finished_at=?, duration_ms=?, metadata=? WHERE trajectory_id=?",
                (status, str(self._safe(response)), now, (now - row["started_at"]) * 1000,
                 json.dumps(combined, ensure_ascii=False, default=str), trajectory_id),
            )

    def add_feedback(self, *, trajectory_id: str = "", session_id: str = "",
                     rating: int | None = None, correction: str = "", reason: str = "") -> str:
        if rating is not None and rating not in {-1, 0, 1}:
            raise ValueError("rating은 -1, 0, 1 중 하나여야 합니다.")
        if not trajectory_id:
            with self._connect() as db:
                if session_id:
                    row = db.execute(
                        "SELECT trajectory_id FROM trajectories WHERE session_id=? AND status<>'running' ORDER BY finished_at DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                else:
                    row = db.execute(
                        "SELECT trajectory_id FROM trajectories WHERE status<>'running' ORDER BY finished_at DESC LIMIT 1"
                    ).fetchone()
            trajectory_id = row[0] if row else ""
        feedback_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?)", (
                feedback_id, trajectory_id, session_id, rating,
                str(self._safe(correction)), str(self._safe(reason)), time.time(),
            ))
        return feedback_id

    def upsert_case(self, case: EvaluationCase) -> None:
        with self._connect() as db:
            db.execute("""
                INSERT INTO evaluation_cases(case_id, category, prompt, expected, tags, enabled)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(case_id) DO UPDATE SET category=excluded.category, prompt=excluded.prompt,
                    expected=excluded.expected, tags=excluded.tags
            """, (case.case_id, case.category, case.prompt,
                  json.dumps(case.expected, ensure_ascii=False), json.dumps(case.tags, ensure_ascii=False)))

    def list_cases(self, category: str = "") -> list[EvaluationCase]:
        query, params = "SELECT * FROM evaluation_cases WHERE enabled=1", []
        if category:
            query += " AND category=?"; params.append(category)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [EvaluationCase(row["case_id"], row["category"], row["prompt"],
                               json.loads(row["expected"]), tuple(json.loads(row["tags"]))) for row in rows]

    def propose_skill(self, failure_signature: str, proposal: str, evidence: Iterable[str]) -> str:
        candidate_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO skill_candidates VALUES (?, ?, ?, ?, 'quarantined', ?, NULL)", (
                candidate_id, failure_signature, proposal,
                json.dumps(list(evidence), ensure_ascii=False), time.time(),
            ))
        return candidate_id

    def review_skill(self, candidate_id: str, approved: bool) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE skill_candidates SET status=?, reviewed_at=? WHERE candidate_id=? AND status='quarantined'",
                ("approved" if approved else "rejected", time.time(), candidate_id),
            )
        return cursor.rowcount == 1

    def export_training_data(self, output_dir: str | Path) -> Dict[str, int]:
        """Export reviewable SFT/DPO JSONL. This never starts model training."""
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            rows = db.execute("""
                SELECT t.*, f.rating, f.correction, f.reason FROM trajectories t
                LEFT JOIN feedback f ON f.trajectory_id=t.trajectory_id
                WHERE t.status='completed' AND t.response<>''
            """).fetchall()
        sft, dpo, verifier_rl = [], [], []
        for row in rows:
            base = {"messages": [
                {"role": "user", "content": row["user_input"]},
                {"role": "assistant", "content": row["response"]},
            ], "metadata": {"trajectory_id": row["trajectory_id"], "workspace": row["workspace"]}}
            if row["rating"] == 1:
                sft.append(base)
            if row["correction"]:
                dpo.append({"prompt": row["user_input"], "chosen": row["correction"],
                            "rejected": row["response"], "reason": row["reason"]})
            with self._connect() as db:
                events = db.execute(
                    "SELECT event_type,payload FROM trajectory_events WHERE trajectory_id=? ORDER BY sequence",
                    (row["trajectory_id"],),
                ).fetchall()
            tool_events = [{"event_type": item[0], "payload": json.loads(item[1])} for item in events
                           if item[0].startswith("tool_")]
            if tool_events:
                verifier_rl.append({
                    "trajectory_id": row["trajectory_id"], "prompt": row["user_input"],
                    "events": tool_events, "reward": 1.0 if row["status"] == "completed" else 0.0,
                    "reward_source": "deterministic_runtime_status",
                })
        for name, records in (("sft.jsonl", sft), ("dpo.jsonl", dpo),
                              ("verifier_rl.jsonl", verifier_rl)):
            with (destination / name).open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        manifest = {"sft": len(sft), "dpo": len(dpo), "verifier_rl": len(verifier_rl), "generated_at": time.time(),
                    "review_required": True, "automatic_training": False}
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"sft": len(sft), "dpo": len(dpo), "verifier_rl": len(verifier_rl)}


_learning_runtime: LearningRuntime | None = None


def get_learning_runtime() -> LearningRuntime:
    global _learning_runtime
    if _learning_runtime is None:
        _learning_runtime = LearningRuntime()
    return _learning_runtime


def record_runtime_event(event_type: str, **payload: Any) -> None:
    get_learning_runtime().event(event_type, payload)
