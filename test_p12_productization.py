import hashlib
import json
import sqlite3
import zipfile

import pytest

from core.productization import (BootstrapInstaller, DatabaseMigrator, DiagnosticReporter,
    E2EProbeRunner, JsonTraceLogger, RuntimeMetrics, SafeModeManager, ScopedPermissionStore,
    SensitiveDataRedactor, UpdateManager, trace_context)


def test_redaction_masks_secrets_and_pii():
    value = SensitiveDataRedactor.redact({"api_key": "abc", "note": "Bearer xyz user@example.com 010-1234-5678"})
    assert value["api_key"] == "[REDACTED]"
    assert "xyz" not in value["note"] and "example.com" not in value["note"] and "1234" not in value["note"]


def test_json_trace_has_correlation_ids_and_redaction(tmp_path):
    logger = JsonTraceLogger(str(tmp_path / "trace.jsonl"), max_bytes=10000)
    with trace_context(task_id="task", tool_call_id="tool", verification_id="verify"):
        logger.emit("done", token="never-log")
    record = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8"))
    assert (record["task_id"], record["tool_call_id"], record["verification_id"]) == ("task", "tool", "verify")
    assert record["token"] == "[REDACTED]"


def test_scoped_permissions_support_once_session_always_and_boundaries(tmp_path):
    store = ScopedPermissionStore(str(tmp_path / "grants.json"))
    store.grant("write", "folder", str(tmp_path / "work"), "once")
    assert store.check("write", "folder", str(tmp_path / "work" / "a.txt")) is True
    assert store.check("write", "folder", str(tmp_path / "work" / "a.txt")) is None
    store.grant("web", "domain", "example.com", "session", "block")
    assert store.check("web", "domain", "api.example.com") is False
    store.grant("send", "account", "Boss@Example.com", "always")
    assert ScopedPermissionStore(str(tmp_path / "grants.json")).check("send", "account", "boss@example.com") is True


def test_metrics_diagnostic_and_safe_mode(tmp_path):
    metrics = RuntimeMetrics(); metrics.increment("retry"); metrics.observe("model.latency", 10); metrics.gauge("gpu", "cuda")
    assert metrics.snapshot()["timings"]["model.latency"]["avg_ms"] == 10
    safe = SafeModeManager(str(tmp_path / "safe.json")); safe.set(True, "test"); assert safe.enabled()
    db = tmp_path / "ok.db"; sqlite3.connect(db).close()
    report = DiagnosticReporter().generate(str(tmp_path / "report.json"), databases=[str(db)])
    assert report["databases"][str(db)] == "ok"


def test_bootstrap_checksum_and_role_selection(tmp_path, monkeypatch):
    source = tmp_path / "asset.bin"; source.write_bytes(b"verified")
    manifest = tmp_path / "manifest.json"; manifest.write_text(json.dumps({"assets":[{"role":"stt","path":"models/stt.bin","url":"unused","sha256":hashlib.sha256(b"verified").hexdigest()}]}), encoding="utf-8")
    monkeypatch.setattr("urllib.request.urlretrieve", lambda _url, target: __import__("shutil").copy2(source, target))
    installed = BootstrapInstaller(str(tmp_path / "install")).install(str(manifest), ["stt"])
    assert len(installed) == 1 and (tmp_path / "install/models/stt.bin").read_bytes() == b"verified"


def test_migration_update_and_rollback(tmp_path):
    db = tmp_path / "app.db"; DatabaseMigrator(str(db)).apply(1, ["CREATE TABLE sample(id INTEGER)"])
    with sqlite3.connect(db) as conn: assert conn.execute("SELECT version FROM schema_migrations").fetchone()[0] == 1
    root = tmp_path / "app"; root.mkdir(); (root / "a.txt").write_text("old", encoding="utf-8")
    archive = tmp_path / "update.zip"
    with zipfile.ZipFile(archive, "w") as zf: zf.writestr("a.txt", "new")
    manager = UpdateManager(str(root)); backup = manager.apply_zip(str(archive), hashlib.sha256(archive.read_bytes()).hexdigest())
    assert (root / "a.txt").read_text(encoding="utf-8") == "new"
    manager.rollback(backup); assert (root / "a.txt").read_text(encoding="utf-8") == "old"


def test_e2e_probe_never_claims_unrun_external_checks():
    result = E2EProbeRunner().run(oauth_configured=False)
    assert result["all_executed"] is False
    assert {p["name"]: p["status"] for p in result["probes"]}["network"] == "not_run"


def test_e2e_probe_can_verify_live_network_when_explicit(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self, _limit): return b'{"current":{}}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    result = E2EProbeRunner().run(oauth_configured=True, live_network=True)
    statuses = {p["name"]: p["status"] for p in result["probes"]}
    assert statuses["network"] == "passed"
    assert statuses["oauth"] == "ready"
    assert result["all_executed"] is False  # configured is not the same as a real OAuth round trip


def test_update_rejects_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf: zf.writestr("../escape.txt", "bad")
    with pytest.raises(ValueError): UpdateManager(str(tmp_path / "root")).apply_zip(str(archive), hashlib.sha256(archive.read_bytes()).hexdigest())
