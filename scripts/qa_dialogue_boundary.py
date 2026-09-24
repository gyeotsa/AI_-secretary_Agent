"""Reproduce answer/action classification with real, local-only Ollama models.

Default: four interpretation-only cases against the real full tool registry.
No tools or generated code are executed. --answer also exercises Executor with
an empty tool allowlist; response text is printed only for synthetic cases.
Application logs are suppressed because they may contain supplied log text.
Only metadata-only model traces and explicit QA records reach stdout.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit


SYNTHETIC_PROBLEM = """문제 설명
정수 배열 values가 주어집니다. 각 원소가 0보다 작으면 부호를 바꾸고,
0이면 그대로 둡니다. 같은 수가 있으면 한 번만 남기고 오름차순으로 정렬합니다.
제한사항
배열 길이는 0 이상 100 이하이고 원소는 -100 이상 100 이하입니다.
입출력 예
values = [-2, 0, 2, -1]이면 결과는 [0, 1, 2]입니다.
빈 배열이 들어오면 빈 배열을 반환합니다.
아래 Python solution 함수를 완성해서 정답 코드를 채팅으로 보여줘.
def solution(values):
    answer = []
    return answer
"""
CASE_NAMES = ("problem", "follow_up", "capability", "frustration")
ANSWER_SOURCES = frozenset({
    "semantic_response_mode", "grounded_coding_problem", "grounded_code_feedback",
    "deterministic_conversation",
})
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_REQUEST_CHARS = 100_000
_REASONS = frozenset({
    "", "semantic_model_unavailable", "semantic_response_mode_invalid",
    "semantic_response_mode_uncertain", "semantic_discovery_invalid", "no_supported_tool",
    "semantic_schema_not_object", "semantic_schema_or_confidence_invalid",
    "invalid_clarification_flag", "invalid_clarification_question", "semantic_relation_unresolved",
    "approval_requires_explicit_confirmation", "invalid_control_scope",
    "cancellation_scope_requires_confirmation", "invalid_tool_names", "intent_tool_mismatch",
    "unknown_intent", "unknown_or_out_of_scope_tool", "invalid_slots", "conversation_cannot_execute",
    "invalid_action_operation", "continuation_without_user_context", "unknown_slot",
    "slot_type_invalid", "operation_tool_mismatch", "read_request_cannot_mutate",
    "filename_not_verified_candidate", "message_literal_changed",
})


@dataclass(frozen=True)
class Case:
    name: str
    request: str
    history: tuple[dict, ...]
    answer_kind: str


def extract_first_request(log_text: str) -> str:
    """Extract precisely the first multiline input, not repeated AI debug text."""
    start = re.search(r"^\[DEBUG\]\s+_on_user_input called with:[ \t]?", log_text, re.M)
    if start is None:
        raise ValueError("input_marker_missing")
    end = re.search(r"^\[DEBUG\]\s+_process_ai\b", log_text[start.end():], re.M)
    if end is None:
        raise ValueError("process_marker_missing")
    request = log_text[start.end():start.end() + end.start()]
    # print() adds one newline; preserve all payload whitespace before it.
    if request.endswith("\r\n"):
        request = request[:-2]
    elif request.endswith("\n"):
        request = request[:-1]
    if not request.strip() or len(request) > MAX_REQUEST_CHARS:
        raise ValueError("input_empty_or_too_large")
    return request


def read_log_request(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise ValueError("log_too_large")
    return extract_first_request(data.decode("utf-8-sig"))


def build_cases(problem: str) -> tuple[Case, ...]:
    prior = ({"role": "user", "content": problem}, {"role": "assistant", "content":
        "문제에 적힌 조건을 먼저 확인할까요, 아니면 지금 실행하라는 뜻인가요?"})
    frustrated = (*prior, {"role": "user", "content": "이 문제를 풀어봐"},
                  {"role": "assistant", "content": "이 요청을 수행할 수 있는 도구가 없습니다."})
    return (
        Case("problem", problem, (), "code"),
        Case("follow_up", "이 문제를 풀어봐", prior, "code"),
        Case("capability", "코딩테스트 문제 풀어줄 수 있어?", (), "conversation"),
        Case("frustration", "말을 전혀 이해 못하는구나?", frustrated, "conversation"),
    )


def loopback_endpoint(value: str, *, ipv4: bool = False) -> str:
    """Reject credentials, remote hosts and ambiguous URLs before any model call."""
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or not parsed.hostname):
        raise ValueError("local_ollama_url_required")
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host == "localhost":
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        if not addresses or not all(ipaddress.ip_address(item).is_loopback for item in addresses):
            raise ValueError("localhost_not_loopback")
    else:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("remote_ollama_forbidden")
    if ipv4:
        host = "127.0.0.1"
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit((parsed.scheme, f"{authority}:{port}", "", "", ""))


def isolated_environment(sandbox: Path, root: Path, endpoint: str) -> dict[str, str]:
    """All overrides are process-only and must be set before application imports."""
    result = {
        "DB_PATH": str(sandbox / "data/assistant.db"),
        "LEARNING_DB_PATH": str(sandbox / "data/learning.db"),
        "OBSIDIAN_VAULT_PATH": str(sandbox / "vault"),
        "ALLOWED_PATHS": str(sandbox),
        "LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": endpoint,
        "ANTHROPIC_API_KEY": "", "HYBRID_CLAUDE_ROLES": "",
        "PYTHON_DOTENV_DISABLED": "1", "PYTHONIOENCODING": "utf-8",
        "JARVIS_DISABLE_MIC_AUTOSTART": "1", "JARVIS_DISABLE_CAMERA_AUTOSTART": "1",
        "JARVIS_SKIP_FIRST_RUN_WIZARD": "1", "QT_QPA_PLATFORM": "offscreen",
        "RAG_DEVICE": "cpu", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "RAG_EMBEDDING_MODEL_PATH": str(root / "data/models/bge-m3"),
        "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
    }
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        result[key] = ""
    return result


def safe_reason(reason: str) -> str:
    if reason in _REASONS:
        return reason
    if str(reason).startswith("semantic_interpretation_failed:"):
        suffix = str(reason).rsplit(":", 1)[-1]
        return "semantic_interpretation_failed:" + (
            suffix if suffix in {"timeout", "connection", "provider", "truncated_output", ""} else "other")
    return "other"


def decision_record(case: Case, decision, scope) -> dict:
    """No request, slots, question, condition text or exception detail is emitted."""
    confidence = decision.confidence
    grounded_answer = bool(decision.is_grounded_conversation)
    passed = (grounded_answer and decision.answer_kind == case.answer_kind
              and decision.source in ANSWER_SOURCES
              and decision.grounded and not decision.needs_clarification
              and not decision.tool_names and decision.operation == "conversation"
              and type(confidence) in {int, float} and math.isfinite(confidence)
              and .85 <= confidence <= 1)
    return {
        "event": "dialogue_boundary", "case": case.name, "passed": passed,
        "source": decision.source if decision.source in {
            "unresolved", "explicit_control", "literal_reply", "model",
            "semantic_discovery", *ANSWER_SOURCES} else "other",
        "reason": safe_reason(decision.reason),
        "relation": decision.relation if decision.relation in {
            "new", "continue", "correct", "cancel", "approve", "conversation", "unknown"} else "other",
        "operation": decision.operation if decision.operation in {
            "read", "change", "execute", "external_send", "control", "conversation", "unknown"} else "other",
        "answer_kind": decision.answer_kind if decision.answer_kind in {
            "code", "reasoning", "conversation"} else "other",
        "expected_answer_kind": case.answer_kind, "grounded": bool(decision.grounded),
        "needs_clarification": bool(decision.needs_clarification),
        "tool_count": len(decision.tool_names),
        "scope_discussion": bool(scope.discussion), "scope_negated": bool(scope.negated),
        "scope_conditional": bool(scope.conditional),
        "execution_guard_applies": bool(not grounded_answer and (scope.negated or scope.conditional)),
    }


@contextmanager
def trace_response_mode(interpreter, case_name, emit):
    """Trace validated classifier metadata, never raw prompts or model output."""
    original = interpreter._classify_response_mode
    previous = vars(interpreter).get("_classify_response_mode")

    def classify(*args, **kwargs):
        result = original(*args, **kwargs)
        record = {"event": "response_mode_trace", "case": case_name, "valid": False}
        if isinstance(result, tuple) and len(result) == 3:
            mode, confidence, kind = result
            if (isinstance(mode, str) and mode in {"answer", "action", "uncertain"}
                    and type(confidence) in {int, float} and math.isfinite(confidence)
                    and 0 <= confidence <= 1 and isinstance(kind, str)
                    and kind in {"conversation", "code", "reasoning"}
                    and (mode == "answer" or kind == "conversation")):
                record.update(valid=True, mode=mode, confidence=confidence, answer_kind=kind)
        emit(record)
        return result

    interpreter._classify_response_mode = classify
    try:
        yield
    finally:
        if previous is None:
            del interpreter._classify_response_mode
        else:
            interpreter._classify_response_mode = previous


@contextmanager
def disposable_workspace(emit):
    """A Windows DB handle must not turn a completed QA result into a failure."""
    temporary = tempfile.TemporaryDirectory(prefix="anis-dialogue-qa-", ignore_cleanup_errors=True)
    sandbox = Path(temporary.name).resolve()
    try:
        yield sandbox
    finally:
        try:
            temporary.cleanup()
        except OSError:
            pass
        if sandbox.exists():
            emit({"event": "qa_cleanup_incomplete", "path": str(sandbox), "retained": True})


@contextmanager
def quiet_runtime(output):
    """Keep the shared safe trace sink separate from content-bearing app logs."""
    from scripts import qa_common_runtime

    missing = object()
    original = getattr(qa_common_runtime, "print", missing)

    def trace_print(*args, **kwargs):
        kwargs["file"] = output
        print(*args, **kwargs)

    qa_common_runtime.print = trace_print
    try:
        with open(os.devnull, "w", encoding="utf-8") as discarded:
            with redirect_stdout(discarded), redirect_stderr(discarded):
                with qa_common_runtime.trace_model_calls(True):
                    yield
    finally:
        if original is missing:
            del qa_common_runtime.print
        else:
            qa_common_runtime.print = original


def _run(args, cases, sandbox, emit) -> int:
    from core.assistant_settings import get_assistant_settings
    from core.executor import Executor
    from core.llm import OllamaClient, get_llm_client
    from core.turn_context import TurnExecutionContext, bind_turn_context
    from core.utterance_scope import analyze_utterance_scope
    from core.workspace import get_workspace_manager

    settings = get_assistant_settings()
    settings.set_tts_enabled(False)
    settings.set("gesture_camera_enabled", "false")
    workspace = sandbox / "workspace"
    workspace.mkdir()
    if not get_workspace_manager().set_workspace(str(workspace)):
        raise RuntimeError("workspace_initialization_failed")
    # Validate every role that answer generation/review can select, not only
    # the initial interpreter client. Never use cloud fallbacks in this QA.
    for role in ("conversation", "reasoning", "code", "planning", "tool_selection"):
        client = get_llm_client(role)
        if not isinstance(client, OllamaClient):
            raise RuntimeError("nonlocal_client_forbidden")
        loopback_endpoint(client.base_url)
    executor = Executor()
    passed = 0
    completed = 0
    try:
        if not executor.semantic_interpreter.classify_response_mode:
            raise RuntimeError("response_mode_classifier_required")
        emit({"event": "dialogue_boundary_ready", "tool_catalog_count": len(
            executor.tool_executor.plugin_registry.get_capabilities()), "mode": args.mode,
            "input_source": "supplied_log" if args.log_file else "synthetic",
            "case_count": len(cases), "case_timeout_seconds": args.case_timeout_seconds})
        for case in cases:
            context = TurnExecutionContext(f"qa-{case.name}", f"qa-{case.name}", str(workspace))
            timer = threading.Timer(args.case_timeout_seconds, context.cancel)
            timer.daemon = True
            started = time.monotonic()
            timer.start()
            abort = False
            try:
                with bind_turn_context(context), trace_response_mode(
                        executor.semantic_interpreter, case.name, emit):
                    scope = analyze_utterance_scope(case.request, list(case.history))
                    # None intentionally means the entire real registry. An
                    # empty catalog here would make answer classification trivial.
                    decision = executor.semantic_interpreter.interpret(
                        case.request, history=case.history, allowed_tools=None)
                    record = decision_record(case, decision, scope)
                    if args.mode == "answer" and record["passed"]:
                        before = {str(p.relative_to(workspace)): p.read_bytes()
                                  for p in workspace.rglob("*") if p.is_file()}
                        outcome = executor.execute_turn(
                            case.request, f"qa-{case.name}", list(case.history),
                            allowed_tool_names=[], turn_context=context)
                        after = {str(p.relative_to(workspace)): p.read_bytes()
                                 for p in workspace.rglob("*") if p.is_file()}
                        safe = before == after and not outcome.tool_results and outcome.tool_result is None
                        record.update({"answer_status": outcome.status,
                            "answer_nonempty": bool(outcome.response.strip()),
                            "answer_grounded_conversation": outcome.grounded_conversation,
                            "no_tools_or_workspace_changes": safe,
                            "response_truncated": bool(outcome.response_truncated),
                            "unsupported_activity_claim": bool(outcome.unsupported_activity_claim)})
                        record["passed"] = bool(safe and outcome.status == "completed"
                            and outcome.grounded_conversation and outcome.response.strip()
                            and not outcome.response_truncated and not outcome.unsupported_activity_claim
                            and not outcome.unverified_completion_claim)
                        if args.log_file:
                            record["response_omitted"] = True
                        else:
                            record["response"] = outcome.response
                    abort = decision.reason.startswith("semantic_interpretation_failed:")
            except Exception as exc:
                record = {"event": "dialogue_boundary", "case": case.name, "passed": False,
                          "error_type": type(exc).__name__, "cancelled": context.cancelled}
                abort = True
            finally:
                timer.cancel()
                timer.join()
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if context.cancelled:
                record["passed"] = False
                record["cancelled"] = True
                abort = True
            emit(record)
            completed += 1
            passed += bool(record["passed"])
            if abort:
                break
        emit({"event": "dialogue_boundary_summary", "passed": passed,
              "failed": completed - passed, "not_run": len(cases) - completed,
              "total": len(cases), "mode": args.mode,
              "note": "Classification/runtime sample only; not independent answer-correctness proof."})
        return 0 if passed == len(cases) else 1
    finally:
        executor.tool_executor.plugin_registry.shutdown()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interpret-only", dest="mode", action="store_const", const="interpret-only")
    mode.add_argument("--answer", dest="mode", action="store_const", const="answer")
    parser.set_defaults(mode="interpret-only")
    parser.add_argument("--log-file", type=Path, help="UTF-8 pasted console log; payload is never printed")
    parser.add_argument("--case", action="append", choices=CASE_NAMES,
                        help="Run only selected cases (repeatable)")
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"))
    parser.add_argument("--ipv4", action="store_true", help="Use 127.0.0.1 for this process only")
    parser.add_argument("--case-timeout-seconds", type=int, default=180,
                        help="Cooperative per-case cancellation budget (5..600; default 180)")
    args = parser.parse_args(argv)
    if not 5 <= args.case_timeout_seconds <= 600:
        parser.error("--case-timeout-seconds must be between 5 and 600")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    output = sys.stdout

    def emit(record):
        print(json.dumps(record, ensure_ascii=False, allow_nan=False), file=output, flush=True)

    try:
        endpoint = loopback_endpoint(args.ollama_url, ipv4=args.ipv4)
        problem = read_log_request(args.log_file.resolve()) if args.log_file else SYNTHETIC_PROBLEM
        cases = [case for case in build_cases(problem) if not args.case or case.name in args.case]
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        # This script is a fresh-process entry point. Refuse an already imported
        # app whose singletons could reference the user's real databases.
        if "config" in sys.modules or "core.executor" in sys.modules:
            raise RuntimeError("fresh_process_required")
        original_cwd = Path.cwd()
        with disposable_workspace(emit) as sandbox:
            overrides = isolated_environment(sandbox, root, endpoint)
            previous = {key: os.environ.get(key) for key in overrides}
            os.environ.update(overrides)
            os.chdir(sandbox)
            emit({"event": "qa_workspace", "path": str(sandbox), "disposable": True})
            try:
                with quiet_runtime(output):
                    return _run(args, cases, sandbox, emit)
            finally:
                os.chdir(original_cwd)
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
    except Exception as exc:
        emit({"event": "dialogue_boundary_error", "passed": False, "error_type": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
