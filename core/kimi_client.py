"""Optional K3 C-engine chat adapter. No automatic downloads or tool execution."""
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import psutil
import requests

from config import Config
from core import auxiliary_models
from core.llm import BaseLLMClient, ModelCallError, ProseResponse
from core.local_inference import local_inference, release_idle_models
from core.turn_context import check_turn_cancelled


_LOG = logging.getLogger(__name__)


class _WindowsJob:
    """One invocation's kernel-owned tree, assigned before its first instruction."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Limits(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                        ("maximum", ctypes.c_size_t), ("processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.ctypes, self.wintypes = ctypes, wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.native = ctypes.WinDLL("ntdll")
        self.native.NtSuspendProcess.argtypes = [wintypes.HANDLE]
        self.native.NtSuspendProcess.restype = wintypes.LONG
        self.native.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.native.NtResumeProcess.restype = wintypes.LONG
        signatures = {
            "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        try:
            self._checked(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def _checked(self, result):
        if not result:
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def assign(self, process):
        self._checked(self.api.AssignProcessToJobObject(self.handle, int(process._handle)))

    def resume(self, process):
        # Popen retains this kernel handle even if its PID is externally recycled.
        status = self.native.NtResumeProcess(int(process._handle))
        if status < 0:
            raise OSError(f"Kimi K3 resume failed: NTSTATUS {status & 0xffffffff:#x}")

    def pids(self):
        capacity = 16
        while True:
            class ProcessIds(self.ctypes.Structure):
                _fields_ = [("assigned", self.wintypes.DWORD), ("count", self.wintypes.DWORD),
                            ("ids", self.ctypes.c_size_t * capacity)]
            result = ProcessIds()
            if self.api.QueryInformationJobObject(self.handle, 3, self.ctypes.byref(result), self.ctypes.sizeof(result), None):
                return list(result.ids[:result.count])
            if self.ctypes.get_last_error() != 234:  # ERROR_MORE_DATA: tree grew.
                self._checked(False)
            capacity = max(capacity * 2, result.assigned)

    def stop(self):
        handles, issues = {}, []
        try:
            try:
                # Freeze members before taking join handles, so a launcher cannot
                # spawn a new inheritor between our snapshot and termination.
                deadline = time.monotonic() + 5
                while True:
                    pending = set(self.pids()) - handles.keys()
                    if not pending:
                        break
                    for pid in pending:
                        handle = self.api.OpenProcess(0x101800, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION | SUSPEND_RESUME
                        if not handle:
                            if self.ctypes.get_last_error() == 87:
                                continue  # Already exited PID.
                            self._checked(False)
                        member = self.wintypes.BOOL()
                        try:
                            self._checked(self.api.IsProcessInJob(handle, self.handle, self.ctypes.byref(member)))
                        except BaseException:
                            self.api.CloseHandle(handle)
                            raise
                        if not member.value:
                            self._checked(self.api.CloseHandle(handle))
                            continue  # PID was recycled after the Job snapshot.
                        handles[pid] = handle
                        if self.api.WaitForSingleObject(handle, 0) != 0:
                            # Act on the checked kernel object, not a second PID
                            # lookup that might target a recycled unrelated PID.
                            status = self.native.NtSuspendProcess(handle)
                            if status < 0 and self.api.WaitForSingleObject(handle, 100) != 0:
                                raise OSError(f"Kimi K3 suspend failed: NTSTATUS {status & 0xffffffff:#x}")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Kimi K3 owned process tree did not quiesce")
            except Exception as exc:
                issues.append(exc)
            # Inspection failure still reaches kernel tree termination.
            try:
                self._checked(self.api.TerminateJobObject(self.handle, 1))
            except Exception as exc:
                issues.append(exc)
            # Job accounting can reach zero before inherited handles are closed.
            for handle in handles.values():
                try:
                    result = self.api.WaitForSingleObject(handle, 5000)
                    if result == 258:
                        raise TimeoutError("Kimi K3 owned process did not join")
                    if result != 0:
                        self._checked(False)
                except Exception as exc:
                    issues.append(exc)
            try:
                deadline = time.monotonic() + 5
                while self.pids():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Kimi K3 owned process tree did not stop")
                    time.sleep(0.01)
            except Exception as exc:
                issues.append(exc)
        finally:
            for handle in handles.values():
                try:
                    self._checked(self.api.CloseHandle(handle))
                except Exception as exc:
                    issues.append(exc)
        if issues:
            raise RuntimeError("Kimi K3 job cleanup: " + "; ".join(repr(exc) for exc in issues)) from issues[0]

    def close(self):
        if self.handle:
            self._checked(self.api.CloseHandle(self.handle))
            self.handle = None


def _cleanup_failure(original, stage, failure):
    diagnostic = f"Kimi K3 cleanup failed ({stage}): {failure!r}"
    _LOG.error(diagnostic)
    if original is not None:
        original.add_note(diagnostic)
        original.cleanup_errors = (*getattr(original, "cleanup_errors", ()), diagnostic)
        return original
    error = RuntimeError(diagnostic)
    error.__cause__ = failure
    return error


def _integer(name, default, minimum, maximum):
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 값은 {minimum}~{maximum} 범위여야 합니다.")
    return value


@dataclass(frozen=True)
class KimiOptions:
    executable: Path
    model_dir: Path
    trunk_dir: Path
    threads: int
    max_tokens: int
    timeout: int
    max_ram_mb: int
    reserve_ram_mb: int
    max_input_bytes: int

    @classmethod
    def from_environment(cls):
        paths = []
        for name in ("KIMI_K3_EXECUTABLE", "KIMI_K3_MODEL_DIR", "KIMI_K3_TRUNK_DIR"):
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"{name}를 .env에 설정해 주세요. Kimi K3는 자동 설치되지 않습니다.")
            paths.append(Path(value).expanduser().resolve())
        return cls(
            *paths,
            _integer("KIMI_K3_THREADS", min(4, max(1, (os.cpu_count() or 2) // 2)), 1, 64),
            _integer("KIMI_K3_MAX_TOKENS", 256, 1, 4096),
            _integer("KIMI_K3_TIMEOUT_SECONDS", 7200, 1, 86400),
            _integer("KIMI_K3_MAX_RAM_MB", 10240, 4096, 262144),
            _integer("KIMI_K3_RESERVE_RAM_MB", 2048, 1024, 65536),
            _integer("KIMI_K3_MAX_INPUT_BYTES", 12000, 128, 100000),
        )

    def validate_paths(self, *, full=False):
        if not self.executable.is_file():
            raise ValueError(f"Kimi K3 실행 파일이 없습니다: {self.executable}")
        required = [self.model_dir / name for name in
                    ("config.json", "tiktoken.model", "tokenizer_config.json", "model.safetensors.index.json")]
        required += [self.trunk_dir / name for name in ("trunk.bin", "trunk.json")]
        for path in required:
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Kimi K3 필수 파일이 없거나 비어 있습니다: {path}")
        if full:
            index = json.loads((self.model_dir / "model.safetensors.index.json").read_text(encoding="utf-8"))
            shards = set(index["weight_map"].values())
            if not shards:
                raise ValueError("Kimi K3 체크포인트 인덱스에 가중치가 없습니다.")
            for name in shards:
                path = (self.model_dir / name).resolve()
                if not path.is_relative_to(self.model_dir) or not path.is_file() or not path.stat().st_size:
                    raise ValueError(f"Kimi K3 가중치 파일을 확인해 주세요: {name}")


def configuration_status():
    """Cheap UI check, not a successful inference or checkpoint integrity claim."""
    try:
        KimiOptions.from_environment().validate_paths()
        return True, "경로 확인됨 · 실제 모델 추론 미검증"
    except (OSError, ValueError) as exc:
        return False, str(exc)


class KimiClient(BaseLLMClient):
    model = "kimi-k3"

    def __init__(self, *, cancellation_check=None):
        # No BaseLLMClient init: K3 must not load a plugin/tool catalogue.
        self.system_prompt = "요청과 제공된 코드에 근거하여 정확하게 답하세요."
        self.tools = []
        self._generation = auxiliary_models.ticket()
        self._cancellation_check = cancellation_check

    def _error(self, code, detail):
        return ModelCallError("kimi_k3", self.model, code, detail, retryable=False)

    def _check(self):
        check_turn_cancelled()
        if self._cancellation_check:
            self._cancellation_check()
        if (not auxiliary_models.is_current(self._generation)
                or not auxiliary_models.is_enabled("kimi_k3")):
            raise self._error("disabled", "설정 변경 또는 앱 종료로 Kimi K3 실행을 중단했습니다.")

    check_cancelled = _check

    def chat_with_tools(self, messages, allowed_tool_names=None):
        raise self._error("unsupported", "이 K3 C 엔진은 native tool calling을 지원하지 않습니다.")

    def chat_prose(self, messages, **kwargs):
        return ProseResponse(self.chat_structured(messages, **kwargs),
                             provider="kimi_k3", model=self.model, finish_reason="stop")

    def chat(self, messages):
        return self.chat_structured(messages)

    def chat_structured(self, messages, json_schema=None, *, context_window=None,
                        request_timeout=None, max_output_tokens=None):
        self._check()
        try:
            options = KimiOptions.from_environment()
            options.validate_paths(full=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise self._error("configuration", str(exc)) from exc
        timeout = options.timeout
        if request_timeout is not None:
            if (isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
                    or not math.isfinite(request_timeout) or request_timeout <= 0):
                raise ValueError("request_timeout must be positive and finite")
            timeout = min(timeout, request_timeout)
        max_tokens = options.max_tokens
        if max_output_tokens is not None:
            if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
                raise ValueError("max_output_tokens must be a positive integer")
            max_tokens = min(max_tokens, max_output_tokens)
        records = self._records(messages)
        if json_schema:
            instruction = "\nReturn one JSON object matching this schema: " + json.dumps(json_schema, ensure_ascii=False)
            records[0]["content"] += instruction
        serialized = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
        # No silent truncation of source code or instructions. Engine checks actual tokens/KV.
        if len(serialized.encode("utf-8")) > options.max_input_bytes:
            raise self._error("context", "Kimi K3 입력 예산을 초과했습니다. 코드 범위를 줄이거나 KIMI_K3_MAX_INPUT_BYTES를 조정하세요.")
        auxiliary_models.set_status("다른 로컬 모델 종료 대기")
        deadline = time.monotonic() + timeout
        try:
            with local_inference(self._check, timeout=timeout):
                release_idle_models()
                self._ensure_ollama_idle()
                self._check_memory(options, before_start=True)
                self._check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Kimi K3 실행 준비 중 제한시간을 초과했습니다.")
                auxiliary_models.set_status("Kimi K3 실행 중")
                return self._run(options, records, serialized, max_tokens, remaining)
        except (ModelCallError,):
            raise
        except TimeoutError as exc:
            raise self._error("timeout", str(exc)) from exc
        except (OSError, ValueError, RuntimeError, psutil.Error, requests.RequestException) as exc:
            # Preserve cooperative turn/tool cancellation rather than converting it.
            from core.plugin import ToolCancelledError
            if isinstance(exc, ToolCancelledError):
                raise
            raise self._error("runtime", str(exc)) from exc
        finally:
            auxiliary_models.set_status("대기")

    def _records(self, messages):
        system = []
        records = []
        for message in messages:
            if message.get("images") or message.get("tool_calls") or message.get("role") not in {"system", "user", "assistant"}:
                raise self._error("unsupported", "Kimi K3 보조 경로는 텍스트 메시지만 받습니다.")
            content = message.get("content")
            if not isinstance(content, str):
                raise self._error("protocol", "Kimi K3 메시지 본문은 문자열이어야 합니다.")
            if message["role"] == "system":
                if records:
                    raise self._error("protocol", "system 메시지는 대화 앞에만 올 수 있습니다.")
                system.append(content)
            else:
                if (not records and message["role"] != "user") or (records and records[-1]["role"] == message["role"]):
                    raise self._error("protocol", "Kimi K3 대화는 user/assistant 순서로 교대해야 합니다.")
                records.append({"role": message["role"], "content": content})
        if not records or records[-1]["role"] != "user":
            raise self._error("protocol", "Kimi K3 요청은 user 메시지로 끝나야 합니다.")
        return [{"role": "system", "content": "\n\n".join(system) or self.system_prompt}, *records]

    def _ensure_ollama_idle(self):
        try:
            response = requests.get(Config.OLLAMA_BASE_URL.rstrip("/") + "/api/ps", timeout=3)
        except requests.ConnectionError:
            return  # No listening Ollama server; no hosted model to overlap.
        response.raise_for_status()
        if response.json().get("models"):
            raise self._error("busy", "Ollama에 모델이 아직 상주합니다. 다른 앱의 모델은 자동 종료하지 않습니다. 해제 후 다시 요청하세요.")

    def _check_memory(self, options, *, before_start=False, process=None, job=None):
        available = psutil.virtual_memory().available / 1024**2
        required = options.reserve_ram_mb + (options.max_ram_mb if before_start else 0)
        if available < required:
            raise self._error("memory", f"가용 RAM {available:.0f}MB가 안전 예산 {required}MB보다 작아 Kimi K3를 중단합니다.")
        if process is not None:
            if job is not None:
                pids = job.pids()
            else:
                try:
                    root = psutil.Process(process.pid)
                    pids = [root.pid, *(child.pid for child in root.children(recursive=True))]
                except psutil.NoSuchProcess:
                    pids = []
            rss = 0
            for pid in pids:
                try:
                    rss += psutil.Process(pid).memory_info().rss / 1024**2
                except psutil.NoSuchProcess:
                    pass  # A just-exited member no longer owns memory.
            if rss > options.max_ram_mb:
                raise self._error("memory", f"Kimi K3 RAM 사용량이 {options.max_ram_mb}MB 한도를 초과했습니다.")

    def _run(self, options, records, serialized, max_tokens, timeout):
        temporary = tempfile.TemporaryDirectory(prefix="anis-kimi-")
        directory = temporary.name
        process = job = log = None
        failure = None
        try:
            history = Path(directory) / "history.jsonl"
            history.write_text(serialized, encoding="utf-8")
            command = [str(options.executable), str(options.model_dir),
                       "--trunk", str(options.trunk_dir), "--preset", "laptop",
                       "--tok", str(options.model_dir), "--chat", "--history", str(history),
                       "--incremental", "--no-think", "--greedy", "--gen", str(max_tokens)]
            env = {**os.environ, "OMP_NUM_THREADS": str(options.threads),
                   "OMP_THREAD_LIMIT": str(options.threads), "OMP_WAIT_POLICY": "PASSIVE"}
            # DEVNULL stdin ends the REPL after the completed history-tail request.
            # Output files avoid pipe deadlocks and unbounded in-memory logs.
            log = (Path(directory) / "engine.log").open("w+b")
            self._check()
            if os.name == "nt":
                job = _WindowsJob()
            process = subprocess.Popen(command, cwd=directory, env=env, shell=False,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                # CREATE_SUSPENDED closes the venv-launcher late-spawn race.
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS | 0x4) if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            if job is not None:
                job.assign(process)
                self._check()
                job.resume(process)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                self._check()
                self._check_memory(options, process=process, job=job)
                if time.monotonic() >= deadline:
                    raise self._error("timeout", f"Kimi K3 실행 제한 {timeout:g}초를 초과했습니다.")
                if log.tell() > 4 * 1024**2:
                    raise self._error("protocol", "Kimi K3 로그가 허용 크기를 초과했습니다.")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            self._check()
            if time.monotonic() >= deadline:
                raise self._error("timeout", "Kimi K3 실행 제한시간을 초과했습니다.")
            if process.returncode != 0:
                # Do not return potentially sensitive native logs as model text.
                raise self._error("engine", f"Kimi K3 엔진 실패(code={process.returncode}). 가중치·문맥·출력 한도를 확인하세요. 미완성 응답은 적용하지 않았습니다.")
            if history.stat().st_size > options.max_input_bytes + 1024 * 1024:
                raise self._error("protocol", "Kimi K3 이력 파일이 허용 크기를 초과했습니다.")
            output = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
            if (not all(isinstance(item, dict) for item in output)
                    or len(output) != len(records) + 1 or output[:-1] != records
                    or output[-1].get("role") != "assistant"
                    or not isinstance(output[-1].get("content"), str) or not output[-1]["content"].strip()):
                raise self._error("protocol", "완료된 Kimi K3 응답 기록을 확인하지 못했습니다.")
            self._check()
            return output[-1]["content"]
        except BaseException as exc:
            failure = exc
            raise
        finally:
            primary = failure
            if process is not None:
                try:
                    if job is not None:
                        job.stop()  # Includes late spawns and workers whose launcher exited.
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                except Exception as exc:
                    failure = _cleanup_failure(failure, "process tree", exc)
                # Inspection/Job errors must never bypass the owned launcher's join.
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                except Exception as exc:
                    failure = _cleanup_failure(failure, "launcher join", exc)
            for stage, resource, close in (("job handle", job, lambda: job.close()),
                                           ("engine log", log, lambda: log.close()),
                                           ("temporary files", temporary, temporary.cleanup)):
                if resource is not None:
                    try:
                        close()
                    except Exception as exc:
                        failure = _cleanup_failure(failure, stage, exc)
            if primary is None and failure is not None:
                raise failure

    def release(self):
        # Each invocation joins its owned process before returning.
        return True
