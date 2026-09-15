"""Offline credential identity tests; never inspect the user's actual vault."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.remote_runtime import RemoteRuntimeError, SecureTokenVault


class FakeDPAPI:
    """Opaque fixture bytes, with plaintext held only in this fake's memory."""

    def __init__(self):
        self.payloads = {}
        self.decrypt_calls = []

    def CryptProtectData(self, data, *_args):
        encrypted = f"fixture-ciphertext-{len(self.payloads)}".encode("ascii")
        self.payloads[encrypted] = data
        return "fixture", encrypted

    def CryptUnprotectData(self, encrypted, *_args):
        self.decrypt_calls.append(encrypted)
        return "fixture", self.payloads[encrypted]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    crypto = FakeDPAPI()
    monkeypatch.setitem(sys.modules, "win32crypt", crypto)
    return SecureTokenVault(str(tmp_path / "isolated-vault")), crypto


def token_path(store, provider, account):
    return store.directory / (store._name(provider, account) + ".dpapi")


@pytest.mark.parametrize("first,second", [
    (("google", "a+b@example.com"), ("google", "a_b@example.com")),
    (("google", "Owner@example.com"), ("google", "owner@example.com")),
    (("google", "x" * 300 + "one"), ("google", "x" * 300 + "two")),
    (("google", "ß"), ("google", "ss")),
    (("google", "계정+하나"), ("google", "계정_하나")),
    (("google", "é"), ("google", "e\u0301")),
    (("service_a", "b"), ("service", "a_b")),
    (("google", "owner"), ("microsoft", "owner")),
])
def test_exact_scope_names_isolate_aliases_suffixes_unicode_and_provider(vault, first, second):
    store, _crypto = vault
    store.save(*first, {"access_token": "first-private-token"})
    store.save(*second, {"access_token": "second-private-token"})
    assert store._name(*first) != store._name(*second)
    assert re.fullmatch(r"v2-[0-9a-f]{64}", store._name(*first))
    assert len(list(store.directory.glob("*.dpapi"))) == 2
    assert store.load(*first) == {"access_token": "first-private-token"}
    assert store.load(*second) == {"access_token": "second-private-token"}
    assert store.delete(*first)
    assert store.load(*first) is None
    assert store.load(*second) == {"access_token": "second-private-token"}


def test_name_is_stable_and_does_not_expose_account_label():
    first = SecureTokenVault._name("google", "user+private@example.com")
    second = SecureTokenVault._name("google", "user+private@example.com")
    assert first == second and "user" not in first and "example" not in first


@pytest.mark.parametrize("provider,account", [
    (None, "owner"), ("google", None), ("", "owner"), ("google", ""),
    (" google", "owner"), ("google", "owner "), ("google", "a\nb"),
    ("google", "a\0b"), ("google", "x" * 16385),
])
def test_invalid_scope_does_not_create_files_or_call_crypto(vault, provider, account):
    store, crypto = vault
    for operation in (
        lambda: store.save(provider, account, {"access_token": "fixture"}),
        lambda: store.load(provider, account),
        lambda: store.delete(provider, account),
    ):
        with pytest.raises(RemoteRuntimeError):
            operation()
    assert not list(store.directory.iterdir()) and not crypto.payloads and not crypto.decrypt_calls


@pytest.mark.parametrize("account", ["a+b@example.com", "a_b@example.com"])
@pytest.mark.parametrize("operation", ["load", "delete"])
def test_ambiguous_legacy_token_requires_reconnect_without_decrypt_or_delete(vault, monkeypatch, account, operation):
    store, crypto = vault
    legacy = store.directory / "google_a_b_example_com.dpapi"
    legacy.write_bytes(b"legacy-encrypted-fixture")
    original_read = Path.read_bytes

    def forbid_legacy_read(path):
        assert path != legacy, "Legacy bytes must never be read to guess ownership"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_legacy_read)
    with pytest.raises(RemoteRuntimeError, match="다시 연결"):
        getattr(store, operation)("google", account)
    assert legacy.exists() and not crypto.decrypt_calls
    assert list(store.directory.iterdir()) == [legacy]


