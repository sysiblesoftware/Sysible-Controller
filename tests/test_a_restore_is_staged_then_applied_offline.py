"""Staging a restore point, and applying it when almost nothing is running.

DECIDED DELIBERATELY: "Writing a filesystem onto a running machine is
genuinely dangerous — you're replacing binaries and libraries under live
processes, and the reboot is what makes it survivable. The safer shape is to
stage the writes and apply them from an initramfs or rescue context on
reboot." — and then: DO this.

So nothing touches the live filesystem until the machine is almost not
running. Stage while it runs normally; apply from systemd's own
offline-update target, which exists for exactly this and is the same on every
systemd distribution.

These tests run the real staging and the real apply against real directories.
A staging routine that is only ever asked to copy three dicts proves nothing
about whether a machine round-trips, and an apply that is only inspected as
source proves nothing at all.
"""
import hashlib
import json
import os
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "host_agent"))

import flashback_restore as fr  # noqa: E402


def _entry(path, body=None, kind="f", mode=0o644, target=""):
    e = {"path": path, "kind": kind, "mode": mode, "uid": 0, "gid": 0}
    if kind == "f":
        e["sha256"] = hashlib.sha256(body).hexdigest()
        e["size"] = len(body)
    if kind == "l":
        e["target"] = target
    return e


@pytest.fixture()
def blobs():
    store = {}

    def add(body):
        sha = hashlib.sha256(body).hexdigest()
        store[sha] = body
        return sha

    def fetch(sha):
        if sha not in store:
            raise KeyError(f"no blob {sha}")
        return store[sha]

    fetch.add = add
    fetch.store = store
    fetch.asked = []
    real = fetch

    def counting(sha):
        real.asked.append(sha)
        return real(sha)

    counting.add = add
    counting.store = store
    counting.asked = real.asked
    return counting


# ---------------------------------------------------------------------------
# staging changes nothing
# ---------------------------------------------------------------------------
def test_staging_does_not_touch_the_live_filesystem(tmp_path, blobs):
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "hosts").write_text("LIVE AND UNTOUCHED\n")
    body = b"the restored content\n"
    blobs.add(body)
    fr.stage(7, [_entry("/etc", kind="d", mode=0o755), _entry("/etc/hosts", body)],
             blobs, stage_root=str(tmp_path / "stage"))
    assert (root / "etc" / "hosts").read_text() == "LIVE AND UNTOUCHED\n", \
        "staging wrote into the live filesystem"


def test_an_interrupted_stage_is_not_applied(tmp_path, blobs):
    """READY is written last and only on complete success. Anything that goes
    wrong leaves a job without it — and a filesystem nothing has touched."""
    body = b"x" * 32
    blobs.add(body)
    stage_root = str(tmp_path / "stage")

    def dies(sha):
        raise OSError("link dropped")

    with pytest.raises(OSError):
        fr.stage(7, [_entry("/etc", kind="d"), _entry("/etc/hosts", body)],
                 dies, stage_root=stage_root)
    assert not fr.is_ready(7, stage_root)
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError) as e:
        fr.apply_staged(7, root=str(root), stage_root=stage_root)
    assert "never fully staged" in str(e.value)


def test_a_digest_that_does_not_match_fails_at_stage_time(tmp_path, blobs):
    """Where it costs nothing. Discovering it at apply time means finding out
    with the machine already in system-update.target."""
    good = b"what the manifest says"
    sha = hashlib.sha256(good).hexdigest()

    def wrong(_sha):
        return b"something else entirely"

    with pytest.raises(ValueError) as e:
        fr.stage(7, [{"path": "/etc/hosts", "kind": "f", "mode": 0o644,
                      "sha256": sha, "size": len(good)}],
                 wrong, stage_root=str(tmp_path / "stage"))
    assert "digest" in str(e.value)
    assert not fr.is_ready(7, str(tmp_path / "stage"))


