import math

import pytest

from core import model_usage
from ui.model_usage import format_usage, quota_rows


def test_codex_account_usage_uses_supplied_runtime_without_inference():
    class Runtime:
        def usage(self):
            return {"token_usage": {"lifetime_tokens": 0, "today_tokens": None}, "rate_limits": None}
    data = model_usage.read_model_usage("gpt", codex_runtime=Runtime())
    assert data["scope"] == "account"
    assert "계정 공용" in data["note"]
    text = format_usage(data)
    assert "누적 사용 토큰: 0" in text
    assert "남은 토큰 수: 미제공" in text and "사용 한도: 조회 불가" in text
    assert "오늘 사용 토큰: 0" not in text


def test_future_provider_reader_keeps_real_token_counts(monkeypatch):
    monkeypatch.setitem(model_usage.USAGE_READERS, "future", lambda: {
        "token_usage": {"used_tokens": 1200, "remaining_tokens": 800}, "scope": "model"})
    text = format_usage(model_usage.read_model_usage("future"))
    assert "사용 토큰: 1,200" in text and "남은 토큰: 800" in text
    assert "미제공" not in text


def test_local_model_has_no_subscription_quota_but_never_invents_usage(monkeypatch):
    monkeypatch.setattr(model_usage.Config, "LLM_PROVIDER", "ollama")
    data = model_usage.read_model_usage("default")
    text = format_usage(data)
    assert "사용 토큰: 조회 불가" in text
    assert "계정 사용 한도: 해당 없음" in text
    assert "남은 토큰 수" not in text


@pytest.mark.parametrize("provider", ["jev", "unregistered"])
def test_unknown_usage_is_not_zero(provider):
    text = format_usage(model_usage.read_model_usage(provider))
    assert "조회 불가" in text and "사용 0%" not in text


def test_multiple_shared_quota_windows_zero_and_full_usage():
    text = format_usage({"rate_limits": [
        {"limit_id": "codex", "primary": {"used_percent": 0, "window_duration_mins": 300},
         "secondary": {"used_percent": 100, "window_duration_mins": 10080}},
        {"limit_id": "other", "primary": {"used_percent": 42, "window_duration_mins": 60}},
    ]})
    assert "codex · 5시간: 사용 0% · 남음 100%" in text
    assert "codex · 7일: 사용 100% · 남음 0%" in text
    assert "other · 1시간: 사용 42% · 남음 58%" in text
    assert "초기화: 미제공" in text


@pytest.mark.parametrize("value", [None, True, -1, math.nan, math.inf, 0.5, "100"])
def test_invalid_token_metrics_and_provider_error_details_are_not_displayed(value):
    text = format_usage({"token_usage": {"used_tokens": value, "remaining_tokens": value},
                         "error_code": "provider-secret", "error_message": "private-value"})
    assert "사용 토큰: 조회 불가" in text and "남은 토큰 수: 미제공" in text
    assert "private-value" not in text and "provider-secret" not in text


def test_malformed_nested_values_are_safe_and_empty_buckets_are_distinct():
    text = format_usage({"token_usage": True, "rate_limits": 15})
    assert "조회 불가" in text
    assert "제공된 한도 없음" in format_usage({"rate_limits": []})


@pytest.mark.parametrize("error", [{"detail": "private-error"}, ["private-error"]])
def test_malformed_provider_error_codes_cannot_break_the_ui(error):
    text = format_usage({"error_code": error})
    assert "사용량 조회 실패" in text and "private-error" not in text


def test_usage_bars_require_real_limits_and_never_infer_tokens_from_account_percent():
    assert quota_rows({"token_usage": {"lifetime_tokens": 12345}}) == []
    row = quota_rows({"token_usage": {"used_tokens": 1200, "remaining_tokens": 800}})[0]
    assert row["remaining"] == 40 and row["used"] == 60
    row = quota_rows({"rate_limits": [{"primary": {"used_percent": None}}]})[0]
    assert row["remaining"] is None
