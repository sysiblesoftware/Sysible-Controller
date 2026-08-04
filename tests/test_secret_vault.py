"""Controller secret vault (backend/secret_vault.py) + the console sudo-password
store's migration onto master-key-derived custody.

Covers: encrypt/decrypt round-trip and the 'v1:' envelope, legacy-plaintext
passthrough, available()/key_source() across the three key sources, strict-mode
fail-closed, derive_fernet_key determinism + domain separation, and that a
sudo-store token written under the old co-located key still decrypts after the
host gains a master key (and re-wraps on next save).
"""
import importlib
import json
import os
from pathlib import Path

import pytest

from backend import secret_vault as sv


@pytest.fixture(autouse=True)
def _isolated_run_dir(tmp_path, monkeypatch):
    """Each test gets its own run dir + a clean, uncached key state."""
    monkeypatch.setenv("SYSIBLE_RUN_DIR", str(tmp_path))
    monkeypatch.delenv("SYSIBLE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SYSIBLE_SECRET_KEY_CMD", raising=False)
    monkeypatch.delenv("SYSIBLE_SECRET_REQUIRED", raising=False)
    sv.reset_cache()
    yield
    sv.reset_cache()


def test_roundtrip_and_v1_envelope():
    token = sv.encrypt("hunter2")
    assert token.startswith("v1:")
    assert token != "hunter2"
    assert sv.is_encrypted(token)
    assert sv.decrypt(token) == "hunter2"


def test_none_passes_through():
    assert sv.encrypt(None) is None
    assert sv.decrypt(None) is None


def test_legacy_plaintext_passthrough():
    # A value stored before encryption was on has no 'v1:' prefix — decrypt must
    # return it unchanged rather than choke.
    assert not sv.is_encrypted("plaintext-secret")
    assert sv.decrypt("plaintext-secret") == "plaintext-secret"


def test_local_file_key_source(tmp_path):
    assert sv.key_source() == "local-file"
    assert sv.available() is True
    # The 0600 key file was created on first use.
    kf = tmp_path / "controller_secret.key"
    assert kf.exists()
    assert (kf.stat().st_mode & 0o777) == 0o600


def test_env_key_source(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SYSIBLE_SECRET_KEY", Fernet.generate_key().decode())
    sv.reset_cache()
    assert sv.key_source() == "env"
    assert sv.decrypt(sv.encrypt("x")) == "x"


def test_env_key_may_be_passphrase(monkeypatch):
    # A non-Fernet high-entropy string must still enable encryption (derived key),
    # not silently disable it.
    monkeypatch.setenv("SYSIBLE_SECRET_KEY", "a-long-random-passphrase-not-fernet")
    sv.reset_cache()
    assert sv.available() is True
    assert sv.decrypt(sv.encrypt("payload")) == "payload"


def test_command_key_source(monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SYSIBLE_SECRET_KEY_CMD", f"printf %s {key}")
    sv.reset_cache()
    assert sv.key_source() == "command"
    assert sv.decrypt(sv.encrypt("y")) == "y"


def test_command_key_failure_fails_closed(monkeypatch):
    # An off-box key source that yields nothing must NOT fall through to a local
    # file key, and in strict mode must refuse to store plaintext.
    monkeypatch.setenv("SYSIBLE_SECRET_KEY_CMD", "false")
    monkeypatch.setenv("SYSIBLE_SECRET_REQUIRED", "1")
    sv.reset_cache()
    assert sv.available() is False
    with pytest.raises(sv.EncryptionUnavailable):
        sv.encrypt("must-not-store-plaintext")


def test_non_strict_degrades_to_plaintext(monkeypatch):
    monkeypatch.setenv("SYSIBLE_SECRET_KEY_CMD", "false")
    monkeypatch.setenv("SYSIBLE_SECRET_REQUIRED", "0")
    sv.reset_cache()
    assert sv.encrypt("z") == "z"  # warned once, stored as-is


def test_derive_fernet_key_deterministic_and_domain_separated():
    k1 = sv.derive_fernet_key(b"webgui-sudo")
    k2 = sv.derive_fernet_key(b"webgui-sudo")
    k3 = sv.derive_fernet_key(b"other-purpose")
    assert k1 == k2 and k1 is not None      # deterministic
    assert k1 != k3                          # domain-separated
    # It is a usable Fernet key AND differs from the audit HMAC key.
    from cryptography.fernet import Fernet
    assert Fernet(k1).decrypt(Fernet(k1).encrypt(b"ok")) == b"ok"
    assert k1 != sv.audit_hmac_key()


def test_audit_hmac_key_present_with_local_key():
    assert sv.audit_hmac_key() is not None


# ---------------------------------------------------------------------------
# Console sudo-store custody migration
# ---------------------------------------------------------------------------
def _fresh_sudo_store(tmp_path, monkeypatch):
    """Import webgui.sudo_store bound to this test's run dir."""
    monkeypatch.setenv("SYSIBLE_RUN_DIR", str(tmp_path))
    import webgui.sudo_store as store
    importlib.reload(store)
    return store


def test_sudo_store_uses_derived_key_when_master_present(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SYSIBLE_SECRET_KEY", Fernet.generate_key().decode())
    sv.reset_cache()
    store = _fresh_sudo_store(tmp_path, monkeypatch)
    assert store.set_password("alice", store.ALL, "s3cret") is True
    # No legacy key file should have been created — custody is the master key.
    assert not (tmp_path / "webgui_sudo.key").exists()
    assert store.resolve("alice", "web01") == "s3cret"


def test_sudo_store_legacy_token_still_readable_after_master_key(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    # Phase 1: no master key -> legacy co-located key path.
    monkeypatch.setenv("SYSIBLE_SECRET_KEY_CMD", "false")  # forces no master key
    monkeypatch.setenv("SYSIBLE_SECRET_REQUIRED", "0")
    sv.reset_cache()
    store = _fresh_sudo_store(tmp_path, monkeypatch)
    assert store.set_password("bob", store.ALL, "old-pass") is True
    assert (tmp_path / "webgui_sudo.key").exists()  # legacy key was minted

    # Phase 2: a master key now resolves. The legacy token must still decrypt.
    monkeypatch.delenv("SYSIBLE_SECRET_KEY_CMD", raising=False)
    monkeypatch.setenv("SYSIBLE_SECRET_KEY", Fernet.generate_key().decode())
    sv.reset_cache()
    store = _fresh_sudo_store(tmp_path, monkeypatch)
    assert store.resolve("bob", "anyhost") == "old-pass"  # read-both worked

    # A re-save re-wraps under the derived key; the token now decrypts even if the
    # legacy key file is removed.
    assert store.set_password("bob", store.ALL, "new-pass") is True
    (tmp_path / "webgui_sudo.key").unlink()
    store = _fresh_sudo_store(tmp_path, monkeypatch)
    assert store.resolve("bob", "anyhost") == "new-pass"
