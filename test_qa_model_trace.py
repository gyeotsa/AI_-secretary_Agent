"""QA model tracing is opt-in, metadata-only, and transparent to model calls."""
import io
import json
from types import SimpleNamespace

import pytest
import requests

from core import llm
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context
from scripts import qa_common_runtime as qa


def records(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()
            if line.startswith('{"event": "model_trace"')]


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


def test_disabled_trace_does_not_patch_or_emit(monkeypatch, capsys):
    original = lambda *args, **kwargs: "original"
    monkeypatch.setattr(llm, "post_json", original)
    with qa.trace_model_calls():
        assert llm.post_json is original
    assert llm.post_json is original
    assert not records(capsys)


def test_trace_records_only_bounded_metadata_and_preserves_request(monkeypatch, capsys):
    secret = "PRIVATE-MESSAGE-HEADER-ERROR"
    expected = response({
        "message": {"content": secret}, "error": secret,
        "prompt_eval_count": 200, "prompt_eval_duration": 300,
        "eval_count": 1024, "eval_duration": 4_000_000_000,
        "total_duration": 5_000_000_000, "load_duration": 1_000_000_000,
        "done": True, "done_reason": "length", "unknown": secret,
    })
    calls = []
    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return expected
    monkeypatch.setattr(llm, "post_json", original)
    payload = {"model": "test-model", "messages": [{"content": secret}],
               "format": {"description": secret},
               "tools": [{"description": secret}],
               "options": {"num_ctx": 8192, "num_predict": 1024, "unknown": secret}}
    kwargs = {"json": payload, "timeout": (5, 120), "headers": {"secret": secret}}
    with qa.trace_model_calls(True), bind_turn_context(TurnExecutionContext(secret, secret)):
        assert llm.post_json("https://private.invalid/" + secret, **kwargs) is expected
    assert llm.post_json is original
    assert calls == [(("https://private.invalid/" + secret,), kwargs)]
    assert calls[0][1]["json"] is payload
    logs = records(capsys)
    assert len(logs) == 2 and secret not in json.dumps(logs)
    start, end = logs
    assert start["phase"] == "start" and end["phase"] == "end"
    assert start["call_id"] == end["call_id"] == 1
    assert start["schema_present"] and start["turn_bound"]
    assert start["num_ctx"] == 8192 and start["num_predict"] == 1024
    assert end["eval_count"] == 1024 and end["load_duration"] == 1_000_000_000
    assert end["done_reason"] == "length" and end["provider_error_present"]
    assert end["elapsed_seconds"] >= 0 and end["http_status"] == 200


@pytest.mark.parametrize("failure", [requests.Timeout("private timeout endpoint"),
                                      ToolCancelledError("private turn identity")])
def test_trace_preserves_exception_and_restores_patch(monkeypatch, capsys, failure):
    calls = []
    def original(*args, **kwargs):
        calls.append((args, kwargs))
        raise failure
    monkeypatch.setattr(llm, "post_json", original)
    with pytest.raises(type(failure)) as raised:
        with qa.trace_model_calls(True):
            llm.post_json("private", json={"model": "test"}, timeout=120)
    assert raised.value is failure and len(calls) == 1
    assert llm.post_json is original
    logs = records(capsys)
    assert len(logs) == 2 and "private" not in json.dumps(logs)
    assert logs[-1]["outcome"] == "error"
    assert logs[-1]["error_type"] == type(failure).__name__


@pytest.mark.parametrize("method,kind", [("chat_prose", "prose"),
                                        ("chat_structured", "structured")])
def test_trace_identifies_real_caller_role_without_model_requests(monkeypatch, capsys, method, kind):
    monkeypatch.setattr(llm, "post_json", lambda *args, **kwargs: response({
        "message": {"content": "safe"}, "done": True, "done_reason": "stop",
    }))
    client = object.__new__(llm.OllamaClient)
    client.model = "test"
    client.base_url = "http://unused.invalid"
    client.system_prompt = ""
    client.profile = SimpleNamespace(role="conversation", keep_alive="10m",
                                     temperature=0.65, max_tokens=1024)
    with qa.trace_model_calls(True):
        assert getattr(client, method)([]) == "safe"
    logs = records(capsys)
    assert logs[0]["role"] == "conversation" and logs[0]["call_kind"] == kind
    assert logs[0]["num_ctx"] is None and not logs[0]["schema_present"]


