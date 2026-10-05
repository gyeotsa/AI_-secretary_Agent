"""One in-process local inference at a time, including CPU-only backends."""
from contextlib import contextmanager
from functools import wraps
import gc
import inspect
import threading
import time

from core.turn_context import check_turn_cancelled


# ponytail: process-wide serialization; use a resource broker if multiple Anis
# processes must coordinate. External apps are not controlled by this lock.
_lock = threading.RLock()
_idle_releases = {}


@contextmanager
def local_inference(check=None, timeout=86400):
    """Cancellation remains responsive while another model owns the slot."""
    deadline = time.monotonic() + timeout
    while True:
        check_turn_cancelled()
        if check:
            check()
        if _lock.acquire(timeout=0.05):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("로컬 모델 실행 순서를 기다리다 제한시간을 초과했습니다.")
    try:
        check_turn_cancelled()
        if check:
            check()
        yield
    finally:
        _lock.release()


def serialized_inference(function):
    """Also keep streaming generators inside their lease until exhaustion."""
    if inspect.isgeneratorfunction(function):
        @wraps(function)
        def generate(*args, **kwargs):
            with local_inference():
                yield from function(*args, **kwargs)
        return generate

    @wraps(function)
    def call(*args, **kwargs):
        with local_inference():
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
