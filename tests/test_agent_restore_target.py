"""A restore may only overwrite config this host actually tracks.

THE AGENT RUNS AS ROOT. Before this guard, _d3_apply_restore wrote whatever
absolute path the restore payload named — the only check was a leading "/". That
turns control of the controller, or a poisoned row in the version store, or a
bug in either, into root on EVERY managed host in one poll interval: drop a file
in /etc/cron.d, append to /root/.ssh/authorized_keys, write a systemd unit.

The agent is the last component in that chain that can say no, and it must not
have to trust the thing telling it what to write. A restore is only ever a
rollback of something this host captured, so the rule is the capture rule.
"""
import importlib.util
import os
import shutil
import tempfile

import pytest

AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "host_agent", "agent.py")


def _load(paths=None, exclude=None):
    if paths is not None:
        os.environ["SYSIBLE_D3LOREAN_PATHS"] = paths
    if exclude is not None:
        os.environ["SYSIBLE_D3LOREAN_EXCLUDE"] = exclude
    d = tempfile.mkdtemp()
    copy = os.path.join(d, "agent.py")
    shutil.copy(AGENT, copy)
    spec = importlib.util.spec_from_file_location("sysagent_restore", copy)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def ag(monkeypatch):
    monkeypatch.setenv("SYSIBLE_D3LOREAN_PATHS", "/etc:/boot/grub/grub.cfg:/var/spool/cron")
    monkeypatch.setenv("SYSIBLE_D3LOREAN_EXCLUDE",
                       "/etc/shadow:/etc/ssh/ssh_host_*_key:/etc/ssl/private/*")
    return _load()


# --- what a restore is FOR ---------------------------------------------------
@pytest.mark.parametrize("path", [
    "/etc/hosts",
    "/etc/nginx/nginx.conf",
    "/etc/systemd/system/thing.service",
    "/boot/grub/grub.cfg",                 # a tracked root that is a FILE
    "/var/spool/cron/crontabs/root",
])
def test_a_tracked_config_path_is_allowed(ag, path):
    ok, why = ag._d3_restorable(path)
    assert ok, f"{path} was refused: {why}"


# --- the kill chain this closes ----------------------------------------------
@pytest.mark.parametrize("path,label", [
    ("/root/.ssh/authorized_keys", "an SSH key for root"),
    ("/etc/../root/.ssh/authorized_keys", "the same, via traversal"),
    ("/usr/lib/systemd/system/evil.service", "a systemd unit"),
    ("/usr/local/bin/sysible-agent", "the agent's own binary"),
    ("/home/deploy/.bashrc", "someone's shell profile"),
    ("/tmp/x", "anywhere at all"),
    ("//etc/hosts", "a non-normal form of a tracked path"),
    ("/etc/./hosts", "the same again"),
])
def test_writing_outside_the_tracked_config_is_refused(ag, path, label):
    ok, why = ag._d3_restorable(path)
    assert not ok, f"{label} ({path}) was allowed"
    assert why


def test_a_relative_path_is_refused(ag):
    for p in ("etc/hosts", "", "../etc/hosts", "C:/windows"):
        assert not ag._d3_restorable(p)[0], p


def test_the_never_captured_files_are_never_restorable_either(ag):
    """These are excluded from capture BECAUSE they are credentials. A write path
    into them would be a way to set them."""
    for p in ("/etc/shadow", "/etc/ssh/ssh_host_ed25519_key", "/etc/ssl/private/site.key"):
        ok, why = ag._d3_restorable(p)
        assert not ok, p
        assert "exclude" in why


def test_a_host_that_tracks_nothing_restores_nothing(monkeypatch):
    monkeypatch.setenv("SYSIBLE_D3LOREAN_PATHS", "")
    m = _load()
    assert not m._d3_restorable("/etc/hosts")[0]


# --- symlinked parents -------------------------------------------------------
def test_a_symlinked_directory_cannot_walk_the_write_out_of_the_root(tmp_path, monkeypatch):
    """The classic escape: a tracked directory contains a symlink to somewhere
    else, and the write lands there instead. The FILE may be a symlink (that is
    what /etc/resolv.conf is, and os.replace swaps the link itself); its PARENT
    may not."""
    root = tmp_path / "etc"
    (root / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)

    monkeypatch.setenv("SYSIBLE_D3LOREAN_PATHS", str(root))
    monkeypatch.setenv("SYSIBLE_D3LOREAN_EXCLUDE", "")
    m = _load()
    assert m._d3_restorable(str(root / "real" / "conf"))[0]
    ok, why = m._d3_restorable(str(root / "escape" / "authorized_keys"))
    assert not ok, "a symlinked directory inside the root walked the write out of it"
    assert "resolves outside" in why


def test_the_file_itself_may_still_be_a_symlink(tmp_path, monkeypatch):
    """/etc/resolv.conf is a symlink on most systems and must stay restorable."""
    root = tmp_path / "etc"
    root.mkdir()
    target = tmp_path / "run-resolv.conf"
    target.write_text("nameserver 1.1.1.1\n")
    (root / "resolv.conf").symlink_to(target)
    monkeypatch.setenv("SYSIBLE_D3LOREAN_PATHS", str(root))
    monkeypatch.setenv("SYSIBLE_D3LOREAN_EXCLUDE", "")
    m = _load()
    assert m._d3_restorable(str(root / "resolv.conf"))[0]


# --- the refusal has to be visible -------------------------------------------
def test_a_refused_restore_is_acked_as_failed_not_left_pending(ag, monkeypatch):
    """Silence would leave it pending forever and tell nobody the host said no."""
    acks = []
    monkeypatch.setattr(ag, "_d3_ack",
                        lambda state, rid, ok, path="": acks.append((rid, ok, path)))

    class _R:
        status_code = 200
        headers = {"X-Flashback-Path": "/root/.ssh/authorized_keys",
                   "X-Flashback-Sha256": ""}
        content = b"ssh-rsa AAAA attacker\n"
    monkeypatch.setattr(ag, "_request", lambda *a, **kw: _R())
    written = []
    monkeypatch.setattr(ag, "open", lambda *a, **kw: written.append(a) or (_ for _ in ()).throw(
        AssertionError("the agent opened a file it should have refused")), raising=False)

    ag._d3_apply_restore({"host_id": "h1", "agent_secret": "s"}, {"id": 7})
    assert acks == [(7, False, "/root/.ssh/authorized_keys")]
