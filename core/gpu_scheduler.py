"""Process-wide GPU admission queue with explicit VRAM budgets."""
from __future__ import annotations

import heapq
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass(order=True)
class _Request:
    priority: int
    sequence: int
    request_id: str = field(compare=False)
    role: str = field(compare=False)
    vram_mb: int = field(compare=False)


@dataclass(frozen=True)
class GPUAdmission:
    request_id: str
    role: str
    device: str
    reserved_vram_mb: int
    waited_ms: float


class GPUResourceQueue:
    """Serializes large local models while allowing bounded smaller reservations."""

    def __init__(self, budget_mb: Optional[int] = None):
        self.budget_mb = max(512, int(budget_mb or self._detect_budget_mb()))
        self._condition = threading.Condition()
        self._waiting: list[_Request] = []
        self._active: dict[str, tuple[int, str, float]] = {}
        self._sequence = 0

    @staticmethod
    def _detect_budget_mb() -> int:
        configured = os.getenv("GPU_VRAM_BUDGET_MB", "").strip()
        if configured:
            return max(512, int(configured))
        try:
            import torch
            if torch.cuda.is_available():
                total = int(torch.cuda.get_device_properties(0).total_memory / 1024**2)
                return max(1024, int(total * 0.82))
        except Exception:
            pass
        return 1024

    @property
    def reserved_mb(self) -> int:
        with self._condition:
            return sum(item[0] for item in self._active.values())

    def acquire(self, role: str, vram_mb: int, *, priority: int = 10,
                timeout: float = 120.0) -> GPUAdmission:
        requested = max(0, int(vram_mb))
        if requested > self.budget_mb:
            raise MemoryError(f"GPU 요청 {requested}MB가 예산 {self.budget_mb}MB를 초과합니다.")
        started = time.monotonic()
        with self._condition:
            self._sequence += 1
            request = _Request(int(priority), self._sequence, uuid.uuid4().hex, str(role), requested)
            heapq.heappush(self._waiting, request)
            while True:
                is_head = self._waiting and self._waiting[0].request_id == request.request_id
                fits = sum(item[0] for item in self._active.values()) + requested <= self.budget_mb
                if is_head and fits:
                    heapq.heappop(self._waiting)
                    self._active[request.request_id] = (requested, request.role, time.time())
                    device = "cuda" if self.budget_mb > 1024 else "cpu"
                    admission = GPUAdmission(request.request_id, request.role, device, requested,
                                             (time.monotonic() - started) * 1000)
                    try:
                        from core.productization import METRICS
                        METRICS.gauge("gpu.budget_mb", self.budget_mb)
                        METRICS.gauge("gpu.reserved_mb", sum(item[0] for item in self._active.values()))
                        METRICS.observe("gpu.wait_latency", admission.waited_ms)
                    except Exception:
                        pass
                    return admission
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    self._waiting = [item for item in self._waiting if item.request_id != request.request_id]
                    heapq.heapify(self._waiting)
                    raise TimeoutError(f"GPU 자원 대기 시간이 초과되었습니다: {role}")
                self._condition.wait(min(remaining, 0.25))

    def release(self, admission: GPUAdmission) -> None:
        with self._condition:
            self._active.pop(admission.request_id, None)
            try:
                from core.productization import METRICS
                METRICS.gauge("gpu.reserved_mb", sum(item[0] for item in self._active.values()))
            except Exception:
                pass
            self._condition.notify_all()

    @contextmanager
    def reserve(self, role: str, vram_mb: int, *, priority: int = 10,
                timeout: float = 120.0) -> Iterator[GPUAdmission]:
        admission = self.acquire(role, vram_mb, priority=priority, timeout=timeout)
        try:
            yield admission
        finally:
            self.release(admission)

    def snapshot(self) -> dict:
        with self._condition:
            return {"budget_mb": self.budget_mb,
                    "reserved_mb": sum(item[0] for item in self._active.values()),
                    "active": len(self._active), "waiting": len(self._waiting),
                    "active_requests": [
                        {"request_id": key, "vram_mb": value[0], "role": value[1],
                         "started_at": value[2]} for key, value in self._active.items()
                    ],
                    "waiting_requests": [
                        {"request_id": item.request_id, "role": item.role,
                         "vram_mb": item.vram_mb, "priority": item.priority}
                        for item in sorted(self._waiting)
                    ]}


_queue = None


def get_gpu_resource_queue() -> GPUResourceQueue:
    global _queue
    if _queue is None:
        _queue = GPUResourceQueue()
    return _queue
