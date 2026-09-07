"""Real worker-process regression tests. No Office/COM or external app launch."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from core.artifact_transaction import ArtifactTransactionError
from core.plugin import (
    BasePlugin, CancellationToken, PluginContractError, PluginRegistry,
    ToolCancelledError, ToolExecutionContext, ToolSchema,
)
from core.plugin_worker import ProcessToolWorker, WorkerProtocolError, _write_json
from core.tool_result import Artifact, Evidence, ToolRunResult, ToolRunStatus
from plugins.office_editing import OfficeEditingPlugin


TOOL = "isolated_native_probe"


class IsolatedProbePlugin(BasePlugin):
    """Importable, stateless fake; sleeps model an uncooperative native call."""
    supports_process_isolation = True

    def __init__(self):
        super().__init__()
        self.name = "isolated_probe"

    def get_tools(self):
        return [ToolSchema(
            TOOL, "subprocess-only fake", {
                "type": "object", "required": ["marker"], "additionalProperties": False,
                "properties": {
                    "marker": {"type": "string"}, "late_path": {"type": "string"},
                    "delay": {"type": "number", "minimum": 0},
                    "mode": {"enum": ["success", "crash", "malformed", "unknown"]},
                },
            }, side_effect="change", timeout_seconds=8, cancellable=True,
            execution_isolation="process")]

    def execute_tool(self, tool_name, tool_input):
        context = self.get_execution_context()
        observed = {"pid": os.getpid(), "run_id": context.execution_id,
                    "workspace": context.staging_directory}
        Path(tool_input["marker"]).write_text(json.dumps(observed), encoding="utf-8")
        if tool_input.get("mode") == "crash":
            os._exit(7)
        time.sleep(tool_input.get("delay", 0))
        if tool_input.get("late_path"):
            Path(tool_input["late_path"]).write_text("native side effect", encoding="utf-8")
        if tool_input.get("mode") == "malformed":
            return ToolRunResult(tool_name, ToolRunStatus.SUCCEEDED, "not evidenced")
        if tool_input.get("mode") == "unknown":
            return ToolRunResult.successful(tool_name="different_run_target", raw_output="bad target",
                                            evidence=[Evidence("fake", "wrong tool")])
        return ToolRunResult.successful(tool_name=tool_name, raw_output="fake completed",
                                        evidence=[Evidence("fake_process", "observed child", observed)])


class FakeOfficeRendererPlugin(OfficeEditingPlugin):
    """Exercise the real staging/publish hooks, replacing COM only with a fake."""

    def execute_tool(self, name, data):
        source, output = Path(data["path"]), Path(data["output_pdf"])
        source.with_suffix(".started").write_text(str(os.getpid()), encoding="utf-8")
        if source.read_text(encoding="utf-8") == "pause":
            time.sleep(2.0)
        # This is an IPC/publication fixture, not a PDF-layout QA acceptance.
        output.write_bytes(b"%PDF-1.4\nfake PDF for worker publication test\n")
        preview = output.with_name(f"{output.stem}-page-1.png")
        preview.write_bytes(b"\x89PNG\r\n\x1a\nfake preview\n")
        details = {
            "pdf": str(output), "pages": 1, "nonblank_pages": 1,
            "source_snapshot": str(source),
            "previews": [str(preview)], "size": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "preview_sha256": {str(preview): hashlib.sha256(preview.read_bytes()).hexdigest()},
        }
        return ToolRunResult.successful(tool_name=name, raw_output="fake rendered",
            evidence=[Evidence("rendered_office", "fake render manifest", details)],
            artifacts=[Artifact("pdf", str(output)), Artifact("image", str(preview))])


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config.API_CONFIG, "ALLOWED_PATHS", [str(tmp_path)])
    registry = PluginRegistry()
    registry._process_workspace_root = str(tmp_path)
    registry.register_plugin(IsolatedProbePlugin())
    yield registry
    report = registry.shutdown(timeout_seconds=2)
    assert report["remaining_worker_pids"] == []


def _wait_file(path, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return
        time.sleep(0.01)
    raise AssertionError(f"worker did not enter fake native call: {path.name}")


def _office_marker(registry, source_name):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with registry._state_lock:
            state = registry._executions.get("run-test")
            worker = state.worker if state is not None else None
        if worker is not None:
            marker = worker.workspace / "source" / Path(source_name).with_suffix(".started")
            if marker.is_file() and marker.stat().st_size:
                return marker
        time.sleep(.01)
    raise AssertionError("fake Office renderer did not start")


def _background(registry, payload, run_id="run-test", tool=TOOL):
    results = {}
    thread = threading.Thread(target=lambda: results.setdefault(
        "result", registry.execute_tool(tool, payload, execution_id=run_id,
                                        idempotency_key=f"key-{run_id}")))
    thread.start()
    return thread, results


def _office_finalize_bundle(tmp_path, monkeypatch, *, token=None):
    """Build a two-page worker result and existing destination generation."""
    from config import Config

    monkeypatch.setattr(Config.API_CONFIG, "ALLOWED_PATHS", [str(tmp_path)])
    source = tmp_path / "source.docx"
    destination = tmp_path / "output.pdf"
    destination_previews = [
        tmp_path / "output-page-1.png",
        tmp_path / "output-page-2.png",
    ]
    source.write_text("fake office source", encoding="utf-8")
    destination.write_bytes(b"old PDF generation")
    for index, preview in enumerate(destination_previews, 1):
        preview.write_bytes(f"old PNG generation {index}".encode("ascii"))

    staging_root = tmp_path / "worker-stage"
    staging_root.mkdir()
    cancellation = token or CancellationToken()
    context = ToolExecutionContext(
        "office-finalize", "office_render_visual_qa", "office-finalize-key", 0,
        time.perf_counter(), cancellation, str(staging_root), time.monotonic() + 5,
    )
    plugin = OfficeEditingPlugin()
    plugin.registry = SimpleNamespace(current_execution_context=lambda: context)
    original_input = {"path": str(source), "output_pdf": str(destination)}
    isolated_input = plugin.prepare_isolated_input(
        "office_render_visual_qa", original_input, str(staging_root))

    staged_pdf = Path(isolated_input["output_pdf"])
    staged_pdf.write_bytes(b"%PDF-1.4\nnew PDF generation\n")
    staged_previews = []
    for index in range(1, 3):
        preview = staged_pdf.with_name(f"{staged_pdf.stem}-page-{index}.png")
        preview.write_bytes(b"\x89PNG\r\n\x1a\n" + f"new PNG generation {index}".encode("ascii"))
        staged_previews.append(preview)
    details = {
        "pdf": str(staged_pdf),
        "pages": 2,
        "nonblank_pages": 2,
        "previews": [str(item) for item in staged_previews],
        "sha256": hashlib.sha256(staged_pdf.read_bytes()).hexdigest(),
        "preview_sha256": {
            str(item): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in staged_previews
        },
    }
    result = ToolRunResult.successful(
        tool_name="office_render_visual_qa",
        raw_output="fake rendered",
        evidence=[Evidence("rendered_office", "two-page fake render", details)],
        artifacts=[
            Artifact("pdf", str(staged_pdf)),
            *[Artifact("image", str(item)) for item in staged_previews],
        ],
    )
    return SimpleNamespace(
        plugin=plugin,
        original_input=original_input,
        isolated_input=isolated_input,
        result=result,
        destination=destination,
        destination_previews=destination_previews,
        staged_pdf=staged_pdf,
        staged_previews=staged_previews,
    )


def _isolation(result):
    return next(item.data for item in result.evidence if item.kind == "process_isolation")


def test_worker_success_has_distinct_pid_typed_evidence_and_cleanup(isolated_registry, tmp_path):
    result = isolated_registry.execute_tool(TOOL, {"marker": str(tmp_path / "started.json")})
    assert result.succeeded, result.error
    observed = next(item.data for item in result.evidence if item.kind == "fake_process")
    isolation = _isolation(result)
    assert observed["pid"] != os.getpid()
    assert isolation["worker_pid"] == observed["pid"]
    assert isolation["worker_identity_verified"]
    assert isolation["worker_terminated"] and isolation["workspace_cleaned"]
    assert isolation["result_applied"] is True
    assert not Path(observed["workspace"]).exists()
    assert isolated_registry.get_active_execution_ids() == []


def test_real_deadline_kills_worker_before_late_file_write(isolated_registry, tmp_path):
    registry = isolated_registry
    registry._tools[TOOL][1].timeout_seconds = 1.5
    started, late = tmp_path / "started.json", tmp_path / "late.txt"
    before = time.monotonic()
    result = registry.execute_tool(TOOL, {"marker": str(started), "late_path": str(late), "delay": 2.3},
                                   execution_id="deadline", idempotency_key="no-repeat")
    elapsed = time.monotonic() - before
    assert started.exists(), result.error
    assert 1.3 <= elapsed < 3.0
    assert result.status == ToolRunStatus.UNVERIFIED
    assert "시간 초과" in result.error
    observed, proof = json.loads(started.read_text(encoding="utf-8")), _isolation(result)
    assert proof["worker_terminated"] and proof["external_effects_uncertain"]
    assert proof["result_applied"] is False and proof["workspace_cleaned"]
    assert not Path(observed["workspace"]).exists()
    # Wait past the fake native action's original write time. A merely timed
    # out ThreadPool future would create the file after returning to the UI.
    time.sleep(max(0.0, 2.8 - elapsed))
    assert not late.exists()
    duplicate = registry.execute_tool(TOOL, {"marker": str(started), "late_path": str(late), "delay": 2.3},
                                      execution_id="deadline-duplicate", idempotency_key="no-repeat")
    assert duplicate.to_dict() == result.to_dict()
    assert json.loads(started.read_text(encoding="utf-8"))["pid"] == observed["pid"]


def test_cancellation_terminates_only_its_worker_and_rejects_late_response(isolated_registry, tmp_path):
    first_marker, first_late = tmp_path / "first.json", tmp_path / "first-late.txt"
    second_marker = tmp_path / "second.json"
    first, first_results = _background(isolated_registry, {"marker": str(first_marker),
        "late_path": str(first_late), "delay": 1.5}, "first")
    second, second_results = _background(isolated_registry, {"marker": str(second_marker), "delay": 0.6}, "second")
    try:
        _wait_file(first_marker)
        _wait_file(second_marker)
        assert isolated_registry.cancel_execution("first")
        first.join(2)
        second.join(3)
        assert not first.is_alive() and not second.is_alive()
        assert first_results["result"].status == ToolRunStatus.UNVERIFIED
        assert _isolation(first_results["result"])["worker_terminated"]
        assert second_results["result"].succeeded
        assert _isolation(first_results["result"])["worker_pid"] != _isolation(second_results["result"])["worker_pid"]
        time.sleep(1)
        assert not first_late.exists()
    finally:
        isolated_registry.cancel_tool(TOOL)
        first.join(2)
        second.join(2)


@pytest.mark.parametrize("mode", ["crash", "malformed", "unknown"])
def test_crash_or_invalid_worker_envelope_is_uncertain_not_success(isolated_registry, tmp_path, mode):
    result = isolated_registry.execute_tool(TOOL, {"marker": str(tmp_path / f"{mode}.json"), "mode": mode})
    assert result.status == ToolRunStatus.UNVERIFIED, result
    proof = _isolation(result)
    assert proof["worker_terminated"] and proof["workspace_cleaned"]
    assert proof["external_effects_uncertain"] and proof["result_applied"] is False


def test_shutdown_is_bounded_and_rejects_new_dispatch(isolated_registry, tmp_path):
    marker, late = tmp_path / "shutdown.json", tmp_path / "late.txt"
    thread, results = _background(isolated_registry, {"marker": str(marker), "late_path": str(late), "delay": 5})
    try:
        _wait_file(marker)
        before = time.monotonic()
        report = isolated_registry.shutdown(timeout_seconds=1)
        assert time.monotonic() - before < 1.6
        thread.join(2)
        assert not thread.is_alive()
        assert report["remaining_worker_pids"] == []
        assert results["result"].status == ToolRunStatus.UNVERIFIED
        assert _isolation(results["result"])["worker_terminated"]
        assert not late.exists()
        denied = isolated_registry.execute_tool(TOOL, {"marker": str(tmp_path / "never.json")})
        assert denied.status == ToolRunStatus.CANCELLED
        assert not (tmp_path / "never.json").exists()
    finally:
        isolated_registry.cancel_tool(TOOL)
        thread.join(2)


def test_cancel_before_launch_does_not_spawn_process(isolated_registry, tmp_path):
    token = CancellationToken()
    token.cancel()
    result = isolated_registry.execute_tool(TOOL, {"marker": str(tmp_path / "never.json")}, cancellation_token=token)
    assert result.status == ToolRunStatus.CANCELLED
    assert not list(tmp_path.glob("jarvis-tool-worker-*"))
    assert not (tmp_path / "never.json").exists()


def test_cancel_while_waiting_for_worker_capacity_never_spawns(isolated_registry, tmp_path):
    registry = isolated_registry
    registry._process_slots.acquire()
    registry._process_slots.acquire()
    marker = tmp_path / "never.json"
    thread, results = _background(registry, {"marker": str(marker)})
    try:
        deadline = time.monotonic() + 1
        while not registry.get_active_execution_ids() and time.monotonic() < deadline:
            time.sleep(.01)
        assert registry.cancel_execution("run-test")
        thread.join(1)
        assert not thread.is_alive()
        assert results["result"].status == ToolRunStatus.CANCELLED
        assert not marker.exists()
        assert not list(tmp_path.glob("jarvis-tool-worker-*"))
    finally:
        registry._process_slots.release()
        registry._process_slots.release()
        thread.join(1)


def test_output_schema_rejection_never_enters_publication(isolated_registry, tmp_path, monkeypatch):
    registry = isolated_registry
    plugin, schema = registry._tools[TOOL]
    schema.output_schema = {"type": "object", "required": ["nonexistent_field"]}
    publication = []
    monkeypatch.setattr(plugin, "finalize_isolated_result", lambda *args: publication.append(True))
    result = registry.execute_tool(TOOL, {"marker": str(tmp_path / "invalid.json")})
    assert result.status == ToolRunStatus.UNVERIFIED
    assert publication == []
    assert _isolation(result)["result_applied"] is False


def test_mismatched_ready_pid_cannot_authorize_native_execution(tmp_path):
    worker = ProcessToolWorker("identity", TOOL, root=str(tmp_path))
    class ForeignProcess:
        pid = 456
        def poll(self): return None
    worker._process = ForeignProcess()
    _write_json(worker.ready_path, {"worker_pid": 789, "execution_id": "identity", "tool_name": TOOL})
    try:
        with pytest.raises(WorkerProtocolError, match="PID"):
            worker.wait(time.monotonic() + 1, CancellationToken())
        assert not worker.authorized_path.exists()
    finally:
        worker._process = None
        worker.cleanup()


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"execution_id": "stale-run"},
    {"tool_name": "another_tool"}, {"worker_pid": 999},
])
def test_result_envelope_cannot_cross_execution_or_process_boundary(tmp_path, change):
    worker = ProcessToolWorker("current-run", TOOL, root=str(tmp_path))
    class FinishedProcess:
        pid = 456
        returncode = 0
        def poll(self): return 0
    worker._process = FinishedProcess()
    worker.identity_verified = True
    result = ToolRunResult.successful(tool_name=TOOL, raw_output="ok", evidence=[Evidence("fake", "proof")])
    _write_json(worker.result_path, {"version": 1, "execution_id": "current-run", "tool_name": TOOL,
                                    "worker_pid": 456, "kind": "tool_result", "result": result.to_dict(), **change})
    try:
        with pytest.raises(WorkerProtocolError):
            worker.wait(time.monotonic() + 1, CancellationToken())
    finally:
        worker.cleanup()


def test_valid_but_late_result_is_not_accepted(tmp_path):
    worker = ProcessToolWorker("late", TOOL, root=str(tmp_path))
    class FinishedProcess:
        pid = 456
        returncode = 0
        def poll(self): return 0
    worker._process = FinishedProcess()
    worker.identity_verified = True
    result = ToolRunResult.successful(tool_name=TOOL, raw_output="ok", evidence=[Evidence("fake", "proof")])
    _write_json(worker.result_path, {"version": 1, "execution_id": "late", "tool_name": TOOL,
                                    "worker_pid": 456, "kind": "tool_result", "result": result.to_dict()})
    try:
        with pytest.raises(TimeoutError):
            worker.wait(time.monotonic() - .01, CancellationToken())
    finally:
        worker.cleanup()


def test_cancellation_before_publication_guard_blocks_side_effect():
    token = CancellationToken()
    token.cancel()
    committed = []
    with pytest.raises(Exception, match="취소"):
        with token.publication_guard():
            committed.append(True)
    assert committed == []


def test_cancellation_does_not_wait_for_inflight_publish_or_claim_rollback():
    token = CancellationToken()
    started, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    committed = []
    def publish():
        with token.publication_guard():
            started.set()
            assert release.wait(1)
            committed.append("already-started effect cannot be rolled back")
    publisher = threading.Thread(target=publish)
    canceller = threading.Thread(target=lambda: (token.cancel(), cancelled.set()))
    publisher.start()
    assert started.wait(1)
    canceller.start()
    try:
        assert cancelled.wait(.2)
        release.set()
        publisher.join(1)
        canceller.join(1)
        assert cancelled.is_set() and token.cancelled
        assert len(committed) == 1
    finally:
        release.set()
        publisher.join(1)
        canceller.join(1)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "10"])
def test_invalid_deadline_policy_cannot_create_unbounded_worker(timeout):
    schema = IsolatedProbePlugin().get_tools()[0]
    schema.timeout_seconds = timeout
    with pytest.raises(PluginContractError, match="timeout"):
        PluginRegistry._validate_schema_definition(schema)


def test_serialization_rejected_before_native_spawn(isolated_registry, tmp_path):
    result = isolated_registry.execute_tool(TOOL, {"marker": str(tmp_path / "never.json"), "delay": float("nan")})
    assert result.status == ToolRunStatus.FAILED
    proof = _isolation(result)
    assert proof["worker_pid"] is None and not proof["external_effects_uncertain"]
    assert not (tmp_path / "never.json").exists()
    assert not list(tmp_path.glob("jarvis-tool-worker-*"))


def test_active_timed_out_thread_cannot_be_pruned_from_dedup(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    class LegacyNativePlugin(BasePlugin):
        def __init__(self):
            super().__init__()
            self.name = "legacy_native"
        def get_tools(self):
            return [ToolSchema("legacy_native_write", "fake", side_effect="change", timeout_seconds=.08)]
        def execute_tool(self, name, data):
            calls.append(1)
            started.set()
            release.wait(5)
            return "late result"

    registry = PluginRegistry()
    registry.register_plugin(LegacyNativePlugin())
    thread, results = _background(registry, {}, tool="legacy_native_write")
    try:
        assert started.wait(1)
        thread.join(1)
        assert results["result"].status == ToolRunStatus.FAILED
        registry._deduplication_ttl_seconds = -1
        registry._deduplication_max_records = 0
        duplicate = registry.execute_tool("legacy_native_write", {}, execution_id="second", idempotency_key="key-run-test")
        assert duplicate.to_dict() == results["result"].to_dict()
        assert calls == [1]
        report = registry.shutdown(timeout_seconds=.05)
        assert report["inprocess_threads_cannot_be_forcibly_stopped"] is True
        assert report["remaining_execution_ids"] == ["run-test"]
    finally:
        release.set()
        thread.join(1)
        registry.shutdown(timeout_seconds=1)


def test_process_opt_in_rejects_unimportable_or_stateful_factory():
    class LocalPlugin(IsolatedProbePlugin):
        pass
    with pytest.raises(PluginContractError, match="import 가능한"):
        PluginRegistry().register_plugin(LocalPlugin())
    plugin = IsolatedProbePlugin()
    schema = plugin.get_tools()[0]
    schema.execution_isolation = "unknown"
    with pytest.raises(PluginContractError, match="execution_isolation"):
        PluginRegistry._validate_schema_definition(schema)


@pytest.mark.parametrize("change", [
    {"status": "complete"}, {"tool_name": "another"}, {"raw_output": []},
    {"duration_ms": True}, {"duration_ms": float("nan")}, {"duration_ms": -1},
    {"error": "native export failed"},
    {"evidence": {}}, {"evidence": []}, {"evidence": [{"kind": "x", "summary": []}]},
    {"artifacts": [{"kind": "file", "uri": 7}]},
])
def test_worker_result_codec_rejects_coercion_or_unsupported_truth(change):
    value = ToolRunResult.successful(tool_name=TOOL, raw_output="ok", evidence=[Evidence("fake", "proof")]).to_dict()
    with pytest.raises((ValueError, TypeError)):
        ToolRunResult.from_dict({**value, **change}, expected_tool=TOOL)


def test_worker_cleanup_refuses_changed_target(tmp_path):
    worker = ProcessToolWorker("cleanup", TOOL, root=str(tmp_path))
    owned = worker.workspace
    survivor = tmp_path / "user-data"
    survivor.mkdir()
    (survivor / "keep.txt").write_text("keep", encoding="utf-8")
    worker.workspace = survivor
    assert not worker.cleanup()
    assert (survivor / "keep.txt").is_file()
    worker.workspace = owned
    assert worker.cleanup()


def test_office_only_stateless_renderer_is_process_isolated():
    tools = {tool.name: tool for tool in OfficeEditingPlugin().get_tools()}
    assert tools["office_render_visual_qa"].execution_isolation == "process"
    assert tools["office_render_visual_qa"].cancellable
    assert all(tool.execution_isolation == "thread" for name, tool in tools.items()
               if name != "office_render_visual_qa")


def test_office_staging_publishes_only_timely_verified_artifacts(tmp_path, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config.API_CONFIG, "ALLOWED_PATHS", [str(tmp_path)])
    source, output = tmp_path / "source.docx", tmp_path / "output.pdf"
    source.write_text("fake", encoding="utf-8")
    output.write_bytes(b"original user result")
    registry = PluginRegistry()
    registry._process_workspace_root = str(tmp_path)
    registry.register_plugin(FakeOfficeRendererPlugin())
    try:
        result = registry.execute_tool("office_render_visual_qa", {"path": str(source), "output_pdf": str(output)})
        assert result.succeeded, result.error
        assert output.read_bytes().startswith(b"%PDF-")
        snapshot = Path(result.evidence[0].data["source_snapshot"])
        assert snapshot != source and not snapshot.exists()
        assert source.read_text(encoding="utf-8") == "fake"
        assert output.with_name("output-page-1.png").read_bytes().startswith(b"\x89PNG")
        assert {item.uri for item in result.artifacts} == {str(output), str(output.with_name("output-page-1.png"))}
        assert _isolation(result)["workspace_cleaned"]
        assert not list(tmp_path.glob("jarvis-tool-worker-*"))
        assert not list(tmp_path.glob(".jarvis-render-*"))
    finally:
        registry.shutdown()


def test_office_finalizer_publishes_pdf_and_all_previews_as_verified_bundle(tmp_path, monkeypatch):
    case = _office_finalize_bundle(tmp_path, monkeypatch)

    finalized = case.plugin.finalize_isolated_result(
        "office_render_visual_qa", case.original_input, case.isolated_input, case.result)

    assert finalized.succeeded
    assert case.destination.read_bytes() == case.staged_pdf.read_bytes()
    assert [item.read_bytes() for item in case.destination_previews] == [
        item.read_bytes() for item in case.staged_previews
    ]
    assert {item.uri for item in finalized.artifacts} == {
        str(case.destination), *[str(item) for item in case.destination_previews]
    }
    details = finalized.evidence[0].data
    assert set(details["publication"]) == {
        str(case.destination), *[str(item) for item in case.destination_previews]
    }
    assert all(item["verified"] for item in details["publication"].values())
    assert details["publication_contract"] == {
        "prevalidated_bundle": True,
        "rollback_on_process_failure": True,
        "atomicity": "per_file_with_bundle_rollback",
        "crash_or_power_loss_atomicity": False,
    }


def test_office_finalizer_rolls_back_first_preview_when_second_replace_fails(tmp_path, monkeypatch):
    import core.artifact_transaction as transaction

    case = _office_finalize_bundle(tmp_path, monkeypatch)
    old_pdf = case.destination.read_bytes()
    old_previews = [item.read_bytes() for item in case.destination_previews]
    real_replace = transaction.os.replace
    publication_replaces = 0

    def fail_second_publication(source, destination):
        nonlocal publication_replaces
        if Path(source).name.startswith(".anis-artifact-stage-"):
            publication_replaces += 1
            if publication_replaces == 2:
                raise OSError("simulated second Office artifact replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(transaction.os, "replace", fail_second_publication)
    with pytest.raises(ArtifactTransactionError) as caught:
        case.plugin.finalize_isolated_result(
            "office_render_visual_qa", case.original_input, case.isolated_input, case.result)

    assert publication_replaces == 2
    assert caught.value.rolled_back
    assert not caught.value.recovery_files
    assert case.destination.read_bytes() == old_pdf
    assert [item.read_bytes() for item in case.destination_previews] == old_previews
    assert not list(tmp_path.glob(".anis-artifact-*.tmp"))


def test_office_finalizer_cancel_at_publication_boundary_keeps_previous_bundle(tmp_path, monkeypatch):
    class CancelAtPublicationToken(CancellationToken):
        @contextmanager
        def publication_guard(self):
            self.cancel()
            with CancellationToken.publication_guard(self):
                yield

    token = CancelAtPublicationToken()
    case = _office_finalize_bundle(tmp_path, monkeypatch, token=token)
    old_pdf = case.destination.read_bytes()
    old_previews = [item.read_bytes() for item in case.destination_previews]
    called = False

    def unexpected_commit(_mapping):
        nonlocal called
        called = True
        raise AssertionError("cancelled Office bundle must not enter artifact commit")

    monkeypatch.setattr("plugins.office_editing.commit_artifact_bundle", unexpected_commit)
    with pytest.raises(ToolCancelledError):
        case.plugin.finalize_isolated_result(
            "office_render_visual_qa", case.original_input, case.isolated_input, case.result)

    assert token.cancelled
    assert not called
    assert case.destination.read_bytes() == old_pdf
    assert [item.read_bytes() for item in case.destination_previews] == old_previews


def test_office_cancel_does_not_overwrite_existing_final_files(tmp_path, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config.API_CONFIG, "ALLOWED_PATHS", [str(tmp_path)])
    source, output = tmp_path / "source.docx", tmp_path / "output.pdf"
    source.write_text("pause", encoding="utf-8")
    output.write_bytes(b"existing PDF")
    preview = output.with_name("output-page-1.png")
    preview.write_bytes(b"existing PNG")
    registry = PluginRegistry()
    registry._process_workspace_root = str(tmp_path)
    registry.register_plugin(FakeOfficeRendererPlugin())
    thread, results = _background(registry, {"path": str(source), "output_pdf": str(output)}, tool="office_render_visual_qa")
    try:
        _office_marker(registry, source.name)
        registry.cancel_execution("run-test")
        thread.join(2)
        assert not thread.is_alive()
        assert results["result"].status == ToolRunStatus.UNVERIFIED
        time.sleep(2)
        assert output.read_bytes() == b"existing PDF"
        assert preview.read_bytes() == b"existing PNG"
        assert not list(tmp_path.glob("jarvis-tool-worker-*"))
    finally:
        registry.shutdown()
        thread.join(2)


@pytest.mark.parametrize("corruption", ["content", "outside", "pages", "artifact_list"])
def test_office_finalizer_rejects_corrupt_or_unscoped_staged_output(tmp_path, monkeypatch, corruption):
    from config import Config
    monkeypatch.setattr(Config.API_CONFIG, "ALLOWED_PATHS", [str(tmp_path)])
    source, output, stage = tmp_path / "source.docx", tmp_path / "output.pdf", tmp_path / "stage"
    source.write_text("fake", encoding="utf-8")
    output.write_bytes(b"keep final")
    stage.mkdir()
    plugin = FakeOfficeRendererPlugin()
    token = CancellationToken()
    context = ToolExecutionContext("finalize", "office_render_visual_qa", "", 0, time.perf_counter(), token,
                                   str(stage), time.monotonic() + 5)
    plugin.registry = SimpleNamespace(current_execution_context=lambda: context)
    original = {"path": str(source), "output_pdf": str(output)}
    prepared = plugin.prepare_isolated_input("office_render_visual_qa", original, str(stage))
    result = plugin.execute_tool("office_render_visual_qa", prepared)
    if corruption == "content":
        Path(prepared["output_pdf"]).write_bytes(b"%PDF-tampered")
    elif corruption == "outside":
        result.evidence[0].data["previews"] = [str(output)]
    elif corruption == "pages":
        result.evidence[0].data["pages"] = True
    else:
        result.artifacts = []
    with pytest.raises(ValueError):
        plugin.finalize_isolated_result("office_render_visual_qa", original, prepared, result)
    assert output.read_bytes() == b"keep final"
    assert not list(tmp_path.glob(".jarvis-render-*"))


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
def test_isolated_office_renderer_never_quits_or_hides_shared_application(tmp_path, monkeypatch, suffix):
    import pythoncom
    import win32com.client
    from core.office_runtime import OfficeRenderer
    calls = []
    class Document:
        def ExportAsFixedFormat(self, *args): calls.append(("export", args))
        def SaveAs(self, *args): calls.append(("export", args))
        def Close(self, *args): calls.append(("close_document", args))
    class Collection:
        def Open(self, path, **kwargs):
            calls.append(("open", path, kwargs))
            return Document()
    class ExistingUserApp:
        Visible = True
        Documents = Workbooks = Presentations = Collection()
        def Quit(self): calls.append(("quit_user_app",))
    app = ExistingUserApp()
    monkeypatch.setattr(win32com.client, "DispatchEx", lambda progid: app)
    monkeypatch.setattr(pythoncom, "CoInitialize", lambda: None)
    monkeypatch.setattr(pythoncom, "CoUninitialize", lambda: calls.append(("uninitialize",)))
    monkeypatch.setattr(OfficeRenderer, "verify_pdf", lambda path: {"pdf": str(path)})
    source, output = tmp_path / f"unique-snapshot{suffix}", tmp_path / "out.pdf"
    OfficeRenderer.render_pdf(source, output, protect_existing_application=True)
    assert app.Visible is True
    assert not any(item[0] == "quit_user_app" for item in calls)
    assert sum(item[0] == "close_document" for item in calls) == 1
    assert next(item for item in calls if item[0] == "close_document")[1] == (() if suffix == ".pptx" else (False,))
    opened = next(item for item in calls if item[0] == "open")
    assert opened[1] == str(source) and opened[2]["ReadOnly"] is True
    assert calls[-1] == ("uninitialize",)


def test_worker_entrypoint_does_not_bootstrap_qt_or_models():
    import subprocess
    import sys
    command = [sys.executable, str(Path(__file__).with_name("main_qt.py")), "--plugin-worker", "--help"]
    response = subprocess.run(command, capture_output=True, text=True, timeout=5,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert response.returncode == 0
    assert "--request" in response.stdout and "--result" in response.stdout
    assert "[Plugin]" not in response.stdout and "[LLM]" not in response.stdout


def test_main_shutdown_calls_registry_even_after_other_cleanup_failure(monkeypatch):
    from main_qt import JarvisApp
    calls = []
    def registry_shutdown(timeout_seconds):
        calls.append(timeout_seconds)
        return {"remaining_execution_ids": ["legacy-hang"]}
    app = JarvisApp.__new__(JarvisApp)
    app.tool_executor = SimpleNamespace(plugin_registry=SimpleNamespace(shutdown=registry_shutdown))
    app._shutdown_runtime()
    app._shutdown_runtime()
    assert calls == [2.0]
    assert any("legacy-hang" in error for error in app._shutdown_errors)
