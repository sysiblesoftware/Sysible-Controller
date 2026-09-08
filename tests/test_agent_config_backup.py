"""The agent half of config backup: what it reads, and what it writes back.

The write side deserves the scrutiny. A restore is the ONLY thing in the agent
that changes a file on a managed host, it runs unattended, and it targets /etc —
so it verifies the digest before writing, keeps what it replaced, and swaps
atomically. Each of those is pinned here.

The read side matters for a different reason: it walks /etc, and /etc holds
credentials. Config history is not a secret store, so the default exclusions are
tested as behaviour rather than left as a comment.

Loads agent.py by path (no enrollment, no network) like the other agent tests.
"""
import base64
import hashlib
import importlib.util
import os
import shutil
import tempfile

import pytest

_AGENT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "host_agent", "agent.py")


@pytest.fixture
def ag():
    d = tempfile.mkdtemp()
    copy = os.path.join(d, "agent.py")
    shutil.copy(_AGENT_SRC, copy)
    spec = importlib.util.spec_from_file_location("sysagent_cfgbackup", copy)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _tree(root, files):
    for rel, content in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(content if isinstance(content, bytes) else content.encode())
    return root


def _collect(ag, root, **over):
    ag.D3_PATHS = root
    ag.D3_EXCLUDE = over.get("exclude", ":".join([
        f"{root}/shadow", f"{root}/ssh/ssh_host_*_key", f"{root}/ssl/private/*",
    ]))
    ag.D3_MAX_FILE = over.get("max_file", 256 * 1024)
    ag.D3_MAX_TOTAL = over.get("max_total", 8 * 1024 * 1024)
    return ag._d3_collect()


def _paths(files):
    return sorted(f["path"] for f in files)


# ---- what gets captured ----------------------------------------------------
def test_it_captures_the_tracked_files(ag, tmp_path):
    root = _tree(str(tmp_path / "etc"), {"hosts": "127.0.0.1", "ssh/sshd_config": "Port 22"})
    files, skipped, truncated = _collect(ag, root)
    assert _paths(files) == [f"{root}/hosts", f"{root}/ssh/sshd_config"]
    assert not truncated
    # Content round-trips as base64, which is what the relay expects.
    got = {f["path"]: base64.b64decode(f["content_b64"]) for f in files}
    assert got[f"{root}/hosts"] == b"127.0.0.1"


def test_credentials_are_excluded_by_default(ag, tmp_path):
    """Config history is not a secret store. /etc/shadow and private host keys
    would otherwise be copied into a fleet-wide store, kept for 50 versions and
    readable by anyone with console access."""
    root = _tree(str(tmp_path / "etc"), {
        "hosts": "ok",
        "shadow": "root:$6$hash:1::::::",
        "ssh/ssh_host_ed25519_key": "PRIVATE KEY",
        "ssh/ssh_host_ed25519_key.pub": "public ok",
        "ssl/private/server.key": "PRIVATE",
    })
    files, _, _ = _collect(ag, root)
    got = _paths(files)
    assert f"{root}/hosts" in got
    assert f"{root}/ssh/ssh_host_ed25519_key.pub" in got     # public half is fine
    for secret in ("shadow", "ssh/ssh_host_ed25519_key", "ssl/private/server.key"):
        assert f"{root}/{secret}" not in got, f"{secret} was captured"


def test_the_default_exclusions_really_are_the_shipped_ones(ag):
    """The list above is the test's own; assert the DEFAULT the agent ships with
    covers the same ground, so a host that sets nothing is still safe."""
    for pat in ("/etc/shadow", "/etc/gshadow", "/etc/ssh/ssh_host_*_key", "/etc/ssl/private/*"):
        assert pat in ag.D3_EXCLUDE


def test_an_oversized_file_is_skipped_not_truncated(ag, tmp_path):
    """A big log or database that wandered into a tracked path must not be
    silently stored half-written — a truncated config is worse than none."""
    root = _tree(str(tmp_path / "etc"), {"hosts": "small", "huge.db": "x" * 5000})
    files, skipped, _ = _collect(ag, root, max_file=1000)
    assert _paths(files) == [f"{root}/hosts"]
    assert skipped == 1


def test_hitting_the_total_cap_reports_truncation(ag, tmp_path):
    """The caller prints this, so an operator whose /etc outgrew the budget finds
    out rather than quietly backing up a random subset."""
    root = _tree(str(tmp_path / "etc"), {f"f{i}": "y" * 500 for i in range(20)})
    files, _, truncated = _collect(ag, root, max_total=2000)
    assert truncated is True
    assert 0 < len(files) < 20