def test_trace_metadata_decode_error_does_not_replace_response(monkeypatch, capsys):
    expected = requests.Response()
    expected.status_code = 502
    expected._content = b"private invalid JSON"
    original = lambda *args, **kwargs: expected
    monkeypatch.setattr(llm, "post_json", original)
    with qa.trace_model_calls(True):
        assert llm.post_json("private", json={}, timeout=120) is expected
    assert llm.post_json is original
    end = records(capsys)[-1]
    assert end["http_status"] == 502 and end["metadata_error_type"] == "JSONDecodeError"
    assert "private" not in json.dumps(end)
    with pytest.raises(requests.JSONDecodeError):
        expected.json()


def test_trace_filters_invalid_provider_metadata(monkeypatch, capsys):
    monkeypatch.setattr(llm, "post_json", lambda *args, **kwargs: response({
        "eval_count": "private", "eval_duration": -1, "load_duration": float("nan"),
        "total_duration": 10**19, "prompt_eval_count": True,
        "done": "private", "done_reason": "private",
    }))
    with qa.trace_model_calls(True):
        llm.post_json("private", json={"model": "x" * 500,
                      "options": {"num_predict": "private", "num_ctx": False}}, timeout=120)
    logs = records(capsys)
    assert "private" not in json.dumps(logs)
    assert len(logs[0]["model"]) == 128
    assert logs[0]["num_predict"] is None and logs[0]["num_ctx"] is None
    end = logs[-1]
    for key in ("eval_count", "eval_duration", "load_duration", "total_duration",
                "prompt_eval_count", "done", "done_reason"):
        assert end[key] is None


def test_trace_stdout_failure_does_not_change_result_or_exception(monkeypatch):
    expected = response({})
    original = lambda *args, **kwargs: expected
    monkeypatch.setattr(llm, "post_json", original)
    def broken_print(*args, **kwargs):
        raise BrokenPipeError("closed diagnostic output")
    monkeypatch.setattr(qa, "print", broken_print, raising=False)
    with qa.trace_model_calls(True):
        assert llm.post_json("private", json={}, timeout=120) is expected
    assert llm.post_json is original
    failure = requests.Timeout("unchanged")
    def failing_original(*args, **kwargs):
        raise failure
    monkeypatch.setattr(llm, "post_json", failing_original)
    with qa.trace_model_calls(True), pytest.raises(requests.Timeout) as raised:
        llm.post_json("private", json={}, timeout=120)
    assert raised.value is failure and llm.post_json is failing_original


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_main_installs_opt_in_trace_before_runtime_and_restores_on_exit(
        monkeypatch, tmp_path, enabled, fail):
    class Output(io.StringIO):
        def reconfigure(self, **kwargs):
            pass
    output = Output()
    settings = SimpleNamespace(set=lambda *args: None, set_tts_enabled=lambda *args: None)
    monkeypatch.setitem(qa.sys.modules, "core.assistant_settings",
                        SimpleNamespace(get_assistant_settings=lambda: settings))
    monkeypatch.setattr(qa.sys, "stdout", output)
    monkeypatch.setattr(qa.sys, "argv", ["qa", "--gui"] + (["--model-trace"] if enabled else []))
    monkeypatch.setattr(qa.sys, "path", list(qa.sys.path))
    monkeypatch.setattr(qa.os, "environ", dict(qa.os.environ))
    monkeypatch.setattr(qa.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    monkeypatch.chdir(tmp_path)
    original = lambda *args, **kwargs: response({"done": True})
    monkeypatch.setattr(llm, "post_json", original)

    def runtime(args, root, sandbox, actual_settings):
        assert args.gui and args.model_trace is enabled
        assert sandbox == tmp_path and actual_settings is settings
        assert (llm.post_json is not original) is enabled
        llm.post_json("unused", json={"model": "test"}, timeout=120)
        if fail:
            raise RuntimeError("synthetic startup failure")
        return 7
    monkeypatch.setattr(qa, "_run_qa", runtime)
    if fail:
        with pytest.raises(RuntimeError, match="synthetic startup failure"):
            qa.main()
    else:
        assert qa.main() == 7
    assert llm.post_json is original
    assert output.getvalue().count('"event": "model_trace"') == (2 if enabled else 0)
