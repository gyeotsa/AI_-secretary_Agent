"""Example-based Python checks in WASI, with no host filesystem/network grants.

Expected values stay in the host; guest code can only return an actual value.
This supports JSON-valued function examples, not arbitrary projects or stdin tasks.
"""
from __future__ import annotations

from contextvars import copy_context
from functools import lru_cache
from hashlib import sha256
import atexit
import json
from pathlib import Path
import re
import sys
import threading
import time

from core.plugin import ToolCancelledError
from core.turn_context import check_turn_cancelled


WASM_SHA256 = "e5dc5a398b07b54ea8fdb503bf68fb583d533f10ec3f930963e02b9505f7a763"
WASM_URL = ("https://github.com/vmware-labs/webassembly-language-runtimes/releases/download/"
            "python%2F3.12.0%2B20231211-040d5a6/python-3.12.0.wasm")
WASM_PATH = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "data/models/python-wasi/python.wasm"
# ponytail: serialize guest runs for per-call epoch cancellation; use one engine
# per worker if simultaneous coding conversations require higher throughput.
_RUN_LOCK = threading.Lock()
_MAX_OUTPUT = 64 * 1024


def extract_examples(problem: str) -> dict | None:
    """Read literal examples from the USER problem, never from generated answers.

    Accepts JSON values in tables or ``args -> expected`` lines. Ambiguous,
    conflicting, oversized or nonliteral cases fail closed instead of asking a
    model to invent a test oracle.
    """
    if len(problem) > 100_000:
        return None
    signature = re.search(r"\bdef\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", problem)
    if signature:
        entrypoint = signature[1]
    elif re.search(r"\bsolution\b", problem):
        entrypoint = "solution"
    else:
        return None
    sections = re.split(r"입출력\s*예(?:제)?(?!\s*설명)|(?im:^\s*examples?\s*:?)", problem, maxsplit=1)
    text = sections[-1]
    text = re.split(r"입출력\s*예\s*설명|예제\s*설명", text, maxsplit=1)[0]
    decoder = json.JSONDecoder()
    cases = []
    for line in text.splitlines():
        if len(line) > 8192:
            return None
        value = line.strip().strip("|").strip()
        # Without an example heading, only explicitly labelled mappings count.
        if len(sections) == 1 and not re.search(r"->|=>|→", value):
            continue
        values = []
        mapping = False
        try:
            while value:
                item, end = decoder.raw_decode(value)
                values.append(item)
                value = value[end:]
                if value:
                    separator = re.match(r"[\s,|]*(?:(?:->|=>|→)[\s,|]*)?", value)[0]
                    if not separator:
                        raise ValueError("nonliteral example")
                    mapping = mapping or bool(re.search(r"->|=>|→", separator))
                    value = value[len(separator):]
        except (ValueError, RecursionError):
            continue
        if len(values) < 2 or (len(sections) == 1 and not mapping):
            continue
        case = {"input": values[:-1], "expected": values[-1],
                "source": "user_problem_example", "source_text": line.strip()}
        if any(c["input"] == case["input"] and c["expected"] != case["expected"] for c in cases):
            return None
        if not any(c["input"] == case["input"] for c in cases):
            cases.append(case)
        if len(cases) > 16:
            return None
    if not cases or len({len(c["input"]) for c in cases}) != 1:
        return None
    # Reject NaN/Infinity and excessive nesting/size at the input boundary.
    try:
        json.dumps(cases, allow_nan=False)
    except (ValueError, RecursionError):
        return None
    return {"entrypoint": entrypoint, "cases": cases}


@lru_cache(maxsize=1)
def _runtime():
    import wasmtime
    binary = WASM_PATH.read_bytes()
    if sha256(binary).hexdigest() != WASM_SHA256:
        raise ValueError("python_wasm_integrity_error")
    config = wasmtime.Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    engine = wasmtime.Engine(config)
    return engine, wasmtime.Module(engine, binary)


atexit.register(_runtime.cache_clear)


