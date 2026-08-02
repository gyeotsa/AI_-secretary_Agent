"""P12 security, observability, diagnostics, bootstrap, and update primitives."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import contextvars
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile


class SensitiveDataRedactor:
    SENSITIVE_KEYS = re.compile(r"password|passwd|token|secret|api[_-]?key|credential|authorization|cookie", re.I)
    PATTERNS = (
        (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
        (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
        (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
        (re.compile(r"(?<!\d)(?:01[016789])[- ]?\d{3,4}[- ]?\d{4}(?!\d)"), "[REDACTED_PHONE]"),
    )

    @classmethod
    def redact(cls, value: Any, key: str = "") -> Any:
        if cls.SENSITIVE_KEYS.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): cls.redact(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls.redact(v, key) for v in value]
        if isinstance(value, str):
            for pattern, replacement in cls.PATTERNS:
                value = pattern.sub(replacement, value)
        return value


_trace_context = contextvars.ContextVar("jarvis_trace_context", default={})


@contextmanager
def trace_context(task_id: Optional[str] = None, tool_call_id: Optional[str] = None,
                  verification_id: Optional[str] = None):
    current = dict(_trace_context.get())
    current.update({k: v for k, v in {
        "task_id": task_id, "tool_call_id": tool_call_id,
        "verification_id": verification_id,
    }.items() if v})
    token = _trace_context.set(current)
    try:
        yield current
    finally:
        _trace_context.reset(token)


class JsonTraceLogger:
    def __init__(self, path: str = "data/logs/trace.jsonl", max_bytes: int = 5_000_000,
                 backup_count: int = 5):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"jarvis.trace.{self.path.resolve()}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            self._logger.addHandler(RotatingFileHandler(
                self.path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            ))

    def emit(self, event: str, **fields: Any) -> Dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **_trace_context.get(),
            **fields,
        }
        record = SensitiveDataRedactor.redact(record)
        self._logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        return record


class RuntimeMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._timings: Dict[str, list[float]] = {}
        self._gauges: Dict[str, Any] = {}

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def observe(self, name: str, milliseconds: float) -> None:
        with self._lock:
            values = self._timings.setdefault(name, [])
            values.append(float(milliseconds))
            del values[:-1000]

    def gauge(self, name: str, value: Any) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            timings = {name: {
                "count": len(values), "avg_ms": round(sum(values) / len(values), 2),
                "max_ms": round(max(values), 2),
            } for name, values in self._timings.items() if values}
            return {"counters": dict(self._counters), "timings": timings, "gauges": dict(self._gauges)}


TRACE = JsonTraceLogger()
METRICS = RuntimeMetrics()


class WindowsCredentialVault:
    """Windows Credential Manager adapter. Secrets are never persisted as plaintext."""
    def __init__(self, prefix: str = "Jarvis"):
        self.prefix = prefix.rstrip("/")

    def _target(self, name: str) -> str:
        if not name or any(ch in name for ch in "\\\r\n"):
            raise ValueError("Invalid credential name")
        return f"{self.prefix}/{name}"

    @staticmethod
    def _module():
        if os.name != "nt":
            raise RuntimeError("Windows Credential Manager is only available on Windows")
        try:
            import win32cred
            return win32cred
        except ImportError as exc:
            raise RuntimeError("pywin32 is required for secure credential storage") from exc

    def set(self, name: str, secret: str, username: str = "Jarvis") -> None:
        if not secret:
            raise ValueError("Empty secrets are not allowed")
        wc = self._module()
        wc.CredWrite({"Type": wc.CRED_TYPE_GENERIC, "TargetName": self._target(name),
                      "UserName": username, "CredentialBlob": secret,
                      "Persist": wc.CRED_PERSIST_LOCAL_MACHINE}, 0)

    def get(self, name: str) -> Optional[str]:
        wc = self._module()
        try:
            value = wc.CredRead(self._target(name), wc.CRED_TYPE_GENERIC, 0)["CredentialBlob"]
        except Exception as exc:
            if getattr(exc, "winerror", None) == 1168:
                return None
            raise
        return value.decode("utf-16-le") if isinstance(value, bytes) else str(value)

    def delete(self, name: str) -> bool:
        wc = self._module()
        try:
            wc.CredDelete(self._target(name), wc.CRED_TYPE_GENERIC, 0)
            return True
        except Exception as exc:
            if getattr(exc, "winerror", None) == 1168:
                return False
            raise


@dataclass(frozen=True)
class ScopedGrant:
    permission_id: str
    scope_type: str
    scope_value: str
    lifetime: str
    decision: str = "allow"


class ScopedPermissionStore:
    VALID_SCOPES = {"folder", "app", "domain", "account", "global"}
    VALID_LIFETIMES = {"once", "session", "always"}

    def __init__(self, path: str = "data/scoped_permissions.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._always: list[ScopedGrant] = []
        self._session: list[ScopedGrant] = []
        self._once: list[ScopedGrant] = []
        self._load()

    @staticmethod
    def normalize(scope_type: str, value: str) -> str:
        if scope_type == "folder": return str(Path(value).resolve()).casefold()
        if scope_type == "domain": return value.strip().lower().rstrip(".")
        return value.strip().casefold()

    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._always = [ScopedGrant(**item) for item in raw.get("grants", [])]

    def _save(self) -> None:
        self.path.write_text(json.dumps({"version": 1, "grants": [asdict(g) for g in self._always]},
                                        ensure_ascii=False, indent=2), encoding="utf-8")

    def grant(self, permission_id: str, scope_type: str, scope_value: str,
              lifetime: str, decision: str = "allow") -> ScopedGrant:
        if scope_type not in self.VALID_SCOPES or lifetime not in self.VALID_LIFETIMES:
            raise ValueError("Unsupported permission scope or lifetime")
        grant = ScopedGrant(permission_id, scope_type, self.normalize(scope_type, scope_value), lifetime, decision)
        target = {"once": self._once, "session": self._session, "always": self._always}[lifetime]
        target[:] = [g for g in target if (g.permission_id, g.scope_type, g.scope_value) !=
                    (grant.permission_id, grant.scope_type, grant.scope_value)]
        target.append(grant)
        if lifetime == "always": self._save()
        return grant

    def check(self, permission_id: str, scope_type: str, scope_value: str) -> Optional[bool]:
        value = self.normalize(scope_type, scope_value)
        for collection in (self._once, self._session, self._always):
            for grant in reversed(collection):
                if grant.permission_id != permission_id or grant.scope_type != scope_type:
                    continue
                matched = (value == grant.scope_value)
                if scope_type == "folder":
                    matched = value == grant.scope_value or value.startswith(grant.scope_value + os.sep.casefold())
                elif scope_type == "domain":
                    matched = value == grant.scope_value or value.endswith("." + grant.scope_value)
                if matched:
                    if collection is self._once: collection.remove(grant)
                    return grant.decision == "allow"
        return None

    def list(self) -> list[Dict[str, str]]:
        return [asdict(g) for group in (self._once, self._session, self._always) for g in group]


class SafeModeManager:
    def __init__(self, path: str = "data/safe_mode.json"):
        self.path = Path(path)

    def enabled(self) -> bool:
        try: return bool(json.loads(self.path.read_text(encoding="utf-8")).get("enabled"))
        except (OSError, ValueError): return False

    def set(self, enabled: bool, reason: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": bool(enabled), "reason": reason,
                                         "updated_at": datetime.now(timezone.utc).isoformat()},
                                        ensure_ascii=False, indent=2), encoding="utf-8")


class DiagnosticReporter:
    def generate(self, output: str, plugin_registry=None, databases: Iterable[str] = ()) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "platform": {"python": platform.python_version(), "os": platform.platform()},
            "safe_mode": SafeModeManager().enabled(), "metrics": METRICS.snapshot(),
            "plugins": [], "databases": {},
        }
        if plugin_registry:
            report["plugins"] = [SensitiveDataRedactor.redact(asdict(s)) for s in plugin_registry.get_plugin_statuses()]
        for database in databases:
            try:
                with sqlite3.connect(database) as conn:
                    report["databases"][database] = conn.execute("PRAGMA quick_check").fetchone()[0]
            except Exception as exc:
                report["databases"][database] = f"error:{type(exc).__name__}"
        target = Path(output); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(SensitiveDataRedactor.redact(report), ensure_ascii=False, indent=2), encoding="utf-8")
        return report


class E2EProbeRunner:
    def run(self, oauth_configured: bool = False) -> Dict[str, Any]:
        probes = []
        probes.append({"name": "windows_device_runtime", "status": "passed" if os.name == "nt" else "skipped",
                       "reason": "Windows runtime" if os.name == "nt" else "Windows only"})
        probes.append({"name": "network", "status": "not_run", "reason": "explicit live acceptance required"})
        probes.append({"name": "oauth", "status": "ready" if oauth_configured else "skipped",
                       "reason": "configured" if oauth_configured else "OAuth credentials absent"})
        return {"probes": probes, "all_executed": all(p["status"] == "passed" for p in probes)}


class BootstrapInstaller:
    def __init__(self, install_root: str): self.install_root = Path(install_root)

    def install(self, manifest_path: str, roles: Iterable[str]) -> list[str]:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        selected = set(roles); installed = []
        for asset in manifest.get("assets", []):
            if asset.get("role") not in selected: continue
            relative = Path(asset["path"])
            if relative.is_absolute() or ".." in relative.parts: raise ValueError("Unsafe asset path")
            target = self.install_root / relative; target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(target.suffix + ".part")
            urllib.request.urlretrieve(asset["url"], temp)
            if sha256(temp.read_bytes()).hexdigest() != asset["sha256"]:
                temp.unlink(missing_ok=True); raise ValueError("Asset checksum mismatch")
            os.replace(temp, target); installed.append(str(target))
        return installed


class DatabaseMigrator:
    def __init__(self, db_path: str): self.db_path = db_path

    def apply(self, version: int, statements: Iterable[str]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone(): return
            for statement in statements: conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, datetime.now(timezone.utc).isoformat()))


class UpdateManager:
    def __init__(self, root: str): self.root = Path(root).resolve()

    def apply_zip(self, archive: str, expected_sha256: str) -> str:
        archive_path = Path(archive)
        if sha256(archive_path.read_bytes()).hexdigest() != expected_sha256: raise ValueError("Update checksum mismatch")
        backup = Path(tempfile.mkdtemp(prefix="jarvis-rollback-"))
        stage = Path(tempfile.mkdtemp(prefix="jarvis-update-"))
        with zipfile.ZipFile(archive_path) as bundle:
            for item in bundle.infolist():
                target = (stage / item.filename).resolve()
                if stage not in target.parents and target != stage: raise ValueError("Unsafe update archive")
            bundle.extractall(stage)
        for source in stage.rglob("*"):
            if not source.is_file(): continue
            relative = source.relative_to(stage); target = self.root / relative
            if target.exists():
                saved = backup / relative; saved.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(target, saved)
            target.parent.mkdir(parents=True, exist_ok=True); os.replace(source, target)
        return str(backup)

    def rollback(self, backup: str) -> None:
        backup_path = Path(backup)
        for source in backup_path.rglob("*"):
            if source.is_file():
                target = self.root / source.relative_to(backup_path)
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)


def new_correlation_ids() -> Dict[str, str]:
    return {"task_id": uuid.uuid4().hex, "tool_call_id": uuid.uuid4().hex,
            "verification_id": uuid.uuid4().hex}
