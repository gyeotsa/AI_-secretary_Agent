"""Run the real Anis application in a disposable, local-only QA workspace.

No user databases are copied. Microphone, camera and speech are disabled for
this diagnostic process only. --gui exposes the actual window for manual QA.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import itertools
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time


def _model_trace_labels() -> dict:
    """Read only fixed labels from the immediate LLM caller, never its input.

    Turn context has no model role. Inspecting this one known call site avoids
    patching production client methods or guessing a role from the model name.
    Do not retain the frame (and its prompt) across the HTTP request.
    """
    labels = {"role": "unknown", "call_kind": "unknown"}
    frame = sys._getframe(2)
    try:
        if frame.f_globals.get("__name__") != "core.llm":
            return labels
        name = frame.f_code.co_name
        if name not in {"_chat", "chat_with_tools"}:
            return labels
        profile = getattr(frame.f_locals.get("self"), "profile", None)
        role = getattr(profile, "role", None)
        if role in {"conversation", "planning", "reasoning", "tool_selection",
                    "code", "document", "vision", "image_editing", "mockup_design",
                    "style_vision", "visual_critic", "design_planning",
                    "subject_analysis", "rendering"}:
            labels["role"] = role
        labels["call_kind"] = ("tools" if name == "chat_with_tools" else
                               "prose" if frame.f_locals.get("prose") is True else "structured")
        return labels
    finally:
        del frame


@contextmanager
def trace_model_calls(enabled: bool = False):
    """Opt-in, bounded metadata-only tracing for this disposable QA process.

    Each call emits one start and one end record. Never log prompts, generated
    text, schemas, tools, headers, URLs, exception messages, or turn identities.
    The original response/exception and request arguments remain untouched.
    """
    if not enabled:
        yield
        return
    from core import llm
    from core.turn_context import current_turn_context

    original = llm.post_json
    sequence = itertools.count(1)
    output_lock = threading.Lock()

    def emit(record):
        try:
            with output_lock:
                print(json.dumps(record, ensure_ascii=False, allow_nan=False), flush=True)
        except Exception:
            # Diagnostics must not turn a successful/cancelled call into a
            # different failure when stdout is unavailable.
            pass

    def number(value):
        return value if (type(value) in {int, float} and 0 <= value <= 10**18
                         and math.isfinite(value)) else None

    def traced_post(*args, **kwargs):
        payload = kwargs.get("json")
        payload = payload if isinstance(payload, dict) else {}
        options = payload.get("options")
        options = options if isinstance(options, dict) else {}
        model = payload.get("model")
        record = {
            "event": "model_trace", "call_id": next(sequence),
            "model": model[:128] if isinstance(model, str) else "unknown",
            **_model_trace_labels(),
            "thread_id": threading.get_ident(),
            "turn_bound": current_turn_context() is not None,
            "schema_present": "format" in payload,
            "num_ctx": number(options.get("num_ctx")),
            "num_predict": number(options.get("num_predict")),
        }
        emit({**record, "phase": "start"})
        started = time.monotonic()
        try:
            response = original(*args, **kwargs)
        except BaseException as exc:
            emit({**record, "phase": "end", "outcome": "error",
                  "elapsed_seconds": round(time.monotonic() - started, 3),
                  "error_type": type(exc).__name__[:80]})
            raise
        finished = {**record, "phase": "end", "outcome": "response",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "http_status": number(getattr(response, "status_code", None))}
        try:
            result = response.json()
            if isinstance(result, dict):
                for key in ("prompt_eval_count", "prompt_eval_duration", "eval_count",
                            "eval_duration", "total_duration", "load_duration"):
                    finished[key] = number(result.get(key))
                reason = result.get("done_reason")
                finished["done_reason"] = reason if reason in {"stop", "length", "load", "unload"} else None
                finished["done"] = result.get("done") if type(result.get("done")) is bool else None
                finished["provider_error_present"] = "error" in result
        except Exception as exc:
            finished["metadata_error_type"] = type(exc).__name__[:80]
        emit(finished)
        return response

    llm.post_json = traced_post
    try:
        yield
    finally:
        llm.post_json = original


def check_local_model_cancellation() -> int:
    """Measure cancellation after the real local HTTP request body was sent."""
    import threading
    from urllib.parse import urlsplit
    import httpx
    from core.llm import OllamaClient, get_llm_client
    from core.plugin import ToolCancelledError
    from core.turn_context import TurnExecutionContext, bind_turn_context

    client = get_llm_client("conversation")
    if not isinstance(client, OllamaClient) or urlsplit(client.base_url).hostname not in {
            "127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Cancellation QA requires local Ollama")
    dispatched = threading.Event()
    ended = threading.Event()
    errors = []
    context = TurnExecutionContext("qa-model-cancel", "qa-local")
    original_stream = httpx.AsyncClient.stream

    async def trace(event, _info):
        if event == "http11.send_request_body.complete":
            dispatched.set()

    def traced_stream(self, *args, **kwargs):
        kwargs["extensions"] = {**kwargs.get("extensions", {}), "trace": trace}
        return original_stream(self, *args, **kwargs)

    def generate():
        try:
            with bind_turn_context(context):
                client.chat_structured([{"role": "user", "content":
                    "재귀 함수의 동작을 서로 다른 예제 100개와 상세한 해설로 길게 설명해줘."}])
        except BaseException as exc:
            errors.append(exc)
        finally:
            ended.set()

    # Instrument only this isolated diagnostic process, restoring it even if
    # the server fails. HTTP core's trace confirms bytes were actually sent.
    httpx.AsyncClient.stream = traced_stream
    worker = threading.Thread(target=generate, name="qa-real-model-cancel")
    worker.start()
    try:
        sent = dispatched.wait(20)
        finished_before_cancel = ended.wait(1) if sent else ended.is_set()
        started = time.monotonic()
        context.cancel()
        worker.join(5)
        seconds = time.monotonic() - started
        passed = (sent and not finished_before_cancel and not worker.is_alive()
                  and len(errors) == 1 and isinstance(errors[0], ToolCancelledError)
                  and seconds < 1)
        print(json.dumps({"event": "model_cancel", "passed": passed,
                          "request_body_sent": sent,
                          "finished_before_cancel": finished_before_cancel,
                          "cancel_seconds": round(seconds, 3),
                          "worker_alive": worker.is_alive(),
                          "errors": [type(exc).__name__ for exc in errors]},
                         ensure_ascii=False), flush=True)
    finally:
        context.cancel()
        worker.join(125)
        httpx.AsyncClient.stream = original_stream
    if not passed:
        return 1
    started = time.monotonic()
    with bind_turn_context(TurnExecutionContext("qa-model-next", "qa-local")):
        answer = client.chat_structured([{"role": "user", "content": "짧게 인사만 해줘."}])
    print(json.dumps({"event": "model_after_cancel", "response": answer,
                      "seconds": round(time.monotonic() - started, 2)},
                     ensure_ascii=False), flush=True)
    return 0 if answer.strip() else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--conversation", action="store_true")
    parser.add_argument("--answer-review", action="store_true",
                        help="Exercise tool-free answer acceptance through the real local Executor")
    parser.add_argument("--model-cancel", action="store_true",
                        help="Cancel a real local model HTTP call, then request a new greeting")
    parser.add_argument("--model-trace", action="store_true",
                        help="Log bounded model transport timings/options, never request or response text")
    parser.add_argument("--semantic-matrix", action="store_true",
                        help="Interpret synthetic requests against all real tools; never execute them")
    parser.add_argument("--runtime-read", action="store_true",
                        help="Call the real executor/model/read plugin on a synthetic file, without UI automation")
    parser.add_argument("--stt-prepare", action="store_true",
                        help="Load the installed STT model on demand without opening a microphone")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.stdout.reconfigure(encoding="utf-8")
    sandbox = Path(tempfile.mkdtemp(prefix="anis-common-qa-"))
    os.chdir(sandbox)
    os.environ.update({
        "DB_PATH": str(sandbox / "data/assistant.db"),
        "LEARNING_DB_PATH": str(sandbox / "data/learning.db"),
        "OBSIDIAN_VAULT_PATH": str(sandbox / "vault"),
        "ALLOWED_PATHS": str(sandbox),
        "JARVIS_DISABLE_MIC_AUTOSTART": "1",
        "JARVIS_DISABLE_CAMERA_AUTOSTART": "1",
        "JARVIS_SKIP_FIRST_RUN_WIZARD": "1",
        "RAG_DEVICE": "cpu",
        "RAG_EMBEDDING_MODEL_PATH": str(root / "data/models/bge-m3"),
    })
    os.environ["QT_QPA_PLATFORM"] = "windows" if args.gui else "offscreen"
    from core.assistant_settings import get_assistant_settings
    settings = get_assistant_settings()
    settings.set("assistant_name", "아니스 QA")
    settings.set("response_style", "자연스러운 반말로 대답해")
    settings.set_tts_enabled(False)
    print(json.dumps({"event": "qa_workspace", "path": str(sandbox)}, ensure_ascii=False), flush=True)
    with trace_model_calls(args.model_trace):
        return _run_qa(args, root, sandbox, settings)


def _run_qa(args, root: Path, sandbox: Path, settings) -> int:
    if args.model_cancel:
        return check_local_model_cancellation()
    if args.stt_prepare:
        os.environ["HF_HUB_OFFLINE"] = "1"
        from core.hardware import HardwareManager
        manager = HardwareManager()
        before = manager.stt_state
        started = time.monotonic()
        try:
            manager.ensure_stt_model()
            passed = before == "not_loaded" and manager.stt_state == "ready" and not manager.running
            print(json.dumps({"event": "stt_prepare", "passed": passed, "before": before,
                              "after": manager.stt_state, "device": manager.device,
                              "engine": manager.stt_engine, "model": manager.whisper_model_name,
                              "microphone_running": manager.running,
                              "elapsed_seconds": round(time.monotonic()-started, 2)}, ensure_ascii=False), flush=True)
            return 0 if passed else 1
        finally:
            manager.shutdown()
    if args.conversation:
        from core.agent_services import ConversationService
        from core.llm import get_llm_client
        service = ConversationService(get_llm_client("conversation"))
        history = []
        for request in ("안녕?", "답변이 느리네. 왜 그런지 원인을 아직 확인하지 않았다면 추측이라고 말해줘."):
            started = time.monotonic()
            answer = service.respond(request, history, assistant_name="아니스", style=settings.get("response_style"))
            print(json.dumps({"request": request, "response": answer, "elapsed_seconds": round(time.monotonic()-started, 2)}, ensure_ascii=False), flush=True)
            history.extend([{"role": "user", "content": request}, {"role": "assistant", "content": answer}])
        return 0
    from core.workspace import get_workspace_manager
    fixture_workspace = sandbox / "workspace"
    fixture_workspace.mkdir()
    (sandbox / "workspace-second").mkdir()
    (fixture_workspace / "QA 기록.txt").write_bytes("x = [1, 2] 🙂\r\n두 번째 줄\r\n".encode("utf-8"))
    get_workspace_manager().set_workspace(str(fixture_workspace))
    if args.answer_review:
        from core.executor import Executor
        from core.llm import OllamaClient
        from core.turn_context import TurnExecutionContext
        from urllib.parse import urlsplit
        executor = Executor()
        if not isinstance(executor.llm, OllamaClient) or urlsplit(executor.llm.base_url).hostname not in {
                "127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("Answer QA requires local Ollama")
        before = {str(path.relative_to(fixture_workspace)): path.read_bytes()
                  for path in fixture_workspace.rglob("*") if path.is_file()}
        request = ("파이썬 재귀 함수 예제 3개를 각각 코드와 해설로 보여줘. "
                   "빈 입력과 종료 조건도 설명해줘. 파일을 만들거나 코드를 실행하지 말고 채팅에서만 설명해줘.")
        started = time.monotonic()
        try:
            outcome = executor.execute_turn(
                request, "qa-answer", allowed_tool_names=[],
                turn_context=TurnExecutionContext("qa-answer", "qa-answer", str(fixture_workspace)),
            )
            after = {str(path.relative_to(fixture_workspace)): path.read_bytes()
                     for path in fixture_workspace.rglob("*") if path.is_file()}
            safe = before == after and not outcome.tool_results and outcome.tool_result is None
            record = {"event": "answer_review", "status": outcome.status,
                      "answer_review": outcome.answer_review, "response": outcome.response,
                      "response_truncated": outcome.response_truncated,
                      "unsupported_activity_claim": outcome.unsupported_activity_claim,
                      "no_tools_or_workspace_changes": safe,
                      "elapsed_seconds": round(time.monotonic() - started, 2)}
            print(json.dumps(record, ensure_ascii=False), flush=True)
            # A model-static pass is NOT independent semantic correctness proof.
            return 0 if safe and outcome.status == "completed" else 1
        finally:
            executor.tool_executor.plugin_registry.shutdown()
    if args.semantic_matrix:
        from core.executor import Executor
        from core.llm import OllamaClient
        from urllib.parse import urlsplit
        executor = Executor()
        client = executor.reasoning_llm
        if not isinstance(client, OllamaClient) or urlsplit(client.base_url).hostname not in {
                "127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("Semantic QA requires local Ollama")
        cases = [
            ("안녕?", "conversation"),
            ("오늘 좀 지쳤어. 잠깐 이야기하자.", "conversation"),
            ("실행하지 말고 재귀 함수가 뭔지 설명해줘.", "conversation"),
            ("그 설명은 취소하고 지금은 짧게 인사만 해줘.", "conversation"),
            ("QA 기록.txt 파일의 첫 번째 줄만 그대로 알려줘", "read"),
            ("실제 우주선을 조종해서 화성에 착륙시켜줘", "unsupported"),
        ]
        original = client.chat_structured
        calls = []
        def measured(messages, schema, **kwargs):
            started = time.monotonic()
            response = original(messages, schema, **kwargs)
            calls.append({"seconds": round(time.monotonic() - started, 2),
                          "response": response})
            return response
        client.chat_structured = measured
        passed = 0
        try:
            for request, expected in cases:
                calls.clear()
                started = time.monotonic()
                decision = executor.semantic_interpreter.interpret(request)
                ok = (decision.grounded and decision.relation == "conversation"
                      and not decision.tool_names) if expected == "conversation" else (
                    decision.grounded and decision.operation == "read"
                    and decision.tool_names == ("filesystem_read_file",)
                    and decision.slots.get("start_line") == decision.slots.get("end_line") == 1
                    if expected == "read" else
                    decision.relation != "conversation" and not decision.tool_names)
                passed += bool(ok)
                print(json.dumps({"event": "semantic_matrix", "request": request,
                                  "expected": expected, "passed": bool(ok),
                                  "decision": decision.__dict__, "calls": calls,
                                  "seconds": round(time.monotonic()-started, 2)},
                                 ensure_ascii=False), flush=True)
                if decision.reason.endswith((":timeout", ":connection")):
                    print(json.dumps({"event": "semantic_matrix_aborted",
                                      "reason": decision.reason,
                                      "note": "Remaining cases were not run; this is not a quality pass."}), flush=True)
                    return 1
            print(json.dumps({"event": "semantic_summary", "passed": passed,
                              "total": len(cases)}), flush=True)
            return 0 if passed == len(cases) else 1
        finally:
            executor.tool_executor.plugin_registry.shutdown()
    if args.runtime_read:
        from core.executor import Executor
        from core.llm import OllamaClient
        from core.turn_context import TurnExecutionContext
        executor = Executor()
        if not isinstance(executor.reasoning_llm, OllamaClient):
            raise RuntimeError("This QA requires the configured local Ollama client")
        from urllib.parse import urlsplit
        if urlsplit(executor.reasoning_llm.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("This QA must not transmit to a remote provider")
        target = fixture_workspace / "QA 기록.txt"
        before = target.read_bytes()
        started = time.monotonic()
        try:
            outcome = executor.execute_turn(
                "QA 기록.txt 파일에 작성되어 있는 첫 번째 줄 내용을 알려줘",
                allowed_tool_names=["filesystem_read_file"],
                turn_context=TurnExecutionContext("qa-read", "qa-local", str(fixture_workspace)),
            )
            passed = (outcome.status == "completed" and "x = [1, 2] 🙂\r\n" in outcome.response
                      and target.read_bytes() == before and outcome.tool_result is not None
                      and outcome.tool_result.tool_name == "filesystem_read_file"
                      and bool(outcome.tool_result.evidence))
            print(json.dumps({"event": "runtime_read", "passed": passed, "status": outcome.status,
                              "response": outcome.response, "task_id": outcome.task_id,
                              "elapsed_seconds": round(time.monotonic()-started, 2)}, ensure_ascii=False), flush=True)
            return 0 if passed else 1
        finally:
            executor.tool_executor.plugin_registry.shutdown()
    from PyQt6.QtCore import QTimer
    from main_qt import JarvisApp
    started = time.monotonic()
    app = JarvisApp()
    app.window.setWindowTitle("ANIS — Common Runtime QA")
    app.window.showNormal()
    print(json.dumps({"event": "gui_ready", "platform": app.app.platformName(),
                      "stt_state": app.hardware_manager.stt_state,
                      "stt_model_loaded": app.hardware_manager.whisper_model is not None,
                      "startup_seconds": round(time.monotonic()-started, 2)}, ensure_ascii=False), flush=True)
    QTimer.singleShot(max(5, args.seconds) * 1000, app.app.quit)
    result = app.run()
    print(json.dumps({"event": "gui_closed", "exit_code": result, "shutdown_errors": getattr(app, "_shutdown_errors", [])}, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