def _run_case(code: str, entrypoint: str, arguments: list, seconds: float) -> dict:
    import wasmtime
    engine, module = _runtime()
    # No expected value is provided to the guest. No host dirs, sockets, env or
    # handles are inherited. Python and its stdlib are embedded in the wasm file.
    payload = json.dumps({"code": code, "name": entrypoint, "args": arguments}, ensure_ascii=True)
    script = (
        "import json,sys,io,contextlib\n"
        f"payload=json.loads({payload!r})\n"
        "try:\n"
        " with contextlib.redirect_stdout(io.StringIO()):\n"
        "  scope={}\n"
        "  exec(compile(payload['code'],'<answer>','exec'),scope)\n"
        "  result=scope[payload['name']](*payload['args'])\n"
        " output=json.dumps({'actual':result},allow_nan=False)\n"
        "except BaseException as error:\n"
        " output=json.dumps({'error':type(error).__name__,'message':str(error)[:500]})\n"
        "sys.stdout.write(output)\n"
    )
    captured, errors = bytearray(), bytearray()
    def capture(target, data):
        if len(target) + len(data) > _MAX_OUTPUT:
            engine.increment_epoch()
            return -1
        target.extend(data)
        return len(data)
    with wasmtime.Store(engine) as store, wasmtime.Linker(engine) as linker:
        store.set_limits(memory_size=256 * 1024 * 1024, instances=1, memories=1)
        store.set_fuel(10_000_000_000)
        store.set_epoch_deadline(1)
        wasi = wasmtime.WasiConfig()
        wasi.argv = ["python", "-I", "-S", "-c", script]
        wasi.stdout_custom = lambda data: capture(captured, data)
        wasi.stderr_custom = lambda data: capture(errors, data)
        store.set_wasi(wasi)
        linker.define_wasi()
        stop = threading.Event()
        cancelled = []
        deadline = time.monotonic() + seconds
        def watch():
            while not stop.wait(0.025):
                try:
                    check_turn_cancelled()
                except ToolCancelledError as exc:
                    cancelled.append(exc)
                    engine.increment_epoch()
                    return
                if time.monotonic() >= deadline:
                    engine.increment_epoch()
                    return
        context = copy_context()
        watcher = threading.Thread(target=lambda: context.run(watch), daemon=True)
        watcher.start()
        try:
            instance = linker.instantiate(store, module)
            instance.exports(store)["_start"](store)
        except wasmtime.ExitTrap as exc:
            if exc.code != 0:
                return {"error": "guest_exit", "message": errors.decode("utf-8", "replace")[-500:]}
        except (wasmtime.Trap, wasmtime.WasmtimeError) as exc:
            return {"error": "execution_limit", "message": str(exc)[-500:]}
        finally:
            stop.set()
            watcher.join()
            if cancelled:
                raise cancelled[0]
            check_turn_cancelled()
    try:
        result = json.loads(captured)
        if not isinstance(result, dict) or set(result) not in ({"actual"}, {"error", "message"}):
            raise ValueError("invalid output")
        return result
    except (ValueError, RecursionError):
        return {"error": "invalid_guest_output"}


def check_examples(code: str, specification: dict, *, seconds: float = 8.0) -> dict:
    """Bounded actual execution; infrastructure failure never means code passed."""
    if not code or len(code) > 24_000:
        return {"status": "unavailable", "reason": "unsupported_code_size", "results": []}
    deadline = time.monotonic() + seconds
    while not _RUN_LOCK.acquire(timeout=0.025):
        check_turn_cancelled()
        if time.monotonic() >= deadline:
            return {"status": "unavailable", "reason": "runner_busy", "results": []}
    try:
        check_turn_cancelled()
        try:
            _runtime()
        except (ImportError, OSError, ValueError):
            return {"status": "unavailable", "reason": "python_wasm_unavailable", "results": []}
        results = []
        for case in specification["cases"]:
            check_turn_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"status": "unavailable", "reason": "execution_budget_exhausted", "results": results}
            actual = _run_case(code, specification["entrypoint"], case["input"], min(remaining, 3.0))
            try:
                passed = ("actual" in actual and json.dumps(actual["actual"], sort_keys=True, allow_nan=False)
                          == json.dumps(case["expected"], sort_keys=True, allow_nan=False))
            except (TypeError, ValueError, RecursionError):
                passed = False
            results.append({**case, "actual": actual.get("actual"), "error": actual.get("error", ""),
                            "detail": actual.get("message", ""), "passed": passed, "executed": True})
        return {"status": "passed" if all(r["passed"] for r in results) else "failed", "results": results}
    finally:
        _RUN_LOCK.release()
