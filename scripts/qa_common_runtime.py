"""Run the real Anis application in a disposable, local-only QA workspace.

No user databases are copied. Microphone, camera and speech are disabled for
this diagnostic process only. --gui exposes the actual window for manual QA.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--conversation", action="store_true")
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
    (fixture_workspace / "QA 기록.txt").write_bytes("x = [1, 2] 🙂\r\n두 번째 줄\r\n".encode("utf-8"))
    get_workspace_manager().set_workspace(str(fixture_workspace))
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
