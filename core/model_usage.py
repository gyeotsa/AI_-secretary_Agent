"""Read-only usage readers keyed by the same model IDs as the picker."""
import time

from config import Config


# New providers can return the same snapshot without changing the picker.
USAGE_READERS = {}


def read_model_usage(model, *, codex_runtime=None):
    if model in USAGE_READERS:
        return USAGE_READERS[model]()
    if model == "gpt":
        if codex_runtime is None:
            from core.codex_client import get_codex_runtime
            codex_runtime = get_codex_runtime()
        reader = getattr(codex_runtime, "usage", None)
        data = reader() if reader else {"error_code": "unsupported"}
        return {**data, "scope": "account", "note": "Codex 계정 공용 · 모델별 잔여 토큰 수는 미제공"}
    local = model == "kimi_k3" or (model == "default" and Config.LLM_PROVIDER == "ollama")
    return {"scope": "model", "rate_limits": None, "token_usage": {},
            "quota_applicable": not local,
            "error_code": "unsupported", "fetched_at": time.time(),
            "note": ("로컬 실행 · 계정 사용 한도는 해당 없음. 실제 토큰 통계는 미제공"
                     if local else "현재 연결에서는 토큰 사용량과 남은 한도를 조회할 수 없습니다.")}
