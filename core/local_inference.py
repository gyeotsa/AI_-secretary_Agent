"""One in-process local inference at a time, including CPU-only backends."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import gc
import inspect
import math
import threading
import time

from core.turn_context import check_turn_cancelled


# ponytail: process-wide serialization; use a resource broker if multiple Anis
# processes must coordinate. External apps are not controlled by this lock.
_lock = threading.RLock()
_idle_releases = {}
_deadline = ContextVar("anis_inference_deadline", default=None)


class InferenceDeadlineError(TimeoutError):
    code = "inference_deadline_exceeded"


def current_inference_deadline():
    return _deadline.get()


def check_inference_deadline():
    check_turn_cancelled()
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise InferenceDeadlineError("AI 요청의 전체 처리 시간이 초과되어 후속 추론을 중단했습니다.")


@contextmanager
def inference_deadline(timeout=None):
    """Queue, I/O and nested fallbacks share a clock; never cancel the turn."""
    check_inference_deadline()
    if timeout is None:
        yield
        check_inference_deadline()
        return
    try:
        valid = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                 and math.isfinite(timeout) and timeout > 0)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("request_timeout must be a positive finite number")
    deadline = time.monotonic() + timeout
    parent = _deadline.get()
    token = _deadline.set(min(parent, deadline) if parent is not None else deadline)
    try:
        yield
        check_inference_deadline()
    except InferenceDeadlineError:
        check_turn_cancelled()
        raise
    finally:
        _deadline.reset(token)


@contextmanager
def local_inference(check=None, timeout=86400):
    """Cancellation remains responsive while another model owns the slot."""
    deadline = time.monotonic() + timeout
    while True:
        check_inference_deadline()
        if check:
            check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("로컬 모델 실행 순서를 기다리다 제한시간을 초과했습니다.")
        if _lock.acquire(timeout=min(0.05, remaining)):
            break
    try:
        check_inference_deadline()
        if check:
            check()
        if time.monotonic() >= deadline:
            raise TimeoutError("로컬 모델 실행 순서를 기다리다 제한시간을 초과했습니다.")
        yield
        check_inference_deadline()
    finally:
        _lock.release()


def serialized_inference(function):
    """Also keep streaming generators inside their lease until exhaustion."""
    if inspect.isgeneratorfunction(function):
        @wraps(function)
        def generate(*args, **kwargs):
            with inference_deadline(kwargs.get("request_timeout")), local_inference():
                yield from function(*args, **kwargs)
        return generate

    @wraps(function)
    def call(*args, **kwargs):
        with inference_deadline(kwargs.get("request_timeout")), local_inference():
            return function(*args, **kwargs)
    return call


def remember_idle_model(key, release):
    """Register only models this app actually used; never arbitrary servers."""
    with _lock:
        _idle_releases[key] = release


def release_idle_models():
    """Called under the K3 lease, before its memory admission check."""
    with _lock:
        for key, release in list(_idle_releases.items()):
            if release() is False:
                raise RuntimeError(f"기존 모델을 해제하지 못해 Kimi K3 실행을 중단했습니다: {key}")
            _idle_releases.pop(key, None)
        gc.collect()
