import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.llm import ModelCallError
from core.local_inference import InferenceDeadlineError
from main_qt import JarvisApp, TurnEnvelope


@pytest.mark.parametrize("error,code", [
    (ModelCallError("codex", "selected-gpt", "timeout", "응답 제한시간 초과"), "timeout"),
    (InferenceDeadlineError("shared deadline"), "inference_deadline_exceeded"),
    (RuntimeError("private diagnostic detail"), "RuntimeError"),
])
def test_model_failure_is_delivered_without_another_model_call(error, code):
    app = JarvisApp.__new__(JarvisApp)
    app.signals = SimpleNamespace(ai_response_ready=Mock())
    app.executor = Mock()
    app.executor.execute_turn.side_effect = error

    app._process_ai(TurnEnvelope("error-turn", "error-chat", "이 문제 풀어줘", tuple()))

    result = app.signals.ai_response_ready.emit.call_args.args[0]
    assert result.status == "failed"
    assert result.error_code == code
    assert f"[시스템 상태: {code}]" in result.response_text
    assert "private diagnostic detail" not in result.response_text
    app.executor.render_outcome.assert_not_called()
