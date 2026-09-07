"""Synthetic filesystem contracts; never operates on user artifacts or apps."""
from hashlib import sha256
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

import core.artifact_transaction as transaction
from core.artifact_transaction import ArtifactTransactionError, commit_artifact_bundle


@pytest.fixture
def bundle(tmp_path):
    source = tmp_path / "staging"
    output = tmp_path / "outputs"
    source.mkdir()
    output.mkdir()
    stages, targets, old, new = [], [], {}, {}
    for name, payload in (("preview.png", b"synthetic-PNG-payload"),
                          ("preview.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
                          ("preview.json", b'{"revision": 2}')):
        stage, target = source / name, output / name
        stage.write_bytes(payload)
        target.write_bytes(b"original-" + name.encode())
        stages.append(stage)
        targets.append(target)
        old[target], new[target] = target.read_bytes(), payload
    sentinel = output / "unrelated-user-file.txt"
    sentinel.write_text("never touch", encoding="utf-8")
    return SimpleNamespace(root=tmp_path, source=source, output=output,
                           stages=stages, targets=targets, old=old, new=new,
                           mapping=dict(zip(stages, targets)), sentinel=sentinel)


def _assert_originals(bundle, *, except_targets=()):
    for path, payload in bundle.old.items():
        if path not in except_targets:
            assert path.read_bytes() == payload
    assert bundle.sentinel.read_text(encoding="utf-8") == "never touch"
    assert bundle.source.is_dir() and bundle.output.is_dir()


def _assert_stages(bundle):
    for stage, destination in bundle.mapping.items():
        assert stage.read_bytes() == bundle.new[destination]


def _temps(bundle):
    return list(bundle.root.rglob(".anis-artifact-*.tmp"))


def _fail_nth_publish(monkeypatch, bundle, *, number=2, effect=None, after=False):
    original = transaction.os.replace
    calls = []

    def replace(source, destination):
        source, destination = Path(source), Path(destination)
        if source.name.startswith(".anis-artifact-stage-"):
            calls.append(destination)
            assert source.parent == destination.parent  # Per-file atomic replace.
            if len(calls) == number:
                if after:
                    original(source, destination)
                if effect:
                    effect(source, destination)
                raise OSError("injected publication failure")
        return original(source, destination)

    monkeypatch.setattr(transaction.os, "replace", replace)
    return calls


def test_success_verifies_every_destination_and_preserves_stages(bundle, monkeypatch):
    real_replace = transaction.os.replace
    calls = []

    def replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert source.parent == destination.parent
        # All backups must already exist before publishing the first file.
        if not calls:
            backups = list(bundle.output.glob(".anis-artifact-backup-*.tmp"))
            assert sorted(path.read_bytes() for path in backups) == sorted(bundle.old.values())
        calls.append(destination)
        real_replace(source, destination)

    monkeypatch.setattr(transaction.os, "replace", replace)
    evidence = commit_artifact_bundle(bundle.mapping)
    assert calls == bundle.targets
    assert set(evidence) == set(bundle.targets)
    for destination, record in evidence.items():
        assert destination.read_bytes() == bundle.new[destination]
        assert record == {"sha256": sha256(bundle.new[destination]).hexdigest(),
                          "size": len(bundle.new[destination]), "verified": True,
                          "previous_sha256": sha256(bundle.old[destination]).hexdigest(),
                          "atomicity": "per_file"}
    _assert_stages(bundle)
    assert not _temps(bundle)
    assert bundle.sentinel.read_text(encoding="utf-8") == "never touch"


@pytest.mark.parametrize("number,after", [(1, False), (2, False), (3, False), (1, True), (2, True)])
def test_replacement_failure_restores_entire_previous_bundle(bundle, monkeypatch, number, after):
    _fail_nth_publish(monkeypatch, bundle, number=number, after=after)
    with pytest.raises(ArtifactTransactionError, match="publication failure") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert caught.value.rolled_back and not caught.value.rollback_errors
    assert not caught.value.recovery_files
    _assert_originals(bundle)
    _assert_stages(bundle)
    assert not _temps(bundle)


def test_new_destination_is_removed_on_rollback_but_parent_and_other_files_remain(bundle, monkeypatch):
    bundle.targets[0].unlink()
    _fail_nth_publish(monkeypatch, bundle)
    with pytest.raises(ArtifactTransactionError) as caught:
        commit_artifact_bundle(bundle.mapping)
    assert caught.value.rolled_back
    assert not bundle.targets[0].exists()
    _assert_originals(bundle, except_targets=[bundle.targets[0]])
    _assert_stages(bundle)
    assert not _temps(bundle)


def test_new_bundle_reports_no_previous_hash(bundle):
    for target in bundle.targets:
        target.unlink()
    evidence = commit_artifact_bundle(bundle.mapping)
    assert all(row["previous_sha256"] is None and row["verified"] for row in evidence.values())
    _assert_stages(bundle)
    assert not _temps(bundle)


@pytest.mark.parametrize("fault", ["missing_stage", "directory_stage", "directory_target", "missing_parent"])
def test_all_input_validation_precedes_writes(bundle, monkeypatch, fault):
    stages, targets = list(bundle.stages), list(bundle.targets)
    if fault == "missing_stage":
        stages[-1] = bundle.source / "missing.png"
    elif fault == "directory_stage":
        stages[-1] = bundle.source
    elif fault == "directory_target":
        targets[-1] = bundle.output
    else:
        targets[-1] = bundle.output / "not-created" / "output.json"
    writes = []
    monkeypatch.setattr(transaction.tempfile, "mkstemp", lambda **kwargs: writes.append(kwargs))
    monkeypatch.setattr(transaction.os, "replace", lambda *_args: writes.append("replace"))
    with pytest.raises(ArtifactTransactionError):
        commit_artifact_bundle(dict(zip(stages, targets)))
    assert writes == []
    assert not (bundle.output / "not-created").exists()
    _assert_originals(bundle)
    _assert_stages(bundle)


@pytest.mark.parametrize("fault", ["same_path", "cross_stage", "duplicate_target", "relative_alias"])
def test_path_aliases_are_rejected_before_any_publication(bundle, monkeypatch, fault):
    mapping = dict(bundle.mapping)
    if fault == "same_path":
        mapping[bundle.stages[0]] = bundle.stages[0]
    elif fault == "cross_stage":
        mapping[bundle.stages[0]] = bundle.stages[1]
    elif fault == "duplicate_target":
        mapping[bundle.stages[1]] = bundle.targets[0]
    else:
        monkeypatch.chdir(bundle.root)
        mapping[bundle.stages[0]] = Path("staging") / bundle.stages[0].name
    with pytest.raises(ArtifactTransactionError, match="samefile|duplicate"):
        commit_artifact_bundle(mapping)
    _assert_originals(bundle)
    _assert_stages(bundle)
    assert not _temps(bundle)


@pytest.mark.parametrize("alias_kind", ["stage_target", "target_target", "stage_stage"])
def test_hardlink_samefile_aliases_are_rejected(bundle, alias_kind):
    if alias_kind == "stage_target":
        source, alias = bundle.stages[0], bundle.targets[0]
    elif alias_kind == "target_target":
        source, alias = bundle.targets[0], bundle.targets[1]
    else:
        source, alias = bundle.stages[0], bundle.stages[1]
    alias.unlink()
    os.link(source, alias)
    before = {path: path.read_bytes() for path in [*bundle.stages, *bundle.targets]}
    with pytest.raises(ArtifactTransactionError, match="samefile"):
        commit_artifact_bundle(bundle.mapping)
    assert {path: path.read_bytes() for path in before} == before
    assert not _temps(bundle)


def _symlink(source, alias, *, is_directory=False):
    try:
        alias.symlink_to(source, target_is_directory=is_directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create symbolic links; synthetic reparse test still runs")
        raise


@pytest.mark.parametrize("kind", ["stage", "target", "broken_target", "parent"])
def test_symlinks_never_follow_or_modify_their_referents(bundle, kind):
    mapping = dict(bundle.mapping)
    outside = bundle.root / "untouched.txt"
    outside.write_bytes(b"outside-original")
    if kind == "parent":
        alias = bundle.root / "linked-output"
        _symlink(bundle.output, alias, is_directory=True)
        mapping[bundle.stages[0]] = alias / bundle.targets[0].name
    else:
        alias = bundle.root / "link.png"
        _symlink(bundle.root / "does-not-exist" if kind == "broken_target" else outside, alias)
        if kind == "stage":
            mapping = {alias: bundle.targets[0], bundle.stages[1]: bundle.targets[1]}
        else:
            mapping[bundle.stages[0]] = alias
    with pytest.raises(ArtifactTransactionError, match="non-link|real directory"):
        commit_artifact_bundle(mapping)
    assert outside.read_bytes() == b"outside-original"
    _assert_originals(bundle)
    _assert_stages(bundle)
    assert not _temps(bundle)


@pytest.mark.parametrize("kind", ["stage", "target", "parent"])
def test_windows_reparse_attribute_is_rejected_without_following(bundle, monkeypatch, kind):
    suspicious = {"stage": bundle.stages[0], "target": bundle.targets[0], "parent": bundle.output}[kind]
    real_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if path == suspicious:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ArtifactTransactionError, match="non-link|real directory"):
        commit_artifact_bundle(bundle.mapping)
    _assert_originals(bundle)
    _assert_stages(bundle)
    assert not _temps(bundle)


def test_unreadable_later_source_stops_before_any_temporary_files(bundle, monkeypatch):
    real_seal = transaction._seal

    def seal(path, **kwargs):
        if path == bundle.stages[-1]:
            raise PermissionError("cannot read last source")
        return real_seal(path, **kwargs)

    monkeypatch.setattr(transaction, "_seal", seal)
    with pytest.raises(ArtifactTransactionError, match="cannot read last source"):
        commit_artifact_bundle(bundle.mapping)
    _assert_originals(bundle)
    assert not _temps(bundle)


@pytest.mark.parametrize("kind", ["stage", "target", "prepared"])
def test_changes_after_preflight_abort_before_first_replace(bundle, monkeypatch, kind):
    real_copy = transaction._private_copy
    copies, replacements = [], []

    def copy(source, *args):
        private = real_copy(source, *args)
        copies.append(private)
        if len(copies) == 6:
            victim = {"stage": bundle.stages[0], "target": bundle.targets[0],
                      "prepared": copies[0].path}[kind]
            victim.write_bytes(b"external-new-content")
        return private

    monkeypatch.setattr(transaction, "_private_copy", copy)
    monkeypatch.setattr(transaction.os, "replace", lambda *args: replacements.append(args))
    with pytest.raises(ArtifactTransactionError, match="changed since verification") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert replacements == []
    _assert_originals(bundle, except_targets=[bundle.targets[0]] if kind == "target" else [])
    if kind == "target":
        assert bundle.targets[0].read_bytes() == b"external-new-content"
    elif kind == "stage":
        assert bundle.stages[0].read_bytes() == b"external-new-content"
    else:
        assert copies[0].path.read_bytes() == b"external-new-content"
        assert caught.value.cleanup_errors


@pytest.mark.parametrize("kind", ["inplace", "replacement", "directory", "missing"])
def test_rollback_preserves_external_destination_and_retains_original_backup(bundle, monkeypatch, kind):
    first = bundle.targets[0]

    def external_change(_source, _destination):
        if kind == "inplace":
            first.write_bytes(b"external-user-revision")
        else:
            first.unlink()
            if kind == "replacement":
                # Even identical bytes in a new file are not transaction-owned.
                first.write_bytes(bundle.new[first])
            elif kind == "directory":
                first.mkdir()
                (first / "keep.txt").write_bytes(b"external-user-revision")

    _fail_nth_publish(monkeypatch, bundle, effect=external_change)
    with pytest.raises(ArtifactTransactionError, match="Rollback incomplete") as caught:
        commit_artifact_bundle(bundle.mapping)
    error = caught.value
    assert not error.rolled_back
    assert first in error.recovery_files
    assert error.recovery_files[first].parent == first.parent
    assert error.recovery_files[first].read_bytes() == bundle.old[first]
    if kind == "inplace":
        assert first.read_bytes() == b"external-user-revision"
    elif kind == "replacement":
        assert first.read_bytes() == bundle.new[first]
    elif kind == "directory":
        assert (first / "keep.txt").read_bytes() == b"external-user-revision"
    else:
        assert not first.exists()
    _assert_originals(bundle, except_targets=[first])
    _assert_stages(bundle)


def test_external_writer_creating_future_target_is_preserved(bundle, monkeypatch):
    second = bundle.targets[1]
    second.unlink()
    real_replace = transaction.os.replace

    def replace(source, destination):
        real_replace(source, destination)
        if Path(source).name.startswith(".anis-artifact-stage-") and Path(destination) == bundle.targets[0]:
            second.write_bytes(b"created-by-another-program")

    monkeypatch.setattr(transaction.os, "replace", replace)
    with pytest.raises(ArtifactTransactionError, match="changed since verification") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert caught.value.rolled_back
    assert second.read_bytes() == b"created-by-another-program"
    _assert_originals(bundle, except_targets=[second])
    _assert_stages(bundle)
    assert not _temps(bundle)


def test_corrupt_original_backup_is_never_restored_over_destination(bundle, monkeypatch):
    corrupted = []

    def corrupt(_source, _destination):
        for backup in bundle.output.glob(".anis-artifact-backup-*.tmp"):
            if backup.read_bytes() == bundle.old[bundle.targets[0]]:
                backup.write_bytes(b"invalid-backup")
                corrupted.append(backup)

    _fail_nth_publish(monkeypatch, bundle, effect=corrupt)
    with pytest.raises(ArtifactTransactionError, match="Rollback incomplete") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert corrupted and caught.value.recovery_files[bundle.targets[0]] == corrupted[0]
    assert bundle.targets[0].read_bytes() == bundle.new[bundle.targets[0]]
    assert corrupted[0].read_bytes() == b"invalid-backup"
    _assert_originals(bundle, except_targets=[bundle.targets[0]])


def test_failed_rollback_retains_valid_original_backup(bundle, monkeypatch):
    real_replace = transaction.os.replace
    calls = 0

    def replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second output failed")
        if Path(source).name.startswith(".anis-artifact-backup-"):
            raise PermissionError("destination temporarily locked")
        return real_replace(source, destination)

    monkeypatch.setattr(transaction.os, "replace", replace)
    with pytest.raises(ArtifactTransactionError, match="destination temporarily locked") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert not caught.value.rolled_back
    assert caught.value.recovery_files[bundle.targets[0]].read_bytes() == bundle.old[bundle.targets[0]]
    _assert_originals(bundle, except_targets=[bundle.targets[0]])


def test_rollback_replace_then_raise_is_confirmed_by_readback(bundle, monkeypatch):
    real_replace = transaction.os.replace
    calls = 0

    def replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second output failed")
        real_replace(source, destination)
        if Path(source).name.startswith(".anis-artifact-backup-"):
            raise OSError("rollback syscall outcome uncertain")

    monkeypatch.setattr(transaction.os, "replace", replace)
    with pytest.raises(ArtifactTransactionError) as caught:
        commit_artifact_bundle(bundle.mapping)
    assert caught.value.rolled_back
    _assert_originals(bundle)
    _assert_stages(bundle)
    assert not _temps(bundle)


@pytest.mark.parametrize("original_exists", [True, False])
def test_readonly_published_payload_can_be_rolled_back(bundle, monkeypatch, original_exists):
    if not original_exists:
        bundle.targets[0].unlink()
    mode = bundle.stages[0].stat().st_mode
    bundle.stages[0].chmod(stat.S_IREAD)
    _fail_nth_publish(monkeypatch, bundle)
    try:
        with pytest.raises(ArtifactTransactionError) as caught:
            commit_artifact_bundle(bundle.mapping)
        assert caught.value.rolled_back
        assert not bundle.stages[0].stat().st_mode & stat.S_IWRITE
        if original_exists:
            _assert_originals(bundle)
        else:
            assert not bundle.targets[0].exists()
            _assert_originals(bundle, except_targets=[bundle.targets[0]])
        _assert_stages(bundle)
        assert not _temps(bundle)
    finally:
        bundle.stages[0].chmod(mode)


def test_stage_mutation_during_hash_is_detected_before_writes(bundle, monkeypatch):
    real_digest = transaction.hashlib.sha256
    changed = False

    class Digest:
        def __init__(self):
            self.inner = real_digest()

        def update(self, block):
            nonlocal changed
            self.inner.update(block)
            if not changed:
                changed = True
                bundle.stages[0].write_bytes(b"changed-during-read")

        def hexdigest(self):
            return self.inner.hexdigest()

    monkeypatch.setattr(transaction.hashlib, "sha256", Digest)
    with pytest.raises(ArtifactTransactionError, match="changed while hashing"):
        commit_artifact_bundle(bundle.mapping)
    assert bundle.stages[0].read_bytes() == b"changed-during-read"
    _assert_originals(bundle)
    assert not _temps(bundle)


def test_noop_replace_cannot_report_verified_publication(bundle, monkeypatch):
    monkeypatch.setattr(transaction.os, "replace", lambda *_args: None)
    with pytest.raises(ArtifactTransactionError, match="hash/identity mismatch") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert caught.value.rolled_back
    _assert_originals(bundle)
    _assert_stages(bundle)
    assert not _temps(bundle)


def test_early_destination_change_is_caught_by_final_bundle_verification(bundle, monkeypatch):
    real_replace = transaction.os.replace

    def replace(source, destination):
        real_replace(source, destination)
        if Path(source).name.startswith(".anis-artifact-stage-") and Path(destination) == bundle.targets[-1]:
            bundle.targets[0].write_bytes(b"external-change-after-first-publish")

    monkeypatch.setattr(transaction.os, "replace", replace)
    with pytest.raises(ArtifactTransactionError, match="Rollback incomplete") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert bundle.targets[0].read_bytes() == b"external-change-after-first-publish"
    assert caught.value.recovery_files[bundle.targets[0]].read_bytes() == bundle.old[bundle.targets[0]]
    _assert_originals(bundle, except_targets=[bundle.targets[0]])


def test_failure_preparing_later_artifact_does_not_publish_anything(bundle, monkeypatch):
    real_fsync = transaction.os.fsync
    calls = 0

    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("staging disk full")
        return real_fsync(fd)

    monkeypatch.setattr(transaction.os, "fsync", fsync)
    with pytest.raises(ArtifactTransactionError, match="staging disk full") as caught:
        commit_artifact_bundle(bundle.mapping)
    assert caught.value.rolled_back
    _assert_originals(bundle)
    _assert_stages(bundle)
    assert not _temps(bundle)


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
@pytest.mark.parametrize("name", ["NUL", "relative:stream", "trailing.", "space "])
def test_windows_ambiguous_paths_are_rejected(name):
    with pytest.raises(ArtifactTransactionError, match="Invalid artifact path"):
        transaction._path(name)


def test_parent_replacement_never_writes_or_deletes_in_new_directory(bundle, monkeypatch):
    real_copy = transaction._private_copy
    displaced = bundle.root / "displaced-original-directory"
    replaced = False

    def copy(source, *args):
        nonlocal replaced
        private = real_copy(source, *args)
        if not replaced:
            replaced = True
            bundle.output.rename(displaced)
            bundle.output.mkdir()
            (bundle.output / "external.txt").write_bytes(b"new-owner")
        return private

    monkeypatch.setattr(transaction, "_private_copy", copy)
    with pytest.raises(ArtifactTransactionError, match="parent changed|missing"):
        commit_artifact_bundle(bundle.mapping)
    assert list(bundle.output.iterdir()) == [bundle.output / "external.txt"]
    assert (bundle.output / "external.txt").read_bytes() == b"new-owner"
    for path, payload in bundle.old.items():
        assert (displaced / path.name).read_bytes() == payload
    _assert_stages(bundle)


@pytest.mark.parametrize("mapping", [{}, None, [], {"": "dest"}, {"../stage.png": "dest"}])
def test_invalid_mapping_fails_without_touching_files(mapping):
    with pytest.raises(ArtifactTransactionError):
        commit_artifact_bundle(mapping)