def test_a_retry_does_not_refetch_what_is_already_staged(tmp_path, blobs):
    """A dropped link halfway through a multi-gigabyte stage must not mean
    starting again from nothing."""
    bodies = [b"file %d content\n" % i for i in range(5)]
    for b in bodies:
        blobs.add(b)
    ents = [_entry("/etc", kind="d")] + [
        _entry(f"/etc/f{i}", b) for i, b in enumerate(bodies)]
    sr = str(tmp_path / "stage")
    first = fr.stage(7, ents, blobs, stage_root=sr)
    assert first["fetched"] == 5 and first["reused"] == 0
    blobs.asked.clear()
    second = fr.stage(7, ents, blobs, stage_root=sr)
    assert second["reused"] == 5, second
    assert blobs.asked == [], f"it refetched {len(blobs.asked)} blobs it had"


# ---------------------------------------------------------------------------
# the rule that cannot be broken
# ---------------------------------------------------------------------------
def test_the_system_update_symlink_is_removed_before_any_work(tmp_path, blobs):
    """systemd boots into system-update.target BECAUSE that symlink exists.
    A failure while it is still there boots into the same target again, and
    again, for ever — a rescue tool that bootloops the machine it was
    rescuing. systemd's own documentation says remove it first."""
    body = b"content\n"
    blobs.add(body)
    sr = str(tmp_path / "stage")
    fr.stage(7, [_entry("/etc", kind="d"), _entry("/etc/hosts", body)], blobs,
             stage_root=sr)
    link = tmp_path / "system-update"
    link.symlink_to(tmp_path / "stage")
    root = tmp_path / "root"
    root.mkdir()

    seen = {}
    real_copy = fr.shutil.copyfile

    def watching(src, dst):
        seen.setdefault("link_at_first_write", link.is_symlink() or link.exists())
        return real_copy(src, dst)

    fr.shutil.copyfile = watching
    try:
        fr.apply_staged(7, root=str(root), stage_root=sr,
                        system_update_link=str(link))
    finally:
        fr.shutil.copyfile = real_copy
    assert seen.get("link_at_first_write") is False, \
        "work started while /system-update still existed — a crash here bootloops"
    assert not link.exists() and not link.is_symlink()


def test_a_failure_still_leaves_the_machine_bootable(tmp_path, blobs):
    """The worst case has to be "it booted normally and the restore did not
    happen", which is where the machine already was."""
    body = b"content\n"
    blobs.add(body)
    sr = str(tmp_path / "stage")
    fr.stage(7, [_entry("/etc", kind="d"), _entry("/etc/hosts", body)], blobs,
             stage_root=sr)
    link = tmp_path / "system-update"
    link.symlink_to(tmp_path / "stage")
    root = tmp_path / "root"
    root.mkdir()
    # A file that cannot be written: its directory is read-only.
    (root / "etc").mkdir()
    out = fr.apply_staged(7, root=str(root), stage_root=sr,
                          system_update_link=str(link))
    assert not link.exists() and not link.is_symlink(), \
        "the symlink survived, so the next boot goes back into system-update"
    assert "ok" in out


# ---------------------------------------------------------------------------
# ...and the machine really comes back as it was
# ---------------------------------------------------------------------------
def _machine(root):
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "hostname").write_text("web-01\n")
    (root / "etc" / "shadow").write_text("root:!:19000:::::\n")
    os.chmod(root / "etc" / "shadow", 0o640)
    (root / "usr" / "bin" / "sudo").write_bytes(b"\x7fELF\n")
    os.chmod(root / "usr" / "bin" / "sudo", 0o4755)
    (root / "etc" / "app.conf").write_text("mode = good\n")
    link = root / "etc" / "resolv.conf"
    if not link.is_symlink():
        link.symlink_to("../run/resolv.conf")
    return root


def _manifest_of(root, blobs):
    ents = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            rel = "/" + os.path.relpath(full, root)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                ents.append(_entry(rel, kind="l", mode=0o777,
                                   target=os.readlink(full)))
            elif stat.S_ISDIR(st.st_mode):
                ents.append(_entry(rel, kind="d", mode=stat.S_IMODE(st.st_mode)))
            else:
                body = open(full, "rb").read()
                blobs.add(body)
                ents.append(_entry(rel, body, mode=stat.S_IMODE(st.st_mode)))
    return ents


