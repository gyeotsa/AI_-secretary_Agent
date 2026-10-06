"""QA model tracing is opt-in, metadata-only, and transparent to model calls."""
import io
import json
from types import SimpleNamespace

import pytest
import requests

from core import llm
from core.plugin import ToolCancelledError
from core.local_inference import InferenceDeadlineError
from core.semantic_request import SemanticDecision
from core.tool_result import Evidence, ToolRunResult, ToolRunStatus
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
                                      InferenceDeadlineError("private deadline endpoint"),
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


@pytest.mark.parametrize('fault', [None, 'extra_line', 'wrong_bounds', 'wrong_hash',
                                  'truncated', 'no_evidence', 'wrong_tool', 'failed',
                                  'changed_file', 'extra_display', 'missing_display',
                                  'wrong_path', 'missing_path', 'wrong_evidence_kind',
                                  'wrong_evidence_path', 'wrong_evidence_hash',
                                  'wrong_evidence_content', 'wrong_evidence_bounds', 'failed_receipt'])
def test_read_qa_requires_exact_scope_and_unchanged_source(tmp_path, fault):
    import hashlib
    target = tmp_path / 'QA 기록.txt'
    before = 'x = [1, 2] 🙂\r\n두 번째 줄\r\n'.encode('utf-8')
    target.write_bytes(before)
    first = before.decode('utf-8').splitlines(keepends=True)[0]
    payload = {'path': str(target), 'content': first, 'start_line': 1, 'end_line': 1,
               'sha256': hashlib.sha256(before).hexdigest(), 'truncated': False}
    result = ToolRunResult.successful(tool_name='filesystem_read_file', raw_output='',
                                     evidence=[Evidence('file_content', 'actual', dict(payload))])
    outcome = SimpleNamespace(status='completed', tool_result=result, response=first)
    if fault == 'extra_line':
        payload['content'] = before.decode('utf-8')
    elif fault == 'wrong_bounds':
        payload['end_line'] = 2
    elif fault == 'wrong_hash':
        payload['sha256'] = '0' * 64
    elif fault == 'truncated':
        payload['truncated'] = True
    elif fault == 'no_evidence':
        result.evidence = []
    elif fault == 'wrong_tool':
        result.tool_name = 'read_file'
    elif fault == 'failed':
        outcome.status = 'unverified'
    elif fault == 'changed_file':
        target.write_bytes(b'changed')
    elif fault == 'extra_display':
        outcome.response = before.decode('utf-8')
    elif fault == 'missing_display':
        outcome.response = 'completed'
    elif fault == 'wrong_path':
        payload['path'] = str(tmp_path / 'another.txt')
    elif fault == 'missing_path':
        del payload['path']
    elif fault == 'wrong_evidence_kind':
        result.evidence = [Evidence('tool_error', 'not a read', dict(payload))]
    elif fault and fault.startswith('wrong_evidence_'):
        data = dict(payload)
        key = {'wrong_evidence_path': 'path', 'wrong_evidence_hash': 'sha256',
               'wrong_evidence_content': 'content', 'wrong_evidence_bounds': 'end_line'}[fault]
        data[key] = 2 if key == 'end_line' else 'wrong'
        result.evidence = [Evidence('file_content', 'actual', data)]
    elif fault == 'failed_receipt':
        result.status = ToolRunStatus.FAILED
    if fault in {'extra_line', 'wrong_bounds', 'wrong_hash', 'truncated', 'wrong_path', 'missing_path'}:
        result.evidence = [Evidence('file_content', 'actual', dict(payload))]
    result.raw_output = json.dumps(payload, ensure_ascii=False)
    assert qa.exact_fixture_read(outcome, target, before) is (fault is None)


