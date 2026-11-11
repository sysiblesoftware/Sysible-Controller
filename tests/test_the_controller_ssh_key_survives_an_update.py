"""The Controller's own SSH key: where it lives, and what happens when it can't
be made.

Reported as "ssh doesn't seem to be working", with a bare Internal Server Error
on Enroll a host from here. _ensure_controller_key() runs BEFORE the enrollment
route's try/except, so every way it can fail arrived as an unhandled 500 with no
detail -- a missing ssh-keygen and an unwritable directory were indistinguishable
from a bug in the code.

The second half is the one that would not have announced itself at all. The key
used to default under /opt/sysible, which in the container is an image layer:
pointing it at the /data volume is the fix, and on a host where the old path
still holds a working key, doing that naively means the next enrollment
generates a FRESH keypair. Every host that already has the old public key in its
authorized_keys becomes unreachable, and nothing anywhere raises. So the old key
is adopted before any decision to generate one.

Run: pytest tests/
"""
import os
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException


@pytest.fixture()
def rr(tmp_path, monkeypatch):
    """backend.remote_routes with its paths pointed at a tmp dir."""
    import backend.remote_routes as mod
    monkeypatch.setattr(mod, "REMOTE_KEY_DIR", tmp_path / "data" / "remote_keys")
    monkeypatch.setattr(mod, "CONTROLLER_KEY_PATH",
                        tmp_path / "data" / "remote_keys" / "controller_ed25519")
    monkeypatch.setattr(mod, "CONTROLLER_PUB_KEY_PATH",
                        tmp_path / "data" / "remote_keys" / "controller_ed25519.pub")
    monkeypatch.setattr(mod, "HOST_FILE", tmp_path / "data" / "hosts.json")
    monkeypatch.setattr(mod, "_LEGACY_HOME", tmp_path / "legacy")
    monkeypatch.setattr(mod, "_adopted", False)
    return mod


def _seed_legacy(rr, pub="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILEGACYKEY controller"):
    d = rr._LEGACY_HOME / "remote_keys"
    d.mkdir(parents=True, exist_ok=True)
    (d / "controller_ed25519").write_text("PRIVATE-KEY-MATERIAL\n")
    (d / "controller_ed25519.pub").write_text(pub + "\n")
    (d / "known_hosts").write_text("10.0.0.5 ssh-ed25519 AAAAPINNED\n")
    (rr._LEGACY_HOME / "hosts.json").write_text('{"web-01": {"ip": "10.0.0.5"}}')
    return pub


# ---- the key is carried across, not regenerated ---------------------------
def test_a_key_at_the_old_path_is_adopted_rather_than_replaced(rr):
    """The whole point. A new keypair here means every enrolled host is
    unreachable, silently -- their authorized_keys hold the old public key."""
    pub = _seed_legacy(rr)
    got = rr._ensure_controller_key()
    assert got == pub, (
        "the Controller generated a NEW key instead of adopting the one its "
        "fleet already trusts")
    assert rr.CONTROLLER_KEY_PATH.read_text() == "PRIVATE-KEY-MATERIAL\n"


def test_the_pinned_host_keys_come_too(rr):
    """known_hosts is what makes a changed host key a refusal rather than a
    silent re-trust. Leaving it behind re-opens every host to trust-on-first-use
    once more."""
    _seed_legacy(rr)
    rr._ensure_controller_key()
    assert (rr.REMOTE_KEY_DIR / "known_hosts").read_text() == \
        "10.0.0.5 ssh-ed25519 AAAAPINNED\n"


def test_the_host_records_come_too(rr):
    """hosts.json defaulted under /opt/sysible as well. Left behind, every
    SSH-enrolled host disappears from the console on the first update."""
    _seed_legacy(rr)
    rr._ensure_controller_key()
    assert "web-01" in rr.HOST_FILE.read_text()


def test_adoption_never_overwrites_what_is_already_there(rr):
    """A host that has been running on /data has the real key there. The old
    copy is a stale image layer and must not win."""
    _seed_legacy(rr)
    rr.REMOTE_KEY_DIR.mkdir(parents=True)
    rr.CONTROLLER_KEY_PATH.write_text("CURRENT-KEY\n")
    rr.CONTROLLER_PUB_KEY_PATH.write_text("ssh-ed25519 AAAACURRENT controller\n")
    got = rr._ensure_controller_key()
    assert "CURRENT" in got
    assert rr.CONTROLLER_KEY_PATH.read_text() == "CURRENT-KEY\n"


def test_adoption_runs_once_and_does_not_raise_on_a_missing_old_path(rr):
    """A fresh install has no legacy anything. That is the normal case."""
    assert not rr._LEGACY_HOME.exists()
    rr._adopt_legacy_state()   # must not raise
    rr._adopt_legacy_state()


def test_the_private_key_is_not_left_world_readable(rr):
    _seed_legacy(rr)
    rr._ensure_controller_key()
    assert oct(rr.CONTROLLER_KEY_PATH.stat().st_mode)[-3:] == "600"


# ---- failures say what is wrong -------------------------------------------
def test_a_missing_ssh_keygen_says_so(rr, monkeypatch):
    """subprocess raises FileNotFoundError, which is an OSError, which arrived
    as 'Internal Server Error'. The image was simply missing openssh-client."""
    def _boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "ssh-keygen")
    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(HTTPException) as e:
        rr._ensure_controller_key()
    assert "ssh-keygen" in e.value.detail
    assert "openssh-client" in e.value.detail, \
        f"it does not say what to install: {e.value.detail!r}"


def test_an_unwritable_key_directory_says_so(rr, monkeypatch):
    """The other half of the original bug: /opt/sysible is not writable by the
    container's service user, and the mkdir raised PermissionError."""
    def _boom(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "mkdir", _boom)
    with pytest.raises(HTTPException) as e:
        rr._ensure_controller_key()
    assert "/data" in e.value.detail and "SYSIBLE_REMOTE_KEY_DIR" in e.value.detail, \
        f"it does not say where the key belongs: {e.value.detail!r}"


def test_a_stuck_ssh_keygen_is_a_timeout_not_a_hang(rr, monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh-keygen", timeout=30)
    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(HTTPException) as e:
        rr._ensure_controller_key()
    assert e.value.status_code == 504
    assert "entropy" in e.value.detail


def test_none_of_these_leave_a_half_written_keypair(rr, monkeypatch):
    """ssh-keygen refuses to overwrite, so a leftover half permanently wedges
    every later attempt."""
    def _boom(*a, **k):
        raise FileNotFoundError(2, "nope", "ssh-keygen")
    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(HTTPException):
        rr._ensure_controller_key()
    assert not rr.CONTROLLER_KEY_PATH.exists()
    assert not rr.CONTROLLER_PUB_KEY_PATH.exists()


# ---- and it really does generate one when it can --------------------------
@pytest.mark.skipif(not __import__("shutil").which("ssh-keygen"),
                    reason="ssh-keygen is not on this machine")
def test_it_generates_a_usable_key_when_nothing_is_in_the_way(rr):
    pub = rr._ensure_controller_key()
    assert rr._looks_like_ssh_public_key(pub), f"generated a malformed key: {pub!r}"
    assert pub.startswith("ssh-ed25519 ")
    # Called twice, it returns the SAME key rather than making another.
    assert rr._ensure_controller_key() == pub
