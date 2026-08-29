"""Regression guard for the alerts SMTP-password crypto fix.

alerts.py called the removed sudo_store._get_key(), so saving email-alert config
with an SMTP password AttributeError'd (500) and any stored password stopped
decrypting. It now uses the store's primary key to encrypt and iterates the
custody keys to decrypt — mirroring webgui/sudo_store. This round-trips an SMTP
password through set_config -> _load -> _decrypt.
"""
import os
import tempfile

os.environ.setdefault("SYSIBLE_API_KEY", "test-alerts-key")
os.environ.setdefault("SYSIBLE_DATA_DIR", tempfile.mkdtemp(prefix="sysible-alerts-"))
# Isolate the alert-config + key files to a throwaway run dir.
os.environ.setdefault("SYSIBLE_RUN_DIR", tempfile.mkdtemp(prefix="sysible-alerts-run-"))

import pytest  # noqa: E402

from webgui import alerts, sudo_store  # noqa: E402


@pytest.mark.skipif(not sudo_store.encryption_available(),
                    reason="cryptography not installed")
def test_smtp_password_roundtrips_through_set_config():
    redacted = alerts.set_config({
        "channels": {"email": {"enabled": True, "smtp_host": "smtp.example",
                               "username": "mailer", "password": "s3cr3t-smtp-pw"}},
    })
    # The redacted UI view never returns the plaintext, only a has_password flag.
    assert redacted["channels"]["email"]["has_password"] is True
    assert "password" not in redacted["channels"]["email"]

    # The stored ciphertext decrypts back to the original (no 500, real crypto).
    cfg = alerts._load()
    token = cfg["channels"]["email"]["password_enc"]
    assert token and token != "s3cr3t-smtp-pw"          # actually encrypted at rest
    assert alerts._decrypt(token) == "s3cr3t-smtp-pw"    # round-trips


def test_decrypt_of_blank_is_empty():
    assert alerts._decrypt("") == ""