@pytest.mark.parametrize('tool_name,tool_input', [
    ('write_file', {'filename': 'QA 기록.txt', 'start_line': 1, 'end_line': 1}),
    ('filesystem_read_file', {'filename': 'other.txt', 'start_line': 1, 'end_line': 1}),
    ('filesystem_read_file', {'filename': 'QA 기록.txt', 'start_line': 1, 'end_line': 2}),
    ('filesystem_read_file', {'filename': 'QA 기록.txt', 'start_line': True, 'end_line': True}),
    ('filesystem_read_file', {'filename': 'QA 기록.txt'}),
    ('filesystem_read_file', None),
])
def test_read_qa_guard_covers_every_dispatch_including_replans(tmp_path, tool_name, tool_input):
    calls = []
    executor = SimpleNamespace(tool_executor=SimpleNamespace(
        execute_tool=lambda name, values: calls.append((name, values)) or 'read'))
    target = tmp_path / 'QA 기록.txt'
    qa.restrict_fixture_reads(executor, target)
    allowed = {'filename': target.name, 'start_line': 1, 'end_line': 1}
    assert executor.tool_executor.execute_tool('filesystem_read_file', allowed) == 'read'
    with pytest.raises(ToolCancelledError):
        executor.tool_executor.execute_tool(tool_name, tool_input)
    assert calls == [('filesystem_read_file', allowed)]


@pytest.mark.parametrize('confidence', [0.0, .2, .7, .849, .85, .95, 1.0])
def test_semantic_qa_unsupported_requires_a_limited_response_not_confidence(confidence):
    decision = SemanticDecision('synthetic', relation='new', operation='unknown',
                                grounded=False, tool_names=(), needs_clarification=False,
                                confidence=confidence, reason='no_supported_tool',
                                dialogue_response='현재 연결된 도구로는 요청한 작업을 수행할 수 없습니다.')
    assert qa.semantic_fixture_passed(decision, 'unsupported')


@pytest.mark.parametrize('fault', [
    {'reason': ''},
    {'reason': 'semantic_discovery_invalid'},
    {'reason': 'semantic_schema_or_confidence_invalid'},
    {'relation': 'unknown'},
    {'relation': 'conversation'},
    {'operation': 'read'},
    {'operation': 'conversation'},
    {'grounded': True},
    {'tool_names': ('filesystem_read_file',)},
    {'needs_clarification': True},
    {'dialogue_response': ''},
    {'dialogue_response': ' \n\t '},
])
def test_semantic_qa_does_not_count_unresolved_or_executable_requests_as_unsupported(fault):
    from dataclasses import replace
    decision = SemanticDecision('synthetic', relation='new', operation='unknown',
                                grounded=False, tool_names=(), needs_clarification=False,
                                confidence=.99, reason='no_supported_tool',
                                dialogue_response='현재 연결된 도구로는 요청한 작업을 수행할 수 없습니다.')
    assert not qa.semantic_fixture_passed(replace(decision, **fault), 'unsupported')


@pytest.mark.parametrize('expected,operation,tool,slots', [
    ('read', 'read', 'filesystem_read_file', {'filename': 'QA 기록.txt', 'start_line': 1, 'end_line': 1}),
    ('inbox', 'read', 'mail_list_inbox', {'limit': 5, 'unread_only': True}),
    ('site_search', 'execute', 'browser_site_search', {'provider': 'youtube', 'query': '빗소리'}),
])
def test_semantic_qa_preserves_exact_tool_operation_and_constraints(expected, operation, tool, slots):
    from dataclasses import replace
    decision = SemanticDecision('synthetic', grounded=True, relation='new', operation=operation,
                                tool_names=(tool,), slots=slots)
    assert qa.semantic_fixture_passed(decision, expected)
    for wrong in (replace(decision, grounded=False), replace(decision, operation='unknown'),
                  replace(decision, tool_names=('another_tool',)),
                  replace(decision, needs_clarification=True), replace(decision, slots={}),
                  replace(decision, slots={**slots, next(iter(slots)): None})):
        assert not qa.semantic_fixture_passed(wrong, expected)
