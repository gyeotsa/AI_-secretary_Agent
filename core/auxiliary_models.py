"""Persistent opt-in model selection; switching on never loads weights."""
import os
import threading

from core.assistant_settings import get_assistant_settings


AUXILIARY_MODELS = {"kimi_k3": "Kimi K3", "jev": "Jev"}
MODEL_DESCRIPTIONS = {
    "kimi_k3": "로컬 코딩 보조 · 연결 보류",
    "jev": "클라우드 응답 경로 분류 · 대화 / 코드 / 분석 / 작업",
}
_state_lock = threading.RLock()
_generation = 0
_shutdown = False
_status = "대기"


def selection():
    settings = get_assistant_settings()
    model = settings.get("auxiliary_model")
    if model not in AUXILIARY_MODELS:
        model = "kimi_k3"
    if model == "kimi_k3":
        # Migrate the existing single flag without making old settings unusable.
        enabled = (
            settings.get("auxiliary_model_enabled_kimi_k3") == "true"
            or settings.get("auxiliary_model_enabled") == "true"
        )
    else:
        enabled = settings.get(f"auxiliary_model_enabled_{model}") == "true"
    return model, enabled


def coding_timeout_seconds():
    """Registered tool budgets must allow a later opt-in without re-registration."""
    try:
        seconds = int(os.getenv("KIMI_K3_TIMEOUT_SECONDS", "7200"))
    except ValueError:
        seconds = 7200
    if not 1 <= seconds <= 86400:
        seconds = 7200  # Bad optional settings must not break base tool registration.
    # KimiOptions still rejects invalid values before starting an actual K3 call.
    return max(360, seconds * 3 + 600)


def configure(model, enabled):
    if model not in AUXILIARY_MODELS or not isinstance(enabled, bool):
        raise ValueError("지원하지 않는 보조 모델 설정입니다.")
    global _generation
    with _state_lock:
        settings = get_assistant_settings()
        settings.set("auxiliary_model", model)
        # ponytail: one active auxiliary model keeps the laptop path simple;
        # per-model concurrency can be added only when a real use case needs it.
        for key in AUXILIARY_MODELS:
            settings.set(
                f"auxiliary_model_enabled_{key}",
                "true" if enabled and key == model else "false",
            )
        settings.set("auxiliary_model_enabled", "true" if enabled and model == "kimi_k3" else "false")
        _generation += 1  # OFF then ON must not revive an old queued/running call.
        set_status("대기" if enabled else "OFF · 기본 모델 사용")


def ticket():
    with _state_lock:
        return _generation


def invalidate():
    """Account changes invalidate already queued or running calls."""
    global _generation
    with _state_lock:
        _generation += 1


def is_current(generation):
    with _state_lock:
        return not _shutdown and generation == _generation


def set_status(value):
    global _status
    with _state_lock:
        _status = value


def status():
    with _state_lock:
        return _status


def shutdown():
    global _shutdown, _generation
    with _state_lock:
        _shutdown = True
        _generation += 1
