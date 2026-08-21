"""Bounded prompt context with durable offloading and resumable checkpoints."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Any
import hashlib
import json
import time


@dataclass(frozen=True)
class ContextBudget:
    recent_messages: int = 10
    max_chars: int = 18_000
    max_tool_result_chars: int = 2_500


class ContextLifecycleManager:
    def __init__(self, root: str | Path = "data/context", budget: ContextBudget | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = budget or ContextBudget()

    def offload(self, content: str, *, kind: str, metadata: Mapping[str, Any] | None = None) -> str:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = self.root / "offloaded" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            payload = {"kind": kind, "content": content, "metadata": dict(metadata or {}), "created_at": time.time()}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"context://{digest}"

    def compact_messages(self, messages: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
        normalized = [{"role": str(item.get("role", "user")), "content": str(item.get("content", ""))}
                      for item in messages]
        recent = normalized[-self.budget.recent_messages:]
        total = 0
        bounded = []
        for item in reversed(recent):
            content = item["content"]
            if len(content) > self.budget.max_tool_result_chars and item["role"] == "tool":
                reference = self.offload(content, kind="tool_result")
                content = content[:600] + f"\n[전체 결과: {reference}]"
            remaining = self.budget.max_chars - total
            if remaining <= 0:
                break
            bounded.append({"role": item["role"], "content": content[:remaining]})
            total += min(len(content), remaining)
        return list(reversed(bounded))

    def save_checkpoint(self, task_id: str, state: Mapping[str, Any]) -> Path:
        safe_id = "".join(ch for ch in task_id if ch.isalnum() or ch in "-_")[:80]
        if not safe_id:
            raise ValueError("유효한 task_id가 필요합니다.")
        path = self.root / "checkpoints" / f"{safe_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"task_id": task_id, "state": dict(state), "updated_at": time.time()},
                                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        temp.replace(path)
        return path

    def load_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        safe_id = "".join(ch for ch in task_id if ch.isalnum() or ch in "-_")[:80]
        path = self.root / "checkpoints" / f"{safe_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))["state"] if path.exists() else None
