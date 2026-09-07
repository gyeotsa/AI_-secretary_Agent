"""Killable, per-execution worker for explicitly stateless Plugin tools.

Only the parent Registry selects the trusted importable Plugin factory. User
tool arguments never select Python modules, commands, or IPC paths. File-based
IPC avoids pipe-reader threads surviving a stuck native call on Windows.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from core.tool_result import ToolRunResult


class WorkerProtocolError(RuntimeError):
    pass


def _read_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise WorkerProtocolError("격리 작업의 응답이 없거나 허용 크기를 초과했습니다.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkerProtocolError("격리 작업 응답은 JSON 객체여야 합니다.")
    return value


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


class ProcessToolWorker:
    """Own exactly one Python worker, never the user's app or a COM server."""

    def __init__(self, execution_id: str, tool_name: str, *, root: str | None = None):
        self.execution_id, self.tool_name = execution_id, tool_name
        self.workspace = Path(tempfile.mkdtemp(prefix="jarvis-tool-worker-", dir=root)).resolve()
        self._owned_workspace = self.workspace
        self._owned_parent = self.workspace.parent
        self.request_path = self.workspace / "request.json"
        self.result_path = self.workspace / "result.json"
        self.cancel_path = self.workspace / "cancel"
        self.ready_path = self.workspace / "ready.json"
        self.authorized_path = self.workspace / "execute.json"
        self.identity_verified = False
        self._process: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._cancel_requested = threading.Event()
        self.cleanup_complete = False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def start(self, request: dict) -> None:
        with self._lock:
            if self._cancel_requested.is_set():
                raise WorkerProtocolError("종료 중인 격리 작업은 시작할 수 없습니다.")
            request = {**request, "version": 1, "execution_id": self.execution_id, "tool_name": self.tool_name}
            _write_json(self.request_path, request)
            args = ["--request", str(self.request_path), "--result", str(self.result_path)]
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            frozen = bool(getattr(sys, "frozen", False))
            executable = sys.executable
            if os.name == "nt" and not frozen:
                # Windows venv python.exe is a redirector which creates a
                # second process. Own the real interpreter PID directly while
                # preserving the venv's prefix/site-packages via its standard
                # launcher variable. Killing only a redirector is not a hard
                # cancellation guarantee for the native Python worker.
                base = str(getattr(sys, "_base_executable", "") or "")
                if base and Path(base).is_file() and Path(base).resolve() != Path(executable).resolve():
                    env["__PYVENV_LAUNCHER__"] = executable
                    executable = base
            command = ([executable, "--plugin-worker", *args] if frozen
                       else [executable, "-u", "-m", "core.plugin_worker", *args])
            # No visible console, shell, inherited pipe, or GUI bootstrap.
            self._process = subprocess.Popen(
                command, cwd=str(Path(__file__).resolve().parent.parent), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )

    def wait(self, deadline: float, token: Any) -> Any:
        process = self._process
        if process is None:
            raise WorkerProtocolError("격리 작업 프로세스가 시작되지 않았습니다.")
        while process.poll() is None:
            token.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("격리 작업 시간 초과")
            if not self.identity_verified and self.ready_path.is_file():
                ready = _read_json(self.ready_path)
                if (ready.get("worker_pid") != process.pid or ready.get("execution_id") != self.execution_id
                        or ready.get("tool_name") != self.tool_name):
                    raise WorkerProtocolError("실제 worker PID/실행 ID가 소유한 프로세스와 다릅니다.")
                token.raise_if_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("격리 작업 시작 전에 제한시간을 초과했습니다.")
                _write_json(self.authorized_path, {"execution_id": self.execution_id, "worker_pid": process.pid})
                self.identity_verified = True
            try:
                process.wait(timeout=min(0.03, remaining))
            except subprocess.TimeoutExpired:
                continue
        token.raise_if_cancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("제한시간 이후의 격리 작업 응답은 적용하지 않습니다.")
        if process.returncode != 0:
            raise WorkerProtocolError(f"격리 작업이 비정상 종료됐습니다(code={process.returncode}).")
        envelope = _read_json(self.result_path)
        if (type(envelope.get("version")) is not int or envelope.get("version") != 1
                or envelope.get("execution_id") != self.execution_id
                or envelope.get("tool_name") != self.tool_name or envelope.get("worker_pid") != process.pid
                or not self.identity_verified):
            raise WorkerProtocolError("격리 작업 응답의 실행 ID/대상이 일치하지 않습니다.")
        if envelope.get("kind") == "error":
            raise WorkerProtocolError(str(envelope.get("error", "격리 작업 실패"))[:1000])
        if envelope.get("kind") == "tool_result":
            return ToolRunResult.from_dict(envelope.get("result"), expected_tool=self.tool_name)
        if envelope.get("kind") == "json" and isinstance(envelope.get("result"), (dict, list, str)):
            return envelope["result"]
        raise WorkerProtocolError("격리 작업 응답 형식이 올바르지 않습니다.")

    def request_cancel(self) -> None:
        self._cancel_requested.set()
        try:
            self.cancel_path.touch(exist_ok=True)
        except OSError:
            pass  # The completed worker's private directory may already be gone.

    def stop(self, *, grace: float = 0.1, kill_wait: float = 0.75) -> bool:
        """Bound cancellation by terminating only the owned worker process."""
        self.request_cancel()
        with self._lock:
            process = self._process
        # Never hold the worker lock while joining. shutdown() may race the
        # invocation's cancellation and must retain its own bounded deadline.
        if process is None or process.poll() is not None:
            return True
        deadline = time.monotonic() + max(0.0, grace) + max(0.0, kill_wait)
        try:
            process.wait(timeout=max(0.0, grace))
            return True
        except subprocess.TimeoutExpired:
            pass
        try:
            process.terminate()
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None
        return process.poll() is not None

    def cleanup(self) -> bool:
        with self._lock:
            if self.running:
                return False
            try:
                resolved = self.workspace.resolve()
            except (OSError, RuntimeError):
                return False
            if (resolved != self._owned_workspace or resolved.parent != self._owned_parent
                    or self.workspace.is_symlink() or not resolved.name.startswith("jarvis-tool-worker-")):
                # Refuse to recursively remove anything other than the exact
                # private temporary directory created by this worker.
                return False
            try:
                if self.workspace.exists():
                    shutil.rmtree(self.workspace)
                self.cleanup_complete = True
            except OSError:
                # A separate COM server can still hold its staging output. Do
                # not kill that server or publish that unverified output.
                self.cleanup_complete = False
            return self.cleanup_complete

    def evidence(self) -> dict:
        return {
            "execution_id": self.execution_id, "worker_pid": self.pid,
            "worker_terminated": not self.running,
            "worker_returncode": self._process.returncode if self._process else None,
            "worker_identity_verified": self.identity_verified,
            "workspace_cleaned": self.cleanup_complete,
            "cleanup_pending_path": str(self.workspace) if not self.cleanup_complete else "",
        }


