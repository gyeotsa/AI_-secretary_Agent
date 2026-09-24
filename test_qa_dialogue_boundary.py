"""Offline contracts for the opt-in live-model QA harness; no model is called."""
from contextlib import contextmanager, nullcontext
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import qa_dialogue_boundary as qa


def test_log_extraction_preserves_payload_and_excludes_duplicate_debug_input():
    source = "문제 설명\r\n  if value:\r\n      return ' x '  \r\n"
    log = ("startup\r\n[DEBUG] _on_user_input called with: " + source + "\r\n"
           "[DEBUG] _process_ai called with: repeated input\r\n"
           "[DEBUG] _on_user_input called with: later input\r\n"
           "[DEBUG] _process_ai called with: later duplicate\r\n")
    assert qa.extract_first_request(log) == source


@pytest.mark.parametrize("log", [
    "no markers", "[DEBUG] _on_user_input called with: missing end",
    "[DEBUG] _on_user_input called with: \n[DEBUG] _process_ai called with: blank",
])
def test_missing_or_empty_log_is_not_a_synthetic_success(log):
    with pytest.raises(ValueError):
        qa.extract_first_request(log)


def test_log_payload_bound_is_checked(monkeypatch):
    monkeypatch.setattr(qa, "MAX_REQUEST_CHARS", 4)
    with pytest.raises(ValueError):
        qa.extract_first_request("[DEBUG] _on_user_input called with: 12345\n[DEBUG] _process_ai")


def test_log_file_is_bounded_before_decode(tmp_path, monkeypatch):
    path = tmp_path / "log.txt"
    path.write_bytes(b"x" * 20)
    monkeypatch.setattr(qa, "MAX_LOG_BYTES", 10)
    with pytest.raises(ValueError, match="log_too_large"):
        qa.read_log_request(path)


def test_cases_include_realistic_failed_assistant_context_and_expected_kinds():
    cases = qa.build_cases("private supplied problem")
    assert tuple(case.name for case in cases) == qa.CASE_NAMES
    assert [case.answer_kind for case in cases] == ["code", "code", "conversation", "conversation"]
    assert cases[1].history[0]["content"] == "private supplied problem"
    assert cases[1].history[1]["role"] == "assistant"
    assert cases[2].history == ()
    assert cases[3].history[2]["content"] == "이 문제를 풀어봐"


@pytest.mark.parametrize("url", [
    "https://example.com:11434", "http://192.168.1.10:11434",
    "http://127.0.0.1:11434/api/chat", "http://user:secret@localhost:11434",
    "http://localhost:11434?key=secret", "file:///tmp/ollama",
    "http://localhost.example.com:11434", "http://[2001:db8::1]:11434",
])
def test_remote_or_ambiguous_endpoints_are_rejected_even_with_ipv4(url):
    with pytest.raises(ValueError):
        qa.loopback_endpoint(url, ipv4=True)


def test_only_verified_loopback_can_be_rewritten_to_ipv4(monkeypatch):
    monkeypatch.setattr(qa.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("127.0.0.1", 11434)), (23, 1, 6, "", ("::1", 11434, 0, 0)),
    ])
    assert qa.loopback_endpoint("http://localhost:11434", ipv4=True) == "http://127.0.0.1:11434"
    assert qa.loopback_endpoint("http://[::1]:11434/") == "http://[::1]:11434"
    monkeypatch.setattr(qa.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("203.0.113.1", 11434)),
    ])
    with pytest.raises(ValueError):
        qa.loopback_endpoint("http://localhost:11434", ipv4=True)


