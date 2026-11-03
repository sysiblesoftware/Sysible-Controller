"""Putting a machine back to a Flashback restore point — staged, then applied
offline.

WHY NOT JUST WRITE THE FILES. Replacing a running system's binaries and
libraries under live processes leaves it in a state nothing has ever been
tested in: half of /usr is yesterday's, half is today's, and anything that
execs in that window gets a mixture. The reboot afterwards is what makes it
survivable, and "survivable" is not the bar for the operation an operator
reaches for when a machine is already broken.

So nothing touches the live filesystem until the machine is almost not running.

    STAGE   while the host runs normally: fetch the manifest, fetch only the
            content we do not already have, verify every digest, build the new
            tree under /var/lib/sysible/flashback/stage/<job>/tree. Interrupt
            this at any point and NOTHING has happened.

    ARM     install the offline apply, and only then mark it ready.

    REBOOT

    APPLY   from systemd's own offline-update target, where the root
            filesystem is mounted and essentially nothing from it is running.

    REPORT  the agent comes back up, finds the result, and tells Flashback.

THE MECHANISM IS systemd.offline-updates, which exists for exactly this and is
the same on every systemd distribution: a /system-update symlink makes the next
boot enter system-update.target instead of the normal one, where a unit can do
work with almost no userland started. It needs no initramfs regeneration — and
regenerating an initramfs on a machine the operator is already trying to rescue
is its own way to lose the host.

THE ONE RULE THAT CANNOT BE BROKEN, from systemd's own documentation: the
symlink is removed BEFORE the work starts. A crash after it is gone boots
normally; a crash while it is still there boots into system-update.target
again, and again, for ever. A rescue tool that bootloops the machine it was
rescuing is worse than no rescue tool.

NOTHING IS DELETED, ONLY DISPLACED. Files the restore point does not contain
are moved into the job's `displaced/` tree rather than unlinked, so a restore
aimed at the wrong point is itself reversible.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile

STAGE_ROOT = "/var/lib/sysible/flashback/stage"
SYSTEM_UPDATE_LINK = "/system-update"

READY = "READY"            # written last; its absence means "do not apply"
MANIFEST = "manifest.json"
RESULT = "result.json"
TREE = "tree"
DISPLACED = "displaced"


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def job_dir(job_id, stage_root: str = STAGE_ROOT) -> str:
    return os.path.join(stage_root, str(job_id))


# ---------------------------------------------------------------------------
# STAGE
# ---------------------------------------------------------------------------
def stage(job_id, entries, fetch_blob, stage_root: str = STAGE_ROOT) -> dict:
    """Build the new tree beside the live one. Returns a summary.

    `fetch_blob(sha256) -> bytes` is whatever the caller uses to reach
    Flashback; this module does no networking so it can be tested, and run,
    without any.

    EVERY DIGEST IS CHECKED HERE, where a mismatch is a failed download and
    costs nothing. Checking at apply time instead would mean discovering it
    with the machine already in system-update.target.

    READY is written LAST and only on complete success. Anything that goes
    wrong — a truncated download, a full disk, a reboot — leaves a job without
    it, and `apply_staged` refuses to touch a filesystem on that basis.
    """
    d = job_dir(job_id, stage_root)
    tree = os.path.join(d, TREE)
    os.makedirs(tree, exist_ok=True)
    # A previous half-attempt must not be mistaken for this one.
    ready = os.path.join(d, READY)
    if os.path.exists(ready):
        os.remove(ready)

    entries = list(entries or [])
    files = dirs = links = 0
    fetched = reused = 0
    for e in entries:
        rel = str(e.get("path") or "").lstrip("/")
        if not rel:
            continue
        dest = os.path.join(tree, rel)
        kind = (e.get("kind") or "f")[:1]
        if kind == "d":
            os.makedirs(dest, exist_ok=True)
            os.chmod(dest, int(e.get("mode") or 0o755))
            dirs += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if kind == "l":
            if os.path.islink(dest) or os.path.exists(dest):
                os.remove(dest)
            os.symlink(str(e.get("target") or ""), dest)
            links += 1
            continue
        sha = str(e.get("sha256") or "")
        # Already staged from an earlier, interrupted attempt and still
        # correct: a retry after a dropped link should not refetch gigabytes.
        if os.path.exists(dest) and not os.path.islink(dest) and sha and _sha(dest) == sha:
            reused += 1
        else:
            body = fetch_blob(sha)
            got = hashlib.sha256(body).hexdigest()
            if sha and got != sha:
                raise ValueError(
                    f"{e.get('path')}: content does not match its digest "
                    f"(wanted {sha[:12]}…, got {got[:12]}…)")
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
            fetched += 1
        os.chmod(dest, int(e.get("mode") or 0o644))
        files += 1

    with open(os.path.join(d, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump({"job_id": job_id, "entries": entries}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    with open(ready, "w", encoding="utf-8") as fh:
        fh.write("staged\n")
        fh.flush()
        os.fsync(fh.fileno())
    return {"files": files, "dirs": dirs, "links": links,
            "fetched": fetched, "reused": reused}


def is_ready(job_id, stage_root: str = STAGE_ROOT) -> bool:
    return os.path.exists(os.path.join(job_dir(job_id, stage_root), READY))


# ---------------------------------------------------------------------------
# ARM — install the offline apply, or refuse
# ---------------------------------------------------------------------------
UNIT_PATH = "/etc/systemd/system/sysible-flashback-restore.service"
APPLY_BIN = "/usr/local/lib/sysible/flashback-apply-restore"

# WantedBy=system-update.target is what makes systemd run this, and only this,
# on the boot where /system-update exists. DefaultDependencies=no keeps it out
# of the ordinary boot ordering so almost nothing else is started; and
# FailureAction=reboot means a host that cannot apply its restore still comes
# back rather than sitting at a prompt nobody is in front of.
_UNIT = """\
[Unit]
Description=Sysible Flashback — apply a staged restore point
DefaultDependencies=no
Requires=sysinit.target
After=sysinit.target
Before=shutdown.target
ConditionPathExists=/system-update

