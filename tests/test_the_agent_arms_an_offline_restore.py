"""Arming the offline apply, and refusing when it cannot be armed.

The restore is staged while the host runs normally and applied from systemd's
offline-update target, where almost nothing from the filesystem is running.
Arming is the step between: write the apply, enable it for
system-update.target, and only then create /system-update — the switch that
makes the next boot go there.

THE REFUSAL IS THE POINT. If the offline apply cannot be installed, the
restore does not happen. Falling back to writing a filesystem under live
processes is exactly what this whole design exists to avoid, and the moment
the safe path turns out to be unavailable is the worst possible moment to take
that risk instead. A host that comes back unchanged is one an operator can try
again on; one that was half-rewritten while running may not come back.

ORDER MATTERS, and is asserted: /system-update is created LAST. systemd enters
system-update.target because it exists, so creating it before the apply script
and its unit are on disk means a boot into a target with nothing to run.
"""
import hashlib
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "host_agent"))

import flashback_restore as fr  # noqa: E402


def _staged(tmp_path, job=7):
    body = b"content\n"
    sha = hashlib.sha256(body).hexdigest()
    sr = str(tmp_path / "stage")
    fr.stage(job, [{"path": "/etc", "kind": "d", "mode": 0o755},
                   {"path": "/etc/hosts", "kind": "f", "mode": 0o644,
                    "sha256": sha, "size": len(body)}],
             lambda s: body, stage_root=sr)
    return sr


def _systemd_root(tmp_path):
    root = tmp_path / "root"
    (root / "etc" / "systemd" / "system").mkdir(parents=True)
    return str(root)


# ---------------------------------------------------------------------------
# it arms
# ---------------------------------------------------------------------------
def test_arming_installs_the_apply_its_unit_and_the_switch(tmp_path):
    sr = _staged(tmp_path)
    root = _systemd_root(tmp_path)
    ok, how = fr.arm(7, root=root, stage_root=sr)
    assert ok, how
    assert "systemd" in how
    assert os.path.exists(os.path.join(root, fr.APPLY_BIN.lstrip("/")))
    unit = os.path.join(root, fr.UNIT_PATH.lstrip("/"))
    assert os.path.exists(unit)
    text = open(unit).read()
    assert "WantedBy=system-update.target" in text
    assert "ConditionPathExists=/system-update" in text
    wants = os.path.join(root, "etc/systemd/system/system-update.target.wants",
                         os.path.basename(fr.UNIT_PATH))
    assert os.path.islink(wants), "the unit is not enabled for the target"
    assert os.path.islink(os.path.join(root, "system-update"))


def test_the_apply_script_is_executable_and_names_the_job(tmp_path):
    sr = _staged(tmp_path, job=42)
    root = _systemd_root(tmp_path)
    fr.arm(42, root=root, stage_root=sr)
    unit = open(os.path.join(root, fr.UNIT_PATH.lstrip("/"))).read()
    assert "42" in unit, unit
    binpath = os.path.join(root, fr.APPLY_BIN.lstrip("/"))
    assert os.access(binpath, os.X_OK), "the apply is not executable"


def test_the_switch_is_created_last(tmp_path):
    """systemd goes to system-update.target BECAUSE /system-update exists.
    Creating it before the apply is on disk means booting into a target with
    nothing to run — a reboot that achieves nothing and looks like a failure."""
    sr = _staged(tmp_path)
    root = _systemd_root(tmp_path)
    order = []
    real = fr.os.symlink

    def watching(src, dst):
        order.append(os.path.basename(dst))
        return real(src, dst)

    fr.os.symlink = watching
    try:
        fr.arm(7, root=root, stage_root=sr)
    finally:
        fr.os.symlink = real
    assert order[-1] == "system-update", order


def test_rearming_is_idempotent(tmp_path):
    sr = _staged(tmp_path)
    root = _systemd_root(tmp_path)
    assert fr.arm(7, root=root, stage_root=sr)[0]
    assert fr.arm(7, root=root, stage_root=sr)[0], "a second arm failed"


# ---------------------------------------------------------------------------
# ...and refuses rather than falling back
# ---------------------------------------------------------------------------
def test_it_refuses_on_a_host_without_systemd(tmp_path):
    """And says the restore has NOT been applied, because the alternative —
    writing it live — is the thing this design exists to avoid."""
    sr = _staged(tmp_path)
    root = str(tmp_path / "no-systemd")
    os.makedirs(root)
    ok, why = fr.arm(7, root=root, stage_root=sr)
    assert not ok
    assert "systemd" in why
    assert "NOT been applied" in why, why
    assert not os.path.exists(os.path.join(root, "system-update"))


def test_it_refuses_to_arm_something_that_is_not_staged(tmp_path):
    root = _systemd_root(tmp_path)
    ok, why = fr.arm(99, root=root, stage_root=str(tmp_path / "stage"))
    assert not ok
    assert "staged" in why
    assert not os.path.exists(os.path.join(root, "system-update"))


def test_a_failure_part_way_leaves_no_switch(tmp_path):
    """Half-armed must not boot into system-update.target: there would be
    nothing there to apply the restore, and the host reboots for nothing."""
    sr = _staged(tmp_path)
    root = _systemd_root(tmp_path)
    real = fr.os.symlink

    def fails(src, dst):
        if os.path.basename(dst) == "system-update":
            raise OSError("read-only filesystem")
        return real(src, dst)

    fr.os.symlink = fails
    try:
        ok, why = fr.arm(7, root=root, stage_root=sr)
    finally:
        fr.os.symlink = real
    assert not ok, why
    assert not os.path.exists(os.path.join(root, "system-update"))