def test_non_regular_files_are_skipped(ag, tmp_path):
    root = str(tmp_path / "etc")
    os.makedirs(root)
    os.mkfifo(os.path.join(root, "a-fifo"))
    with open(os.path.join(root, "hosts"), "w") as fh:
        fh.write("ok")
    files, _, _ = _collect(ag, root)
    assert _paths(files) == [f"{root}/hosts"]


def test_a_dangling_symlink_does_not_abort_the_walk(ag, tmp_path):
    """Best-effort is the point: one bad entry must not cost the whole snapshot."""
    root = str(tmp_path / "etc")
    os.makedirs(root)
    os.symlink(os.path.join(root, "gone"), os.path.join(root, "dangling"))
    with open(os.path.join(root, "hosts"), "w") as fh:
        fh.write("ok")
    files, skipped, _ = _collect(ag, root)
    assert _paths(files) == [f"{root}/hosts"]
    assert skipped >= 1


# ---- what gets written back ------------------------------------------------
class _Resp:
    def __init__(self, content=b"", headers=None, status=200):
        self.content = content
        self.headers = headers or {}
        self.status_code = status

    def json(self):
        return {}


def _restore_harness(ag, target, content, sha=None, monkeypatch=None):
    """Drive _d3_apply_restore with a canned payload; record the ack."""
    acked = {}
    digest = sha if sha is not None else hashlib.sha256(content).hexdigest()

    def fake_request(method, path, **kw):
        if "/payload" in path:
            return _Resp(content, {"X-Flashback-Path": target, "X-Flashback-Sha256": digest})
        if "/ack" in path:
            acked.update(kw.get("json") or {})
            return _Resp(b"{}")
        return _Resp(b"{}")

    ag._request = fake_request
    ag._d3_apply_restore({"host_id": "h", "agent_secret": "s"}, {"id": 7})
    return acked


def test_a_restore_writes_the_file_and_acks(ag, tmp_path):
    target = str(tmp_path / "hosts")
    acked = _restore_harness(ag, target, b"new content")
    assert open(target, "rb").read() == b"new content"
    assert acked.get("ok") is True


def test_a_restore_keeps_what_it_replaced(ag, tmp_path):
    """A restore is the one operation that changes the host. 'I restored the
    wrong version' needs a way back that does not involve another restore."""
    target = str(tmp_path / "hosts")
    with open(target, "wb") as fh:
        fh.write(b"the previous config")
    _restore_harness(ag, target, b"new content")
    backups = [p for p in os.listdir(tmp_path) if ".sysible-backup-" in p]
    assert len(backups) == 1
    assert open(tmp_path / backups[0], "rb").read() == b"the previous config"


def test_a_digest_mismatch_is_refused_and_acked_as_failed(ag, tmp_path):
    """Bytes that do not match what the store recorded are never written — and
    the restore is acked FAILED so the console shows it instead of leaving it
    pending forever."""
    target = str(tmp_path / "hosts")
    with open(target, "wb") as fh:
        fh.write(b"original")
    acked = _restore_harness(ag, target, b"tampered", sha="0" * 64)
    assert open(target, "rb").read() == b"original", "wrote unverified content"
    assert acked.get("ok") is False


def test_the_file_mode_survives_a_restore(ag, tmp_path):
    """Restoring sudoers or a key-bearing config with the wrong mode would break
    the very thing being repaired."""
    target = str(tmp_path / "sudoers")
    with open(target, "wb") as fh:
        fh.write(b"old")
    os.chmod(target, 0o440)
    _restore_harness(ag, target, b"new")
    assert oct(os.stat(target).st_mode & 0o7777) == oct(0o440)


def test_a_non_absolute_path_is_refused(ag, tmp_path, monkeypatch):
    """The path comes from the server; treat it as untrusted anyway."""
    monkeypatch.chdir(tmp_path)
    acked = _restore_harness(ag, "relative/evil", b"x")
    assert not os.path.exists(tmp_path / "relative/evil")
    assert acked == {}, "should not even ack a path it refuses to interpret"


def test_no_temp_file_is_left_behind(ag, tmp_path):
    target = str(tmp_path / "hosts")
    _restore_harness(ag, target, b"new content")
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".sysible-restore.tmp")]
    assert leftovers == []
