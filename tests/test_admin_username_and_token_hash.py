"""Two hardening controls audited for the release:

1. Admin console usernames are charset-validated at ingest, so a value carrying
   shell metacharacters can never reach a command builder. This is the ingest
   backstop for the remote_routes "user does not exist" echo de-taint — together
   they close a command-injection → root-RCE path on SSH-managed hosts.
2. Session / bearer tokens are stored only as their SHA-256 at rest, so a leaked
   DB snapshot yields no directly-replayable live sessions.
"""
import hashlib
import time

import pytest

import backend.db as db


# --- Admin username charset validation --------------------------------------

def test_admin_username_rejects_shell_metacharacters():
    from pydantic import ValidationError
    from backend.models.portal_models import (
        AddAdministratorRequest, AdminSetupRequest, ChangeAdminCredentialsRequest)
    bad_names = ["svc$(id)", "a`whoami`", "x;reboot", "b|c", "d'e", 'f"g',
                 "-leadingdash", "has space", "nl\nx", "amp&y"]
    for bad in bad_names:
        with pytest.raises(ValidationError):
            AddAdministratorRequest(username=bad, password="Password123!")
        with pytest.raises(ValidationError):
            AdminSetupRequest(username=bad, password="Password123!")
        with pytest.raises(ValidationError):
            ChangeAdminCredentialsRequest(username="ok", current_password="x",
                                          new_username=bad, new_password="y")


def test_admin_username_accepts_normal_names():
    from backend.models.portal_models import AddAdministratorRequest
    for good in ["alice", "bob.smith", "svc_acct", "ops-team", "user@corp"]:
        assert AddAdministratorRequest(username=good, password="Password123!").username == good


def test_change_credentials_empty_new_username_allowed():
    # Empty new_username means "keep the current name" (the route falls back to
    # `username`); it must not be rejected by the rename charset check.
    from backend.models.portal_models import ChangeAdminCredentialsRequest
    r = ChangeAdminCredentialsRequest(username="alice", current_password="x",
                                      new_username="", new_password="y")
    assert r.new_username == ""


# --- Token hashing at rest --------------------------------------------------

def test_admin_token_hashed_at_rest(make_admin):
    token = make_admin("alice", "superuser")
    # The raw token still resolves (client presents the plaintext)...
    res = db.resolve_admin_token(token)
    assert res and res["username"] == "alice"
    # ...but the DB stores only its SHA-256, never the replayable value.
    conn = db._connect()
    stored = [row[0] for row in conn.execute("SELECT token FROM admin_tokens").fetchall()]
    conn.close()
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored


def test_admin_token_delete_by_plaintext(make_admin):
    token = make_admin("bob", "sysadmin")
    db.delete_admin_token(token)
    assert db.resolve_admin_token(token) is None


def test_portal_session_hashed_at_rest():
    db.create_portal_session("PLAINPORTALTOK", time.time() + 3600)
    assert db.get_portal_session("PLAINPORTALTOK") is not None  # round-trips via hash
    conn = db._connect()
    stored = [row[0] for row in conn.execute("SELECT token FROM portal_sessions").fetchall()]
    conn.close()
    assert "PLAINPORTALTOK" not in stored
    assert hashlib.sha256(b"PLAINPORTALTOK").hexdigest() in stored
