"""Immutable turn attribution with cancellation shared by every execution boundary.

Cancellation prevents future work. It cannot undo a side effect that has already
started; tools retain their own uncertain/read-back contracts for that case.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from core.plugin import CancellationToken, ToolCancelledError


class LinkedToolCancellationToken(CancellationToken):
    """A tool inherits turn cancellation without cancelling its parent on timeout."""

    def __init__(self, parent: CancellationToken):
        super().__init__()
        self._parent = parent

    @property
    def cancelled(self) -> bool:
        return super().cancelled or self._parent.cancelled


@dataclass(frozen=True)
class TurnExecutionContext:
    turn_id: str
    session_id: str
    workspace_path: str = ""
    memory_namespace: str = "global"
    generation: int = 0
    cancellation_token: CancellationToken = field(
        default_factory=CancellationToken, compare=False, repr=False,
    )

    @property
    def cancelled(self) -> bool:
        return self.cancellation_token.cancelled

    def cancel(self) -> None:
        self.cancellation_token.cancel()

    def tool_token(self) -> CancellationToken:
        return LinkedToolCancellationToken(self.cancellation_token)

    def checkpoint(self) -> None:
        if self.cancelled:
            raise ToolCancelledError("새 입력 또는 사용자 취소로 이 요청의 후속 실행을 중단했습니다.")


_current_turn: ContextVar[TurnExecutionContext | None] = ContextVar("anis_turn", default=None)


def current_turn_context() -> TurnExecutionContext | None:
    return _current_turn.get()


def check_turn_cancelled() -> None:
    context = current_turn_context()
    if context is not None:
        context.checkpoint()


@contextmanager
def bind_turn_context(context: TurnExecutionContext | None) -> Iterator[None]:
    # A nested compatibility call inherits its parent's identity.
    if context is None:
        yield
        return
    token = _current_turn.set(context)
    try:
        context.checkpoint()
        yield
    finally:
        _current_turn.reset(token)
