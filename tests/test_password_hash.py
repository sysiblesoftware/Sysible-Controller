"""Regression: password hashing must not depend on the stdlib `crypt` module.

`crypt` was removed from the standard library in Python 3.13 and never existed
on non-POSIX platforms, so a controller on a modern Python could no longer build
`usermod -p '$6$...'` commands - the console surfaced "Password hashing
unavailable on this client" and refused to create users with a password. The
pure-Python SHA-512 crypt fallback keeps hashing local to the controller on any
platform/Python.
"""
import builtins

import pytest

import client.api  # noqa: F401  (initialise the client package / resolve import order)
import client._api_users as U


def _force_no_crypt(monkeypatch):
    """Make `import crypt` raise, as it does on Python 3.13+, to exercise the
    pure-Python fallback path."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "crypt":
            raise ImportError("No module named 'crypt'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _validate(password, hashed):
    """A valid $6$ hash re-hashes the same password to itself under system
    crypt; skip the assertion only where system crypt is genuinely absent."""
    assert hashed.startswith("$6$")
    try:
        import crypt
    except ImportError:  # non-POSIX / Py3.13 test host - shape check only
        assert len(hashed.split("$")[3]) == 86
        return
    assert crypt.crypt(password, hashed) == hashed


def test_hash_password_present_crypt():
    pw = "An%R^P@rCFfq8n6_"
    _validate(pw, U._hash_password(pw))


def test_hash_password_fallback(monkeypatch):
    _force_no_crypt(monkeypatch)
    pw = "An%R^P@rCFfq8n6_"
    _validate(pw, U._hash_password(pw))


@pytest.mark.parametrize("pw", [
    "simple",
    "with spaces",
    "quotes'and\"stuff",
    "unicode-pä$$-🔒",
    "x" * 256,
    "$dollar$signs$",
])
def test_fallback_matches_system_crypt(monkeypatch, pw):
    """The pure-Python hash must be byte-for-byte what glibc crypt() produces."""
    pytest.importorskip("crypt")
    import crypt
    _force_no_crypt(monkeypatch)
    hashed = U._hash_password(pw)
    monkeypatch.undo()  # restore import so we can call the real crypt
    assert crypt.crypt(pw, hashed) == hashed


def test_cmd_set_password_no_longer_raises(monkeypatch):
    _force_no_crypt(monkeypatch)
    cmd = U.cmd_set_password("admin", "An%R^P@rCFfq8n6_")
    # The hash is applied via `chpasswd -e` over a heredoc (not `usermod -p` on
    # argv) so the crypt hash is never exposed via ps/procfs during exec.
    assert cmd.startswith("chpasswd -e ")
    assert "admin:$6$" in cmd


def test_cmd_create_user_with_password_builds(monkeypatch):
    _force_no_crypt(monkeypatch)
    cmd = U.cmd_create_user("admin", "An%R^P@rCFfq8n6_", "/bin/bash")
    assert "useradd -m" in cmd
    assert "chpasswd -e" in cmd  # password change chained on, no exception


def test_official_spec_vector():
    """glibc SHA-crypt.txt reference vector, via the pure-Python path."""
    got = U._sha512_crypt("Hello world!", "saltstring")
    assert got == (
        "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uB"
        "nIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1"
    )