def test_reconnect_writes_v2_without_migrating_or_removing_legacy(vault):
    store, crypto = vault
    legacy = store.directory / "google_a_b_example_com.dpapi"
    legacy.write_bytes(b"legacy-encrypted-fixture")
    store.save("google", "a+b@example.com", {"access_token": "fresh-oauth-token"})
    assert store.load("google", "a+b@example.com") == {"access_token": "fresh-oauth-token"}
    assert legacy.read_bytes() == b"legacy-encrypted-fixture"
    assert b"legacy-encrypted-fixture" not in crypto.decrypt_calls
    with pytest.raises(RemoteRuntimeError, match="다시 연결"):
        store.load("google", "a_b@example.com")
    assert store.delete("google", "a+b@example.com") and legacy.exists()
    # Deleting the new token must never revive an ambiguous old credential.
    with pytest.raises(RemoteRuntimeError, match="다시 연결"):
        store.load("google", "a+b@example.com")


@pytest.mark.parametrize("destination", [("google", "other"), ("microsoft", "owner")])
def test_copied_ciphertext_is_rejected_for_wrong_bound_identity(vault, destination):
    store, _crypto = vault
    store.save("google", "owner", {"access_token": "owner-private-token"})
    token_path(store, *destination).write_bytes(token_path(store, "google", "owner").read_bytes())
    with pytest.raises(RemoteRuntimeError) as error:
        store.load(*destination)
    assert "owner-private-token" not in str(error.value)
    assert store.load("google", "owner") == {"access_token": "owner-private-token"}


@pytest.mark.parametrize("payload", [
    {"access_token": "unbound-token"},
    {"format": "anis.oauth.v1", "provider": "google", "account": "owner", "token": {}},
    {"format": "anis.oauth.v2", "provider": "google", "account": "owner", "token": []},
    {"format": "anis.oauth.v2", "provider": "google", "account": "other", "token": {}},
])
def test_unbound_or_malformed_envelopes_never_load_from_v2_path(vault, payload):
    store, crypto = vault
    _label, encrypted = crypto.CryptProtectData(json.dumps(payload).encode("utf-8"))
    token_path(store, "google", "owner").write_bytes(encrypted)
    with pytest.raises(RemoteRuntimeError):
        store.load("google", "owner")


def test_decryption_errors_are_redacted_and_do_not_fall_back_to_legacy(vault, monkeypatch):
    store, crypto = vault
    store.save("google", "owner", {"access_token": "private-value"})
    (store.directory / "google_owner.dpapi").write_bytes(b"legacy")

    def fail(*_args):
        raise ValueError("private-value should not enter logs")

    monkeypatch.setattr(crypto, "CryptUnprotectData", fail)
    with pytest.raises(RemoteRuntimeError) as error:
        store.load("google", "owner")
    assert "private-value" not in str(error.value)
    assert "private-value" not in repr(error.value)


def test_failed_atomic_replace_preserves_existing_token_and_removes_own_temp(vault, monkeypatch):
    store, _crypto = vault
    store.save("google", "owner", {"access_token": "old"})
    original = token_path(store, "google", "owner").read_bytes()

    def fail(*_args):
        raise OSError("fixture replace failure")

    monkeypatch.setattr("core.remote_runtime.os.replace", fail)
    with pytest.raises(OSError):
        store.save("google", "owner", {"access_token": "new"})
    assert token_path(store, "google", "owner").read_bytes() == original
    assert store.load("google", "owner") == {"access_token": "old"}
    assert not list(store.directory.glob("*.tmp"))


def test_exclusive_temp_collision_does_not_delete_another_writers_file(vault, monkeypatch):
    store, _crypto = vault
    name = store._name("google", "owner")
    other_temp = store.directory / f".{name}.fixed-test-id.tmp"
    other_temp.write_bytes(b"other-writer-fixture")
    monkeypatch.setattr("core.remote_runtime.uuid.uuid4", lambda: SimpleNamespace(hex="fixed-test-id"))
    with pytest.raises(FileExistsError):
        store.save("google", "owner", {"access_token": "new"})
    assert other_temp.read_bytes() == b"other-writer-fixture"
    assert not token_path(store, "google", "owner").exists()


def test_empty_vault_needs_no_crypto_module(vault, monkeypatch):
    store, _crypto = vault
    monkeypatch.setitem(sys.modules, "win32crypt", None)
    assert store.load("google", "missing") is None
    assert store.delete("google", "missing") is False
