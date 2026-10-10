"""K3 transport/lifecycle tests use a tiny child process, never real weights."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import psutil

if __name__ == "__main__" and "--fake-kimi" in sys.argv:
    history, mode = Path(sys.argv[-2]), sys.argv[-1]
    if mode in {"spawn", "spawn_exit"}:
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        pid_record = history.parent / "worker.pid.pending"
        pid_record.write_text(str(worker.pid))
        pid_record.replace(history.parent / "worker.pid")
        mode = "hang" if mode == "spawn" else "ok"
    if mode == "hang":
        time.sleep(120)
    if mode != "missing":
        content = '수정안 😀\n{"edits": []}'
        if mode == "patch":
            content = json.dumps({"summary": "Update setting", "change_kind": "config", "edits": [
                {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}]})
        elif mode == "malformed":
            content = '{"edits": ['
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"role": "assistant", "content": content}, ensure_ascii=False) + "\n")
    sys.exit(4 if mode == "error" else 0)

import pytest

from core import auxiliary_models as selection
from core import local_inference as gate
from core.assistant_settings import AssistantSettings
from core.kimi_client import KimiClient, KimiOptions, configuration_status
from core.llm import ModelCallError
from core.plugin import ToolCancelledError
from core.turn_context import TurnExecutionContext, bind_turn_context


@pytest.fixture
def isolated(monkeypatch):
    values = {}
    profile = SimpleNamespace(get_preference=lambda k, d: values.get(k, d),
                              set_preference=lambda k, v: values.__setitem__(k, v))
    settings = AssistantSettings(profile)
    monkeypatch.setattr(selection, "get_assistant_settings", lambda: settings)
    monkeypatch.setattr(selection, "_generation", 0)
    monkeypatch.setattr(selection, "_jev_generation", 0)
    monkeypatch.setattr(selection, "_shutdown", False)
    monkeypatch.setattr(selection, "_status", "대기")
    monkeypatch.setattr(selection, "_jev_status", "대기")
    monkeypatch.setattr(gate, "_idle_releases", {})
    for key in tuple(os.environ):
        if key.startswith("KIMI_K3_"):
            monkeypatch.delenv(key)
    return settings


@pytest.fixture
def engine(tmp_path, monkeypatch, isolated):
    import core.kimi_client as module
    selection.configure("kimi_k3", True)
    model, trunk = tmp_path / "모델", tmp_path / "트렁크"
    model.mkdir(); trunk.mkdir()
    for name in ("config.json", "tiktoken.model", "tokenizer_config.json", "part.safetensors"):
        (model / name).write_text("fixture", encoding="utf-8")
    (model / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"weight": "part.safetensors"}}))
    for name in ("trunk.bin", "trunk.json"):
        (trunk / name).write_text("fixture")
    monkeypatch.setenv("KIMI_K3_EXECUTABLE", sys.executable)
    monkeypatch.setenv("KIMI_K3_MODEL_DIR", str(model))
    monkeypatch.setenv("KIMI_K3_TRUNK_DIR", str(trunk))
    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=32 * 1024**3))
    monkeypatch.setattr(KimiClient, "_ensure_ollama_idle", lambda self: None)
    original_popen = subprocess.Popen
    state = SimpleNamespace(mode="ok", commands=[], children=[], history=None, started=threading.Event())
    def launch(command, **kwargs):
        if "--history" not in command:
            return original_popen(command, **kwargs)
        state.commands.append((command, kwargs))
        history = Path(command[command.index("--history") + 1])
        state.history = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
        process = original_popen([sys.executable, "-u", str(Path(__file__).resolve()), "--fake-kimi", str(history), state.mode], **kwargs)
        state.children.append(process)
        state.started.set()
        return process
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    yield state
    for process in state.children:
        if process.poll() is None:
            process.kill(); process.wait(timeout=3)


def test_selection_defaults_off_persists_and_invalidates_old_requests(isolated):
    assert selection.selection() == ("default", True)
    client = KimiClient()
    selection.configure("kimi_k3", True)
    assert isolated.get("auxiliary_model_enabled") == "true"
    with pytest.raises(ModelCallError, match="설정 변경"):
        client._check()
    with pytest.raises(ValueError):
        selection.configure("invented", True)


def test_missing_configuration_never_launches(monkeypatch, isolated):
    selection.configure("kimi_k3", True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected process"))
    assert not configuration_status()[0]
    with pytest.raises(ModelCallError, match="KIMI_K3_EXECUTABLE"):
        KimiClient().chat([{"role": "user", "content": "hello"}])


def test_jev_changes_keep_active_kimi_ticket_and_main_status(isolated):
    selection.configure("kimi_k3", True)
    client = KimiClient()
    selection.set_status("Kimi K3 실행 중")
    selection.configure("jev", True)
    selection.set_status("Jev 분류 중", "jev")
    client._check()
    assert selection.main_selection() == ("kimi_k3", True)
    assert selection.is_enabled("jev")
    assert selection.status() == "Kimi K3 실행 중"
    assert selection.status("jev") == "Jev 분류 중"


def test_bad_optional_timeout_does_not_break_base_tool_registration(engine, monkeypatch):
    for value in ("", "invalid", "0", "999999"):
        monkeypatch.setenv("KIMI_K3_TIMEOUT_SECONDS", value)
        assert selection.coding_timeout_seconds() == 22200
        with pytest.raises(ModelCallError) as caught:
            KimiClient().chat([{"role": "user", "content": "hello"}])
        assert caught.value.code == "configuration"
    assert not engine.children


def test_real_child_receives_exact_json_history_and_is_joined(engine):
    message = '한국어\n/exit\n"코드" 😀 <|end_of_msg|>'
    answer = KimiClient().chat([{"role": "system", "content": "코딩 지침"}, {"role": "user", "content": message}])
    assert answer == '수정안 😀\n{"edits": []}'
    assert engine.history == [{"role": "system", "content": "코딩 지침"}, {"role": "user", "content": message}]
    command, kwargs = engine.commands[0]
    assert "--chat" in command and "--no-think" in command and "--trunk" in command
    assert "--out" not in command and message not in command
    assert kwargs["shell"] is False and int(kwargs["env"]["OMP_THREAD_LIMIT"]) <= 4
    assert all(child.poll() == 0 for child in engine.children)
    assert not Path(kwargs["cwd"]).exists()


@pytest.mark.parametrize("mode,code", [("error", "engine"), ("missing", "protocol")])
def test_failed_or_unfinished_generation_is_never_patch_text(engine, mode, code):
    engine.mode = mode
    with pytest.raises(ModelCallError) as caught:
        KimiClient().chat([{"role": "user", "content": "코드"}])
    assert caught.value.code == code
    assert all(child.poll() is not None for child in engine.children)


@pytest.mark.parametrize("reason", ["off", "turn", "shutdown", "tool"])
def test_inflight_cancellation_stops_owned_child_and_frees_slot(engine, reason):
    engine.mode = "hang"
    context = TurnExecutionContext("k3", "test")
    tool_cancelled = threading.Event()
    def tool_check():
        if tool_cancelled.is_set():
            raise ToolCancelledError("tool cancelled")
    client = KimiClient(cancellation_check=tool_check)
    def run():
        with bind_turn_context(context):
            return client.chat([{"role": "user", "content": "코드"}])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        assert engine.started.wait(5)
        if reason == "off":
            selection.configure("kimi_k3", False)
        elif reason == "turn":
            context.cancel()
        elif reason == "shutdown":
            selection.shutdown()
        else:
            tool_cancelled.set()
        with pytest.raises((ModelCallError, ToolCancelledError)):
            future.result(timeout=5)
    assert all(child.poll() is not None for child in engine.children)
    with gate.local_inference(timeout=1):
        pass


def test_wall_timeout_stops_owned_process(engine, monkeypatch):
    engine.mode = "hang"
    monkeypatch.setenv("KIMI_K3_TIMEOUT_SECONDS", "1")
    with pytest.raises(ModelCallError) as caught:
        KimiClient().chat([{"role": "user", "content": "코드"}])
    assert caught.value.code == "timeout", str(caught.value)
    assert engine.children[0].poll() is not None
    assert not Path(engine.commands[0][1]["cwd"]).exists()


def test_cancellation_stops_descendants_and_releases_log(engine):
    engine.mode = "spawn"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(KimiClient().chat, [{"role": "user", "content": "test"}])
        assert engine.started.wait(5)
        directory = Path(engine.commands[0][1]["cwd"])
        pid_file = directory / "worker.pid"
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            assert pid_file.exists()
            worker = psutil.Process(int(pid_file.read_text()))
        finally:
            selection.configure("kimi_k3", False)
        with pytest.raises(ModelCallError) as caught:
            future.result(timeout=10)
    assert caught.value.code == "disabled"
    assert not worker.is_running()
    assert engine.children[0].poll() is not None
    assert not directory.exists()


def test_low_memory_and_oversized_input_refuse_before_launch(engine, monkeypatch):
    import core.kimi_client as module
    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=8 * 1024**3))
    with pytest.raises(ModelCallError) as caught:
        KimiClient().chat([{"role": "user", "content": "코드"}])
    assert caught.value.code == "memory" and not engine.children
    with pytest.raises(ModelCallError) as caught:
        KimiClient().chat([{"role": "user", "content": "가" * 12000}])
    assert caught.value.code == "context" and not engine.children


def test_runtime_memory_limit_terminates_child(engine, monkeypatch):
    import core.kimi_client as module
    engine.mode = "hang"
    monkeypatch.setattr(module.psutil.Process, "memory_info",
                        lambda self: SimpleNamespace(rss=11 * 1024**3))
    with pytest.raises(ModelCallError) as caught:
        KimiClient().chat([{"role": "user", "content": "코드"}])
    assert caught.value.code == "memory", str(caught.value)
    assert engine.children[0].poll() is not None
    assert not Path(engine.commands[0][1]["cwd"]).exists()


def test_runtime_memory_limit_includes_workers(engine, monkeypatch):
    import core.kimi_client as module
    engine.mode = "spawn"
    monkeypatch.setattr(module.psutil.Process, "memory_info", lambda self: SimpleNamespace(
        rss=0 if self.pid == engine.children[0].pid else 11 * 1024**3))
    with pytest.raises(ModelCallError) as caught:
        KimiClient().chat([{"role": "user", "content": "test"}])
    assert caught.value.code == "memory", str(caught.value)
    assert engine.children[0].poll() is not None
    assert not Path(engine.commands[0][1]["cwd"]).exists()


def test_successful_launcher_exit_still_joins_owned_workers(engine):
    engine.mode = "spawn_exit"
    assert KimiClient().chat([{"role": "user", "content": "test"}])
    assert engine.children[0].poll() == 0
    assert not Path(engine.commands[0][1]["cwd"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native Job ownership")
def test_windows_job_is_assigned_suspended_without_child_inspection(engine, monkeypatch):
    import core.kimi_client as module
    original = module._WindowsJob.assign
    assigned = []
    def assign(job, process):
        assert engine.commands[-1][1]["creationflags"] & 0x4
        assert job.pids() == []
        original(job, process)
        assert job.pids() == [process.pid]
        assigned.append(process.pid)
    monkeypatch.setattr(module._WindowsJob, "assign", assign)
    monkeypatch.setattr(module.psutil.Process, "children", lambda *_a, **_k: (_ for _ in ()).throw(
        psutil.AccessDenied("injected child inspection failure")))
    assert KimiClient().chat([{"role": "user", "content": "test"}])
    assert assigned == [engine.children[0].pid]
    assert not Path(engine.commands[0][1]["cwd"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native Job ownership")
def test_windows_lifecycle_never_suspends_or_resumes_by_pid(engine, monkeypatch):
    import core.kimi_client as module
    engine.mode = "spawn"
    for method in ("suspend", "resume"):
        monkeypatch.setattr(module.psutil.Process, method,
                            lambda *_a: pytest.fail("lifecycle must use an owned kernel handle"))
    with pytest.raises(ModelCallError) as caught:
        KimiClient().chat_structured([{"role": "user", "content": "test"}], request_timeout=1)
    assert caught.value.code == "timeout"
    assert engine.children[0].poll() is not None
    assert not Path(engine.commands[0][1]["cwd"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows native Job ownership")
def test_windows_job_assignment_failure_never_resumes_child(engine, monkeypatch):
    import core.kimi_client as module
    monkeypatch.setattr(module._WindowsJob, "assign", lambda *_a: (_ for _ in ()).throw(
        RuntimeError("injected assignment failure")))
    with pytest.raises(ModelCallError, match="assignment failure"):
        KimiClient().chat([{"role": "user", "content": "test"}])
    assert engine.children[0].poll() is not None
    assert not Path(engine.commands[0][1]["cwd"]).exists()


@pytest.mark.parametrize("reason", ["memory", "timeout", "cancel"])
def test_cleanup_failure_preserves_primary_error_with_diagnostics(engine, monkeypatch, caplog, reason):
    import core.kimi_client as module
    engine.mode = "hang"
    client = KimiClient()
    original_check = client._check_memory
    def reject(options, *, process=None, **kwargs):
        if process is not None:
            if reason == "cancel":
                raise ToolCancelledError("injected cancellation")
            raise client._error(reason, "injected rejection")
        return original_check(options, process=process, **kwargs)
    monkeypatch.setattr(client, "_check_memory", reject)
    original_cleanup = module.tempfile.TemporaryDirectory.cleanup
    def cleanup(temporary):
        original_cleanup(temporary)
        raise OSError("injected cleanup failure")
    monkeypatch.setattr(module.tempfile.TemporaryDirectory, "cleanup", cleanup)
    with pytest.raises(ToolCancelledError if reason == "cancel" else ModelCallError) as caught:
        client.chat([{"role": "user", "content": "test"}])
    if reason != "cancel":
        assert caught.value.code == reason
    assert "injected cleanup failure" in caught.value.cleanup_errors[0]
    assert caught.value.__notes__ and "temporary files" in caplog.text
    assert engine.children[0].poll() is not None
    assert not Path(engine.commands[0][1]["cwd"]).exists()


def test_external_ollama_residency_blocks_without_unloading(isolated, monkeypatch):
    import core.kimi_client as module
    monkeypatch.setattr(module.requests, "get", lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"models": [{"name": "other-app"}]}))
    monkeypatch.setattr(module.requests, "post", lambda *a, **k: pytest.fail("external model unload"))
    with pytest.raises(ModelCallError) as caught:
        KimiClient()._ensure_ollama_idle()
    assert caught.value.code == "busy"


def test_k3_complete_json_flows_through_existing_patch_validation(engine, tmp_path):
    from core.coding_agent import CodingAgent
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    engine.mode = "malformed"
    with pytest.raises(ValueError, match="JSON"):
        CodingAgent(root).execute_request("app.py 설정 변경", KimiClient())
    assert source.read_text(encoding="utf-8") == "value = 1\n"
    engine.mode = "patch"
    _, result = CodingAgent(root).execute_request("app.py 설정 변경", KimiClient())
    assert result.succeeded and result.validation_output
    assert source.read_text(encoding="utf-8") == "value = 2\n"


def test_models_serialize_and_cancelled_waiter_never_enters(isolated):
    entered, release, later = threading.Event(), threading.Event(), threading.Event()
    context = TurnExecutionContext("queued", "test")
    @gate.serialized_inference
    def first():
        entered.set()
        assert release.wait(5)
    def second():
        with bind_turn_context(context), gate.local_inference():
            later.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first)
        assert entered.wait(2)
        waiter = pool.submit(second)
        context.cancel()
        try:
            with pytest.raises(ToolCancelledError):
                waiter.result(timeout=2)
            assert not later.is_set()
        finally:
            release.set()
        owner.result(timeout=2)


def test_idle_release_happens_before_k3_spawn_and_failure_stops_it(engine):
    releases = []
    gate.remember_idle_model("owned-ollama", lambda: releases.append("released") or True)
    KimiClient().chat([{"role": "user", "content": "코드"}])
    assert releases == ["released"] and not gate._idle_releases
    gate.remember_idle_model("cannot-unload", lambda: False)
    with pytest.raises(ModelCallError, match="해제하지 못해"):
        KimiClient().chat([{"role": "user", "content": "코드"}])
    assert len(engine.children) == 1


def test_coding_factory_switches_but_tool_client_does_not(isolated, monkeypatch):
    import core.llm as llm
    from core.executor import _local_answer_draft_client
    fallback = object()
    monkeypatch.setattr(llm, "get_llm_client", lambda role: fallback)
    assert llm.get_coding_llm_client() is fallback
    selection.configure("kimi_k3", True)
    assert isinstance(llm.get_coding_llm_client(), KimiClient)
    assert isinstance(_local_answer_draft_client(SimpleNamespace(requires_code=True)), KimiClient)
    assert not isinstance(_local_answer_draft_client(SimpleNamespace(requires_code=False)), KimiClient)
    with pytest.raises(ModelCallError, match="tool calling"):
        llm.get_coding_llm_client().chat_with_tools([])
    selection.configure("kimi_k3", False)
    assert llm.get_coding_llm_client() is fallback


def test_queued_k3_off_then_on_never_revives_old_call(engine):
    client = KimiClient()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with gate.local_inference():
            future = pool.submit(client.chat, [{"role": "user", "content": "코드"}])
            selection.configure("kimi_k3", False)
            selection.configure("kimi_k3", True)
        with pytest.raises(ModelCallError, match="설정 변경"):
            future.result(timeout=3)
    assert not engine.children