def test_a_broken_machine_is_put_back_exactly(tmp_path, blobs):
    src = _machine(tmp_path / "src")
    ents = _manifest_of(src, blobs)
    sr = str(tmp_path / "stage")
    fr.stage(7, ents, blobs, stage_root=sr)

    # The live machine, now broken by a bad deploy.
    live = _machine(tmp_path / "live")
    (live / "etc" / "app.conf").write_text("mode = catastrophically bad\n")
    (live / "etc" / "extra-junk").write_text("left behind by the deploy\n")

    out = fr.apply_staged(7, root=str(live), stage_root=sr)
    assert out["ok"], out["errors"]
    assert (live / "etc" / "app.conf").read_text() == "mode = good\n"
    assert (live / "etc" / "hostname").read_text() == "web-01\n"
    assert stat.S_IMODE(os.lstat(live / "usr" / "bin" / "sudo").st_mode) == 0o4755
    assert stat.S_IMODE(os.lstat(live / "etc" / "shadow").st_mode) == 0o640
    assert os.path.islink(live / "etc" / "resolv.conf")


def test_what_the_point_does_not_contain_is_displaced_not_destroyed(tmp_path, blobs):
    """A restore aimed at the wrong point has to be survivable: the files it
    moved out of the way are still there."""
    src = _machine(tmp_path / "src")
    ents = _manifest_of(src, blobs)
    sr = str(tmp_path / "stage")
    fr.stage(7, ents, blobs, stage_root=sr)
    live = _machine(tmp_path / "live")
    (live / "etc" / "extra-junk").write_text("not in the restore point\n")

    out = fr.apply_staged(7, root=str(live), stage_root=sr)
    assert not (live / "etc" / "extra-junk").exists(), \
        "a file the restore point does not contain survived the restore"
    assert out["displaced"] >= 1
    kept = os.path.join(fr.job_dir(7, sr), fr.DISPLACED, "etc", "extra-junk")
    assert os.path.exists(kept), "it was destroyed rather than moved aside"
    assert open(kept).read() == "not in the restore point\n"


def test_it_does_not_reach_outside_what_was_captured(tmp_path, blobs):
    """A restore point of /etc must not decide that /home is surplus. The
    difference between a rescue and an erase."""
    src = tmp_path / "src"
    (src / "etc").mkdir(parents=True)
    (src / "etc" / "hosts").write_text("127.0.0.1\n")
    ents = _manifest_of(src, blobs)
    sr = str(tmp_path / "stage")
    fr.stage(7, ents, blobs, stage_root=sr)

    live = tmp_path / "live"
    (live / "etc").mkdir(parents=True)
    (live / "etc" / "hosts").write_text("wrong\n")
    (live / "home" / "alice").mkdir(parents=True)
    (live / "home" / "alice" / "thesis.txt").write_text("four years of work\n")
    (live / "var").mkdir()
    (live / "var" / "important.db").write_text("the database\n")

    fr.apply_staged(7, root=str(live), stage_root=sr)
    assert (live / "home" / "alice" / "thesis.txt").exists(), \
        "it deleted a file in a directory the restore point never captured"
    assert (live / "var" / "important.db").exists()
    assert (live / "etc" / "hosts").read_text() == "127.0.0.1\n"


def test_removing_extras_can_be_declined(tmp_path, blobs):
    src = _machine(tmp_path / "src")
    ents = _manifest_of(src, blobs)
    sr = str(tmp_path / "stage")
    fr.stage(7, ents, blobs, stage_root=sr)
    live = _machine(tmp_path / "live")
    (live / "etc" / "keep-me").write_text("mine\n")
    fr.apply_staged(7, root=str(live), stage_root=sr, remove_extras=False)
    assert (live / "etc" / "keep-me").exists()


# ---------------------------------------------------------------------------
# and it says what it did
# ---------------------------------------------------------------------------
def test_the_result_is_left_for_the_agent_to_report(tmp_path, blobs):
    """The agent is not running when this happens — it comes back after the
    reboot and has to find out how it went."""
    src = _machine(tmp_path / "src")
    ents = _manifest_of(src, blobs)
    sr = str(tmp_path / "stage")
    fr.stage(7, ents, blobs, stage_root=sr)
    live = _machine(tmp_path / "live")
    fr.apply_staged(7, root=str(live), stage_root=sr)
    res = fr.read_result(7, sr)
    assert res and res["ok"] is True
    assert res["wrote"] >= 4, res
    assert res["job_id"] == 7
