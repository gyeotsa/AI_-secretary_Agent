"""Checked publication of a small, already-rendered artifact bundle.

``os.replace`` is atomic for each file, NOT for the whole bundle. Readers can
observe an intermediate revision, and portable Python has no compare-and-swap
rename against uncooperative external writers. This module seals inputs before
publishing, rechecks at each boundary, and rolls back only files still owned by
this transaction. It is not a crash/power-loss journal or a filesystem sandbox.

Stages are preserved. Destination directories must already exist; symlinks,
Windows reparse points, aliases, and missing/replaced parents fail closed. On a
rollback conflict the external writer's file is left alone and an original
backup is retained for explicit recovery. No recursive cleanup is performed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from typing import Any


_BUNDLE_LOCK = threading.RLock()
_CHUNK_SIZE = 1024 * 1024


class ArtifactTransactionError(RuntimeError):
    """Publication failed; inspect rollback/recovery fields before retrying."""

    def __init__(self, message: str, *, rollback_errors=(), recovery_files=None,
                 published_paths=(), cleanup_errors=()):
        self.rollback_errors = tuple(rollback_errors)
        self.recovery_files = dict(recovery_files or {})
        self.published_paths = tuple(published_paths)
        self.cleanup_errors = tuple(cleanup_errors)
        self.rolled_back = not self.rollback_errors
        details = [message]
        if self.rollback_errors:
            details.append("Rollback incomplete: " + "; ".join(self.rollback_errors))
        if self.recovery_files:
            details.append("Recovery backup paths (validate before use): " + "; ".join(
                f"{destination} -> {backup}" for destination, backup in self.recovery_files.items()))
        if self.cleanup_errors:
            details.append("Temporary cleanup incomplete: " + "; ".join(self.cleanup_errors))
        super().__init__(". ".join(details))


@dataclass(frozen=True)
class _Seal:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str

    @property
    def identity(self):
        return self.device, self.inode

    def matches_moved_file(self, other: _Seal) -> bool:
        # A rename may update ctime, but cannot change the inode or payload.
        return (self.device, self.inode, self.mode, self.size, self.mtime_ns, self.sha256) == (
            other.device, other.inode, other.mode, other.size, other.mtime_ns, other.sha256)


@dataclass
class _PrivateFile:
    path: Path
    seal: _Seal


@dataclass
class _Entry:
    stage: Path
    destination: Path
    source: _Seal
    original: _Seal | None
    prepared: _PrivateFile | None = None
    backup: _PrivateFile | None = None
    attempted: bool = False
    published: _Seal | None = None


def _path(value) -> Path:
    try:
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise ValueError("a non-empty filesystem path is required")
        candidate = Path(raw)
        if ".." in candidate.parts or (candidate.drive and not candidate.is_absolute()):
            raise ValueError("parent traversal and drive-relative paths are not supported")
        if os.name == "nt":
            parts = candidate.parts[1:] if candidate.anchor else candidate.parts
            if any(":" in part for part in parts) or candidate.is_reserved():
                raise ValueError("device names and alternate data streams are not supported")
            if any(part.endswith((" ", ".")) for part in parts):
                raise ValueError("ambiguous Windows trailing-dot/space paths are not supported")
        return Path(os.path.abspath(candidate))  # Do not resolve/follow a link.
    except (TypeError, ValueError, OSError) as exc:
        raise ArtifactTransactionError(f"Invalid artifact path: {exc}") from exc


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _stat_version(info):
    # On Windows, path-stat and fd-stat can expose different ctime values just
    # after copystat. Do not interpret that metadata-cache difference as an
    # external writer. File identity, mode, size, mtime and SHA-256 stay sealed.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, 0 if os.name == "nt" else info.st_ctime_ns)


def _seal(path: Path, *, allow_missing=False) -> _Seal | None:
    """Hash a regular file and reject replacement/mutation during the read."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ArtifactTransactionError(f"Required artifact is missing: {path}") from None
    if _is_link(before) or not stat.S_ISREG(before.st_mode):
        raise ArtifactTransactionError(f"Artifact must be a regular, non-link file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _stat_version(before) != _stat_version(opened):
            raise ArtifactTransactionError(f"Artifact changed while opening: {path}")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(block)
        after_read = os.fstat(stream.fileno())
    after = path.lstat()
    if _is_link(after) or not (_stat_version(before) == _stat_version(after_read) == _stat_version(after)):
        raise ArtifactTransactionError(f"Artifact changed while hashing: {path}")
    return _Seal(*_stat_version(after), digest.hexdigest())


def _parents(paths) -> dict[Path, tuple[int, int]]:
    result = {}
    for path in paths:
        for parent in reversed(path.parents):
            if parent in result:
                continue
            info = parent.lstat()
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise ArtifactTransactionError(f"Artifact parent must be a real directory: {parent}")
            result[parent] = (info.st_dev, info.st_ino)
    return result


def _check_parents(parents, *, for_path: Path | None = None):
    for path, identity in parents.items():
        if for_path is not None and path not in for_path.parents:
            continue
        info = path.lstat()
        if (_is_link(info) or not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != identity):
            raise ArtifactTransactionError(f"Artifact parent changed: {path}")


def _expect(path: Path, expected: _Seal | None):
    actual = _seal(path, allow_missing=True)
    if actual != expected:
        raise ArtifactTransactionError(f"Artifact changed since verification: {path}")
    return actual


def _preflight(staged: dict[Path, Path]):
    if not isinstance(staged, dict) or not staged:
        raise ArtifactTransactionError("At least one stage -> destination entry is required")
    pairs = [(_path(stage), _path(destination)) for stage, destination in staged.items()]
    all_paths = [path for pair in pairs for path in pair]
    if len(set(all_paths)) != len(all_paths):
        raise ArtifactTransactionError("Stage/destination paths must be distinct (samefile/duplicate target)")
    parents = _parents(all_paths)
    entries, identities = [], {}
    # Finish every source/target read and alias check before any temp or replace.
    for stage, destination in pairs:
        source, original = _seal(stage), _seal(destination, allow_missing=True)
        for path, seal in ((stage, source), (destination, original)):
            if seal is not None:
                if seal.identity in identities:
                    raise ArtifactTransactionError(f"Artifact paths alias the samefile: {identities[seal.identity]}, {path}")
                identities[seal.identity] = path
        entries.append(_Entry(stage, destination, source, original))
    _check_parents(parents)
    return entries, parents


def _private_copy(source: Path, expected: _Seal, destination: Path, kind: str, parents) -> _PrivateFile:
    """Prepare on the destination filesystem without moving the source."""
    _check_parents(parents)
    _expect(source, expected)
    fd, name = tempfile.mkstemp(prefix=f".anis-artifact-{kind}-", suffix=".tmp", dir=destination.parent)
    path = Path(name)
    created_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as origin:
            if _stat_version(os.fstat(origin.fileno())) != (
                    expected.device, expected.inode, expected.mode, expected.size,
                    expected.mtime_ns, expected.ctime_ns):
                raise ArtifactTransactionError(f"Artifact changed before copying: {source}")
            shutil.copyfileobj(origin, target, _CHUNK_SIZE)
            target.flush()
            os.fsync(target.fileno())
        _check_parents(parents)
        _expect(source, expected)
        shutil.copystat(source, path, follow_symlinks=False)
        result = _seal(path)
        if result.sha256 != expected.sha256 or result.size != expected.size:
            raise ArtifactTransactionError(f"Private artifact copy failed hash verification: {path}")
        return _PrivateFile(path, result)
    except BaseException:
        # Only a file allocated by this function is eligible for cleanup.
        try:
            _check_parents(parents, for_path=path)
            info = path.lstat()
            if not _is_link(info) and (info.st_dev, info.st_ino) == created_identity:
                if os.name == "nt" and not info.st_mode & stat.S_IWRITE:
                    os.chmod(path, info.st_mode | stat.S_IWRITE)
                path.unlink()
        except (OSError, ArtifactTransactionError):
            pass
        raise


def _cleanup(private: _PrivateFile, parents) -> str | None:
    try:
        _check_parents(parents, for_path=private.path)
        current = _seal(private.path, allow_missing=True)
        if current is None:
            return None  # Already consumed by replace/rollback.
        if current != private.seal:
            return f"Changed temporary file preserved: {private.path}"
        # Read-only original metadata must not make our private backup immortal
        # on Windows. This is never applied to a destination or caller's stage.
        if os.name == "nt" and not current.mode & stat.S_IWRITE:
            os.chmod(private.path, current.mode | stat.S_IWRITE)
        private.path.unlink()
        return None
    except (OSError, ArtifactTransactionError) as exc:
        return f"{private.path}: {exc}"


def _rollback(entries, parents):
    errors, recovery, changed = [], {}, []
    for entry in reversed(entries):
        if not entry.attempted:
            continue
        try:
            _check_parents(parents, for_path=entry.destination)
            current = _seal(entry.destination, allow_missing=True)
            if current == entry.original:
                continue  # Replace failed before taking effect.
            changed.append(entry.destination)
            owned = (current == entry.published if entry.published is not None else
                     current is not None and entry.prepared.seal.matches_moved_file(current))
            if not owned:
                raise ArtifactTransactionError("Destination was changed externally; preserving current state")
            if os.name == "nt" and not current.mode & stat.S_IWRITE:
                # This is a just-published, identity/hash-checked transaction
                # output, never the old user file or an external replacement.
                os.chmod(entry.destination, current.mode | stat.S_IWRITE)
            if entry.original is None:
                entry.destination.unlink()
                if _seal(entry.destination, allow_missing=True) is not None:
                    raise ArtifactTransactionError("New destination reappeared during rollback")
            else:
                _expect(entry.backup.path, entry.backup.seal)
                if entry.backup.seal.sha256 != entry.original.sha256:
                    raise ArtifactTransactionError("Original backup hash does not match")
                try:
                    os.replace(entry.backup.path, entry.destination)
                except BaseException:
                    # A wrapper/OS boundary may raise after the rename took
                    # effect. Read-back, not the exception alone, decides.
                    restored = _seal(entry.destination, allow_missing=True)
                    if restored is None or not entry.backup.seal.matches_moved_file(restored):
                        raise
                restored = _seal(entry.destination)
                if not entry.backup.seal.matches_moved_file(restored):
                    raise ArtifactTransactionError("Restored destination failed verification")
        except BaseException as exc:
            errors.append(f"{entry.destination}: {type(exc).__name__}: {exc}")
            if entry.backup is not None:
                recovery[entry.destination] = entry.backup.path
    return errors, recovery, changed


def commit_artifact_bundle(staged: dict[Path, Path]) -> dict[Path, dict[str, Any]]:
    """Publish stage -> destination files, preserving stages and rollback data.

    Success returns absolute destination keys with SHA-256, size, previous hash,
    and verification evidence. Cleanup warnings, if any, are explicit. All
    preflight validation precedes writes. Existing destination bytes/basic stat
    are backed up in that directory before the first replacement (ACL cloning
    is not promised). A failure raises ``ArtifactTransactionError``; callers
    must not report completion or retry blindly when rollback is incomplete.
    """
    with _BUNDLE_LOCK:
        entries, parents = [], {}
        try:
            entries, parents = _preflight(staged)
            for entry in entries:
                _check_parents(parents)
                entry.prepared = _private_copy(entry.stage, entry.source, entry.destination, "stage", parents)
                if entry.original is not None:
                    entry.backup = _private_copy(entry.destination, entry.original, entry.destination, "backup", parents)
            # Seal the entire batch again before the first visible change.
            _check_parents(parents)
            for entry in entries:
                _expect(entry.stage, entry.source)
                _expect(entry.destination, entry.original)
                _expect(entry.prepared.path, entry.prepared.seal)
                if entry.backup is not None:
                    _expect(entry.backup.path, entry.backup.seal)
            for entry in entries:
                _check_parents(parents)
                _expect(entry.stage, entry.source)
                _expect(entry.destination, entry.original)
                _expect(entry.prepared.path, entry.prepared.seal)
                entry.attempted = True  # Includes a replace-then-raise outcome.
                os.replace(entry.prepared.path, entry.destination)
                current = _seal(entry.destination)
                if not entry.prepared.seal.matches_moved_file(current):
                    raise ArtifactTransactionError(f"Published artifact hash/identity mismatch: {entry.destination}")
                entry.published = current
            _check_parents(parents)
            for entry in entries:
                _expect(entry.stage, entry.source)
                _expect(entry.destination, entry.published)
                if entry.backup is not None:
                    _expect(entry.backup.path, entry.backup.seal)
        except BaseException as exc:
            rollback_errors, recovery, changed = _rollback(entries, parents)
            cleanup_errors = []
            retained = set(recovery.values())
            for entry in entries:
                for private in (entry.prepared, entry.backup):
                    if private is not None and private.path not in retained:
                        problem = _cleanup(private, parents)
                        if problem:
                            cleanup_errors.append(problem)
            raise ArtifactTransactionError(
                f"Artifact bundle publication failed: {type(exc).__name__}: {exc}",
                rollback_errors=rollback_errors, recovery_files=recovery,
                published_paths=changed, cleanup_errors=cleanup_errors) from exc

        cleanup_errors = []
        for entry in entries:
            for private in (entry.prepared, entry.backup):
                if private is not None:
                    problem = _cleanup(private, parents)
                    if problem:
                        cleanup_errors.append(problem)
        return {entry.destination: {
            "sha256": entry.published.sha256, "size": entry.published.size,
            "previous_sha256": entry.original.sha256 if entry.original else None,
            "verified": True, "atomicity": "per_file",
            **({"cleanup_warnings": list(cleanup_errors)} if cleanup_errors else {}),
        } for entry in entries}