def _execute_request(request: dict, workspace: Path) -> Any:
    from core.plugin import BasePlugin, CancellationToken, ToolExecutionContext

    module_name, class_name = request.get("module"), request.get("class_name")
    if (not isinstance(module_name, str) or not isinstance(class_name, str)
            or "<" in class_name or module_name == "__main__"):
        raise WorkerProtocolError("격리 Plugin은 import 가능한 명시적 factory여야 합니다.")
    factory = importlib.import_module(module_name)
    for part in class_name.split("."):
        factory = getattr(factory, part)
    if not inspect.isclass(factory) or not issubclass(factory, BasePlugin) or not factory.supports_process_isolation:
        raise WorkerProtocolError("이 Plugin은 stateless process isolation을 선언하지 않았습니다.")
    plugin = factory()
    tool_name = request["tool_name"]
    schema = next((tool for tool in plugin.get_tools() if tool.name == tool_name), None)
    if schema is None or schema.execution_isolation != "process":
        raise WorkerProtocolError("격리 작업 대상 Tool이 허용되지 않습니다.")

    class WorkerToken(CancellationToken):
        @property
        def cancelled(self):
            return (super().cancelled or (workspace / "cancel").exists()
                    or time.monotonic() >= float(request["deadline"]))

    token = WorkerToken()
    context = ToolExecutionContext(
        execution_id=request["execution_id"], tool_name=tool_name,
        idempotency_key=str(request.get("idempotency_key", "")), attempt=int(request.get("attempt", 0)),
        started_at=float(request.get("started_at", time.perf_counter())), cancellation_token=token,
        staging_directory=str(workspace),
        deadline=float(request["deadline"]),
    )

    class ContextRegistry:
        def current_execution_context(self):
            return context

    plugin.registry = ContextRegistry()
    from config import Config
    paths = request.get("allowed_paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise WorkerProtocolError("격리 작업의 허용 경로 계약이 올바르지 않습니다.")
    Config.API_CONFIG.ALLOWED_PATHS = [*paths, str(workspace)]
    token.raise_if_cancelled()
    result = plugin.execute_tool(tool_name, dict(request.get("input", {})))
    result = asyncio.run(result) if inspect.isawaitable(result) else result
    token.raise_if_cancelled()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    request_path, result_path = Path(args.request).resolve(), Path(args.result).resolve()
    if request_path.parent != result_path.parent:
        return 2
    request = _read_json(request_path)
    envelope = {"version": 1, "execution_id": request.get("execution_id"),
                "tool_name": request.get("tool_name"), "worker_pid": os.getpid()}
    try:
        # Before importing/executing native plugins, prove that this is the
        # exact process handle owned by the parent. Unexpected bootloaders or
        # venv redirector children never receive execution authorization.
        _write_json(request_path.parent / "ready.json", envelope)
        authorization = request_path.parent / "execute.json"
        while not authorization.is_file():
            if ((request_path.parent / "cancel").exists()
                    or time.monotonic() >= float(request["deadline"])):
                raise WorkerProtocolError("실행 승인 전에 격리 작업이 취소되거나 만료됐습니다.")
            time.sleep(0.01)
        permit = _read_json(authorization)
        if permit.get("worker_pid") != os.getpid() or permit.get("execution_id") != request.get("execution_id"):
            raise WorkerProtocolError("격리 작업의 실행 승인이 일치하지 않습니다.")
        result = _execute_request(request, request_path.parent)
        envelope.update(kind="tool_result" if isinstance(result, ToolRunResult) else "json",
                        result=result.to_dict() if isinstance(result, ToolRunResult) else result)
        _write_json(result_path, envelope)
    except Exception as exc:
        envelope.update(kind="error", error=f"{type(exc).__name__}: {exc}"[:1000])
        _write_json(result_path, envelope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