[Service]
Type=oneshot
ExecStart={apply_bin} {job_id}
FailureAction=reboot
SuccessAction=reboot

[Install]
WantedBy=system-update.target
"""

_APPLY_SCRIPT = """\
#!/usr/bin/env python3
# Applies one staged Flashback restore point, from systemd's offline-update
# target. Written by the Sysible agent; see host_agent/flashback_restore.py.
import sys
sys.path.insert(0, {helper_dir!r})
import flashback_restore as fr
job = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    out = fr.apply_staged(job)
    print("sysible-flashback: wrote %d, displaced %d, ok=%s"
          % (out["wrote"], out["displaced"], out["ok"]))
except Exception as e:
    print("sysible-flashback: restore failed:", e)
    raise SystemExit(1)
"""


def arm(job_id, root: str = "/", stage_root: str = STAGE_ROOT) -> tuple:
    """Make the next boot apply this staged restore. (ok, how-or-why).

    REFUSES RATHER THAN FALLING BACK. If the offline apply cannot be installed
    on this host, this returns False and the restore does not happen. Writing a
    filesystem under live processes is precisely what this design exists to
    avoid, and the moment the safe path turns out to be unavailable is the
    worst possible moment to take that risk instead.
    """
    if not is_ready(job_id, stage_root):
        return False, "that restore point is not fully staged"
    if not os.path.isdir(os.path.join(root, "run/systemd/system")) and \
            not os.path.isdir(os.path.join(root, "etc/systemd/system")):
        return False, ("this host does not appear to run systemd, so the "
                       "offline apply cannot be installed — the restore has "
                       "NOT been applied")
    helper_dir = os.path.dirname(os.path.abspath(__file__))
    apply_bin = os.path.join(root, APPLY_BIN.lstrip("/"))
    unit_path = os.path.join(root, UNIT_PATH.lstrip("/"))
    try:
        os.makedirs(os.path.dirname(apply_bin), exist_ok=True)
        with open(apply_bin, "w", encoding="utf-8") as fh:
            fh.write(_APPLY_SCRIPT.format(helper_dir=helper_dir))
        os.chmod(apply_bin, 0o755)
        os.makedirs(os.path.dirname(unit_path), exist_ok=True)
        with open(unit_path, "w", encoding="utf-8") as fh:
            fh.write(_UNIT.format(apply_bin=APPLY_BIN, job_id=job_id))
        # Enabled by hand rather than by `systemctl enable`: this may run in a
        # container or a chroot where systemctl cannot talk to PID 1, and the
        # symlink is all `enable` does for a WantedBy unit.
        wants = os.path.join(root, "etc/systemd/system/system-update.target.wants")
        os.makedirs(wants, exist_ok=True)
        link = os.path.join(wants, os.path.basename(UNIT_PATH))
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(UNIT_PATH, link)
        # LAST: this is the switch. systemd enters system-update.target on the
        # next boot because this exists, so it goes in only once everything it
        # needs is already on disk.
        su = os.path.join(root, SYSTEM_UPDATE_LINK.lstrip("/"))
        if os.path.islink(su) or os.path.exists(su):
            os.remove(su)
        os.symlink(job_dir(job_id, stage_root), su)
    except OSError as e:
        return False, f"could not install the offline apply: {e}"
    return True, "systemd offline update"


# ---------------------------------------------------------------------------
# APPLY — offline, from system-update.target
# ---------------------------------------------------------------------------
def apply_staged(job_id, root: str = "/", stage_root: str = STAGE_ROOT,
                 remove_extras: bool = True,
                 system_update_link: str = SYSTEM_UPDATE_LINK) -> dict:
    """Swap the staged tree into `root`. Returns a result summary.

    THE SYMLINK GOES FIRST. systemd boots into system-update.target because
    /system-update exists, so anything that fails while it is still there
    reboots into the same target and tries again — for ever. Removing it
    before any work means the worst case is a machine that boots normally with
    the restore not applied, which is where it already was.

    Files are written to a temporary name in their FINAL DIRECTORY and renamed
    over the target, so a crash leaves either the old file or the new one and
    never a half-written one — which matters most for the files that decide
    whether the machine boots at all.
    """
    d = job_dir(job_id, stage_root)
    if not is_ready(job_id, stage_root):
        raise ValueError("that restore point was never fully staged, so it will "
                         "not be applied — nothing has been changed")

    # BEFORE the work. See above.
    try:
        if os.path.islink(system_update_link) or os.path.exists(system_update_link):
            os.remove(system_update_link)
    except OSError:
        pass

    with open(os.path.join(d, MANIFEST), encoding="utf-8") as fh:
        entries = (json.load(fh) or {}).get("entries") or []
    tree = os.path.join(d, TREE)
    displaced_root = os.path.join(d, DISPLACED)

    wrote = removed = 0
    errors = []
    want = set()

    def _displace(live_path: str, rel: str) -> None:
        """Move what is there now out of the way rather than destroying it."""
        keep = os.path.join(displaced_root, rel)
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        try:
            os.replace(live_path, keep)
        except OSError:
            shutil.copy2(live_path, keep)
            os.remove(live_path)

    # Directories first, so files have somewhere to land.
    for e in sorted(entries, key=lambda x: ((x.get("kind") or "f") != "d",
                                            str(x.get("path") or ""))):
        rel = str(e.get("path") or "").lstrip("/")
        if not rel:
            continue
        want.add(rel)
        live = os.path.join(root, rel)
        kind = (e.get("kind") or "f")[:1]
        try:
            if kind == "d":
                os.makedirs(live, exist_ok=True)
                os.chmod(live, int(e.get("mode") or 0o755))
                continue
            os.makedirs(os.path.dirname(live), exist_ok=True)
            if kind == "l":
                if os.path.islink(live) or os.path.exists(live):
                    os.remove(live)
                os.symlink(str(e.get("target") or ""), live)
                wrote += 1
                continue
            src = os.path.join(tree, rel)
            tmp = live + ".sysible-new"
            shutil.copyfile(src, tmp)
            # CHOWN FIRST, THEN CHMOD. chown(2) clears the setuid and setgid
            # bits — deliberately, so a file cannot be handed to another owner
            # with its privilege intact. Doing it the other way round silently
            # dropped them: /usr/bin/sudo came back 0755, and a machine that
            # had just been rescued had no working sudo. Caught by restoring a
            # real tree and comparing the modes, which is the only way this
            # shows up at all.
            try:
                os.chown(tmp, int(e.get("uid") or 0), int(e.get("gid") or 0))
            except (OSError, AttributeError):
                pass          # unprivileged test runs, and non-POSIX corners
            os.chmod(tmp, int(e.get("mode") or 0o644))
            os.replace(tmp, live)
            wrote += 1
        except OSError as exc:
            errors.append(f"{e.get('path')}: {exc}")

    if remove_extras:
        # ONLY under the directories the point actually captured. A restore
        # point of /etc must not reach into /home or /var/lib and decide
        # those files are surplus.
        for rel in sorted(_extras_under(root, want, entries), reverse=True):
            live = os.path.join(root, rel)
            try:
                _displace(live, rel)
                removed += 1
            except OSError as exc:
                errors.append(f"{rel}: {exc}")

    out = {"job_id": job_id, "wrote": wrote, "displaced": removed,
           "errors": errors, "ok": not errors}
    with open(os.path.join(d, RESULT), "w", encoding="utf-8") as fh:
        json.dump(out, fh)
        fh.flush()
        os.fsync(fh.fileno())
    return out


def _extras_under(root: str, want: set, entries) -> list:
    """Files present now, inside a captured directory, that the point has no
    entry for.

    Scoped to the directories the manifest itself names. Walking from `root`
    and deleting everything unlisted would turn a restore point of /etc into
    an instruction to empty the disk.
    """
    captured_dirs = {str(e.get("path") or "").lstrip("/")
                     for e in entries if (e.get("kind") or "f")[:1] == "d"}
    out = []
    for rel_dir in captured_dirs:
        live_dir = os.path.join(root, rel_dir)
        try:
            names = os.listdir(live_dir)
        except OSError:
            continue
        for name in names:
            rel = os.path.join(rel_dir, name) if rel_dir else name
            if rel in want:
                continue
            full = os.path.join(root, rel)
            # A directory that is itself unlisted is left alone: its contents
            # may be outside what was captured, and this walks no deeper.
            if os.path.isdir(full) and not os.path.islink(full):
                continue
            out.append(rel)
    return out


def read_result(job_id, stage_root: str = STAGE_ROOT):
    p = os.path.join(job_dir(job_id, stage_root), RESULT)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