def test_environment_is_process_only_local_and_disables_dotenv_and_devices(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    sandbox = tmp_path / "isolated"
    overrides = qa.isolated_environment(sandbox, tmp_path, "http://127.0.0.1:11434")
    for key in ("DB_PATH", "LEARNING_DB_PATH", "OBSIDIAN_VAULT_PATH", "ALLOWED_PATHS"):
        assert Path(overrides[key]).is_relative_to(sandbox)
    assert overrides["PYTHON_DOTENV_DISABLED"] == "1"
    assert overrides["LLM_PROVIDER"] == "ollama"
    assert overrides["JARVIS_DISABLE_MIC_AUTOSTART"] == "1"
    assert overrides["JARVIS_DISABLE_CAMERA_AUTOSTART"] == "1"
    assert overrides["RAG_DEVICE"] == "cpu"
    assert overrides["HTTP_PROXY"] == overrides["HTTPS_PROXY"] == ""
    assert qa.os.environ["LLM_PROVIDER"] == "anthropic"


def _decision(**changes):
    return SimpleNamespace(**{
        "is_grounded_conversation": True, "answer_kind": "code",
        "source": "semantic_response_mode", "confidence": .95, "reason": "",
        "relation": "conversation", "operation": "conversation", "grounded": True,
        "needs_clarification": False, "tool_names": (), **changes,
    })


def test_valid_answer_bypasses_lexical_condition_without_hiding_it():
    case = qa.build_cases("private problem")[0]
    scope = SimpleNamespace(discussion=False, negated=False, conditional=True)
    result = qa.decision_record(case, _decision(), scope)
    assert result["passed"]
    assert result["scope_conditional"]
    assert not result["execution_guard_applies"]
    assert "private problem" not in json.dumps(result)


@pytest.mark.parametrize("source", ["grounded_coding_problem", "grounded_code_feedback"])
def test_grounded_coding_fast_path_preserves_strict_answer_contract(source):
    case = qa.build_cases("private problem")[0]
    scope = SimpleNamespace(discussion=False, negated=False, conditional=True)
    result = qa.decision_record(case, _decision(source=source, confidence=1.0), scope)
    assert result["passed"] and result["source"] == source
    assert not qa.decision_record(case, _decision(source=source, answer_kind="conversation"), scope)["passed"]


@pytest.mark.parametrize("change", [
    {"is_grounded_conversation": False}, {"answer_kind": "conversation"},
    {"source": "semantic_discovery"}, {"confidence": .7},
    {"confidence": True}, {"confidence": float("nan")},
    {"grounded": False}, {"needs_clarification": True},
    {"tool_names": ("send_message",)}, {"operation": "execute"},
])
def test_failed_classification_is_not_a_smoke_pass(change):
    case = qa.build_cases("private problem")[0]
    scope = SimpleNamespace(discussion=False, negated=False, conditional=True)
    assert not qa.decision_record(case, _decision(**change), scope)["passed"]


def test_safe_metadata_drops_unrecognized_model_text():
    case = qa.build_cases("secret")[0]
    scope = SimpleNamespace(discussion=False, negated=False, conditional=False)
    result = qa.decision_record(case, _decision(
        source="secret-source", reason="secret-reason", relation="secret-relation",
        operation="secret-operation", answer_kind="secret-answer"), scope)
    assert "secret" not in json.dumps(result)
    assert qa.safe_reason("semantic_interpretation_failed:Error:timeout") == "semantic_interpretation_failed:timeout"
    assert qa.safe_reason("semantic_interpretation_failed:Error:secret") == "semantic_interpretation_failed:other"


def test_app_logs_are_discarded_while_shared_trace_has_separate_sink(monkeypatch):
    from scripts import qa_common_runtime
    output = io.StringIO()

    @contextmanager
    def fake_trace(enabled):
        assert enabled
        qa_common_runtime.print('{"event":"model_trace","phase":"start"}', flush=True)
        yield

    monkeypatch.setattr(qa_common_runtime, "trace_model_calls", fake_trace)
    assert not hasattr(qa_common_runtime, "print")
    with qa.quiet_runtime(output):
        print("private problem or model response")
        print("private exception detail", file=qa.sys.stderr)
    assert output.getvalue() == '{"event":"model_trace","phase":"start"}\n'
    assert not hasattr(qa_common_runtime, "print")


@pytest.mark.parametrize("result,metadata", [
    (("answer", .0, "conversation"), {"valid": True, "mode": "answer", "confidence": .0,
                                      "answer_kind": "conversation"}),
    (("action", .95, "conversation"), {"valid": True, "mode": "action", "confidence": .95,
                                        "answer_kind": "conversation"}),
    (None, {"valid": False}),
    (("private output", .9, "conversation"), {"valid": False}),
    (("answer", float("nan"), "conversation"), {"valid": False}),
    (("answer", True, "conversation"), {"valid": False}),
    (("action", .9, "code"), {"valid": False}),
])
def test_response_mode_trace_has_only_validated_metadata_and_restores_method(result, metadata):
    records = []

    class Interpreter:
        def _classify_response_mode(self, *_args):
            return result

    interpreter = Interpreter()
    with qa.trace_response_mode(interpreter, "capability", records.append):
        assert interpreter._classify_response_mode("private input", "private history", {}) is result
    assert records == [{"event": "response_mode_trace", "case": "capability", **metadata}]
    assert "_classify_response_mode" not in vars(interpreter)
    assert "private" not in json.dumps(records)


def test_failed_cleanup_reports_retained_temp_without_masking_original_result(tmp_path, monkeypatch):
    records = []

    class LockedTemporaryDirectory:
        name = str(tmp_path)

        def __init__(self, **kwargs):
            assert kwargs == {"prefix": "anis-dialogue-qa-", "ignore_cleanup_errors": True}

        def cleanup(self):
            raise PermissionError("private exception details")

    monkeypatch.setattr(qa.tempfile, "TemporaryDirectory", LockedTemporaryDirectory)
    with pytest.raises(ValueError, match="original_failure"):
        with qa.disposable_workspace(records.append) as sandbox:
            assert sandbox == tmp_path
            raise ValueError("original_failure")
    assert records == [{"event": "qa_cleanup_incomplete", "path": str(tmp_path), "retained": True}]


def test_invalid_endpoint_exits_nonzero_without_app_import_or_content(capsys):
    assert qa.main(["--ollama-url", "https://example.com:11434"]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["event"] == "dialogue_boundary_error"
    assert result["passed"] is False
    assert "example.com" not in output


@pytest.mark.parametrize("mode,classification_passes,expected_exit", [
    ("interpret-only", True, 0), ("interpret-only", False, 1), ("answer", True, 0),
])
def test_runner_checks_classification_and_never_grants_tools(
    monkeypatch, tmp_path, mode, classification_passes, expected_exit,
):
    """Fake only the model/runtime for testing the harness, never a live QA run."""
    records = []
    calls = []

    class LocalClient:
        base_url = "http://127.0.0.1:11434"

    class Context:
        cancelled = False

        def __init__(self, *_args):
            pass

        def cancel(self):
            self.cancelled = True

    def interpret(request, *, history, allowed_tools):
        calls.append("interpret")
        assert allowed_tools is None
        return _decision(is_grounded_conversation=classification_passes)

    def execute(request, session, history, *, allowed_tool_names, turn_context):
        calls.append("answer")
        assert allowed_tool_names == []
        return SimpleNamespace(tool_results=[], tool_result=None, status="completed",
            response="synthetic answer", grounded_conversation=True, response_truncated=False,
            unsupported_activity_claim=False, unverified_completion_claim=False)

    registry = SimpleNamespace(get_capabilities=lambda: ["read", "write"],
                               shutdown=lambda: calls.append("shutdown"))
    executor = SimpleNamespace(semantic_interpreter=SimpleNamespace(
        classify_response_mode=True, interpret=interpret, _classify_response_mode=lambda *_args: None),
        execute_turn=execute, tool_executor=SimpleNamespace(plugin_registry=registry))
    modules = {
        "core.assistant_settings": SimpleNamespace(get_assistant_settings=lambda: SimpleNamespace(
            set_tts_enabled=lambda _value: None, set=lambda *_args: None)),
        "core.executor": SimpleNamespace(Executor=lambda: executor),
        "core.llm": SimpleNamespace(OllamaClient=LocalClient, get_llm_client=lambda _role: LocalClient()),
        "core.turn_context": SimpleNamespace(TurnExecutionContext=Context, bind_turn_context=lambda _: nullcontext()),
        "core.utterance_scope": SimpleNamespace(analyze_utterance_scope=lambda *_args: SimpleNamespace(
            discussion=False, conditional=True, negated=False)),
        "core.workspace": SimpleNamespace(get_workspace_manager=lambda: SimpleNamespace(set_workspace=lambda _: True)),
    }
    for name, module in modules.items():
        monkeypatch.setitem(qa.sys.modules, name, module)
    args = SimpleNamespace(mode=mode, log_file=Path("private-log") if mode == "answer" else None,
                           case_timeout_seconds=5)
    assert qa._run(args, [qa.build_cases("private payload")[0]], tmp_path, records.append) == expected_exit
    assert calls == (["interpret", "answer", "shutdown"] if mode == "answer" else ["interpret", "shutdown"])
    assert records[-1]["failed"] == (0 if classification_passes else 1)
    assert "private payload" not in json.dumps(records)
    if mode == "answer":
        assert records[1]["response_omitted"]
        assert "synthetic answer" not in json.dumps(records)
