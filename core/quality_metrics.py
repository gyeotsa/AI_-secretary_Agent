"""Persistent operational quality metrics and acceptance scenario evaluation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import sqlite3
import threading

from core.productization import METRICS


QUALITY_KEYS = (
    "runtime_completion", "clarification_requested",
    "task_success", "false_completion", "clarification_quality",
    "tool_selection_accuracy", "latency_ms", "rag_used",
    "long_run_recovery", "stt_false_wake", "stt_echo",
    "specialist_artifact_quality", "mockup_visual_approval",
)


@dataclass(frozen=True)
class QualityObservation:
    key: str
    value: float
    success: bool
    context: dict[str, Any]
    created_at: str


class QualityMetricStore:
    def __init__(self, db_path: str = "data/quality_metrics.db"):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS quality_observations(
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value REAL NOT NULL,
                success INTEGER NOT NULL, context TEXT NOT NULL, created_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_quality_key_time ON quality_observations(key,created_at DESC)")

    def record(self, key: str, value: float = 1.0, *, success: bool = True,
               context: dict[str, Any] | None = None) -> None:
        if key not in QUALITY_KEYS:
            raise KeyError(f"등록되지 않은 품질 지표입니다: {key}")
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO quality_observations(key,value,success,context,created_at) VALUES(?,?,?,?,?)",
                         (key, float(value), int(bool(success)), json.dumps(context or {}, ensure_ascii=False), now))
        METRICS.increment(f"quality.{key}.count")
        if key == "latency_ms":
            METRICS.observe("quality.end_to_end.latency", float(value))

    def snapshot(self, limit_per_key: int = 500) -> dict[str, Any]:
        result: dict[str, Any] = {}
        with self._lock, sqlite3.connect(self.db_path) as conn:
            for key in QUALITY_KEYS:
                rows = conn.execute("SELECT value,success FROM quality_observations WHERE key=? ORDER BY created_at DESC LIMIT ?",
                                    (key, max(1, int(limit_per_key)))).fetchall()
                values = [float(row[0]) for row in rows]
                result[key] = {
                    "count": len(rows), "success_rate": round(sum(bool(row[1]) for row in rows) / len(rows), 4) if rows else None,
                    "average": round(sum(values) / len(values), 2) if values else None,
                }
        return {"updated_at": datetime.now().astimezone().isoformat(), "metrics": result}


class AcceptanceScenarioEvaluator:
    """Evaluates recorded outcomes. It does not fabricate unavailable observations."""

    TARGETS = {
        "task_success": (">=", 0.90), "false_completion": ("<=", 0.01),
        "clarification_quality": (">=", 0.85), "tool_selection_accuracy": (">=", 0.90),
        "latency_ms": ("<=", 15000.0), "rag_used": (">=", 0.70),
        "long_run_recovery": (">=", 0.95), "stt_false_wake": ("<=", 0.02),
        "stt_echo": ("<=", 0.01),
        "specialist_artifact_quality": (">=", 0.98),
        "mockup_visual_approval": (">=", 0.90),
    }
    # A single lucky interaction is not product acceptance.  These are small
    # enough for local operation while still preventing one-sample "100%".
    MIN_SAMPLES = {
        "task_success": 30, "false_completion": 30,
        "clarification_quality": 15, "tool_selection_accuracy": 30,
        "latency_ms": 20, "rag_used": 15, "long_run_recovery": 3,
        "stt_false_wake": 30, "stt_echo": 30,
        "specialist_artifact_quality": 10, "mockup_visual_approval": 10,
    }

    def __init__(self, store: QualityMetricStore):
        self.store = store

    def evaluate(self) -> dict[str, Any]:
        snapshot = self.store.snapshot()
        scenarios = []
        for key, (operator, target) in self.TARGETS.items():
            metric = snapshot["metrics"][key]
            value = metric["average"]
            count = int(metric.get("count") or 0)
            minimum = self.MIN_SAMPLES[key]
            if value is None:
                status, reason = "not_run", "실제 관측 데이터가 없습니다."
            elif count < minimum:
                status, reason = "insufficient", f"표본 {count}/{minimum}개로 판정할 수 없습니다."
            else:
                passed = value >= target if operator == ">=" else value <= target
                status, reason = ("passed" if passed else "failed"), f"측정 {value} {operator} 목표 {target}"
            scenarios.append({"key": key, "status": status, "value": value,
                              "sample_count": count, "minimum_samples": minimum,
                              "operator": operator, "target": target, "reason": reason})
        return {"generated_at": datetime.now().astimezone().isoformat(), "scenarios": scenarios,
                "all_passed": bool(scenarios) and all(item["status"] == "passed" for item in scenarios)}


_quality_store: QualityMetricStore | None = None


def get_quality_metric_store() -> QualityMetricStore:
    global _quality_store
    if _quality_store is None:
        _quality_store = QualityMetricStore()
    return _quality_store
