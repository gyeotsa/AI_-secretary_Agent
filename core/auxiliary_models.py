"""Persistent opt-in model selection; switching on never loads weights."""
import os
import threading

from core.assistant_settings import get_assistant_settings


AUXILIARY_MODELS = {"default": "기존 모델", "kimi_k3": "Kimi K3", "jev": "Jev", "gpt": "GPT"}
MODEL_DESCRIPTIONS = {
    "default": "기존 설정 모델 · 대화 / 판단 / 코드",
    "kimi_k3": "메인 모델 · 연결 보류",
    "jev": "보조 모델 · 대화 / 코드 / 분석 / 작업 분류",
    "gpt": "ChatGPT 구독 · Codex 엔진 · 대화 / 판단 / 코드",
}
_state_lock = threading.RLock()
_generation = 0
_jev_generation = 0
_shutdown = False
_status = "대기"
_jev_status = "대기"


def _main_model(settings):
    if settings.get("auxiliary_model_enabled_gpt") == "true":
        return "gpt"
    if (settings.get("auxiliary_model_enabled_kimi_k3") == "true"
            or settings.get("auxiliary_model_enabled") == "true"):
        return "kimi_k3"
    return "default"


def main_selection():
    with _state_lock:
        return _main_model(get_assistant_settings()), True


def is_enabled(model):
    with _state_lock:
        settings = get_assistant_settings()
        if model == "jev":
            return settings.get("auxiliary_model_enabled_jev") == "true"
        return model in AUXILIARY_MODELS and _main_model(settings) == model


def selection():
    """Last selected list item; inference uses main_selection/is_enabled."""
    with _state_lock:
        model = get_assistant_settings().get("auxiliary_model")
        if model not in AUXILIARY_MODELS:
            model = "default"
        return model, is_enabled(model)


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
    with _state_lock:
        settings = get_assistant_settings()
        settings.set("auxiliary_model", model)
        if model == "jev":
            changed = is_enabled("jev") != enabled
            settings.set("auxiliary_model_enabled_jev", "true" if enabled else "false")
            if changed:
                invalidate("jev")
            set_status("대기" if enabled else "OFF · 기본 분류 사용", "jev")
            return
        previous = _main_model(settings)
        if enabled:
            for key in ("gpt", "kimi_k3"):
                settings.set(f"auxiliary_model_enabled_{key}", "true" if key == model else "false")
            settings.set("auxiliary_model_enabled", "true" if model == "kimi_k3" else "false")
        elif model != "default":
            settings.set(f"auxiliary_model_enabled_{model}", "false")
            if model == "kimi_k3":
                settings.set("auxiliary_model_enabled", "false")
        if previous != _main_model(settings):
            invalidate()  # OFF then ON must not revive an old queued/running call.
            set_status("대기" if enabled else "OFF · 기본 모델 사용")


def ticket(model=None):
    with _state_lock:
        return _jev_generation if model == "jev" else _generation


def invalidate(model=None):
    """Account changes invalidate already queued or running calls."""
    global _generation, _jev_generation
    with _state_lock:
        if model == "jev":
            _jev_generation += 1
        else:
            _generation += 1


def is_current(generation, model=None):
    with _state_lock:
        return not _shutdown and generation == ticket(model)


def set_status(value, model=None):
    global _status, _jev_status
    with _state_lock:
        if model == "jev":
            _jev_status = value
        else:
            _status = value


def status(model=None):
    with _state_lock:
        return _jev_status if model == "jev" else _status


def shutdown():
    global _shutdown, _generation, _jev_generation
    with _state_lock:
        _shutdown = True
        _generation += 1
        _jev_generation += 1
    from core.codex_client import get_codex_runtime
    get_codex_runtime().shutdown()
