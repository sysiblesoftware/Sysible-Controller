"""Backup & Recovery command builders (dual-host: run via agent or SSH).

Plain POSIX sh, shlex.quote() on anything interpolated, an explicit
message instead of silent empty output, and a real exit code so the GUI
can colour the result success/failure.
"""
import re
import shlex

_CRON_RE = re.compile(r"^[\d*/,\- ]+$")

# Control characters (incl. newline, CR, NUL) never appear in a legitimate path.
# Rejecting them is what keeps an embedded newline from breaking out of the
# quoted heredoc that writes the backup helper script — a value containing
# "\nSYS_EOF\n<shell>" would otherwise terminate the here-document early and run
# the trailing lines as root (command injection).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(value: str, label: str) -> None:
    if _CONTROL_RE.search(value or ""):
        raise ValueError(f"{label} must not contain control characters or newlines.")


def cmd_backup_files(source: str, dest_dir: str) -> str:
    """tar.gz `source` into a timestamped archive under `dest_dir`."""
    source = (source or "").strip()
    dest_dir = (dest_dir or "").strip()
    if not source:
        raise ValueError("Source path is required.")
    if not dest_dir:
        raise ValueError("Destination directory is required.")
    qs, qd = shlex.quote(source), shlex.quote(dest_dir)
    return (
        f"src={qs}; dest={qd}; "
        'if [ ! -e "$src" ]; then echo "Source does not exist: $src" >&2; exit 1; fi; '
        'mkdir -p "$dest" || exit 1; '
        'ts=$(date +%Y%m%d-%H%M%S); base=$(basename "$src"); '
        'archive="$dest/backup-$base-$ts.tar.gz"; '
        'tar czf "$archive" -C "$(dirname "$src")" "$base" && '
        'echo "Backup created: $archive" && ls -lh "$archive"'
    )


def cmd_restore_files(archive: str, dest_dir: str) -> str:
    """Extract a .tar.gz archive into `dest_dir`."""
    archive = (archive or "").strip()
    dest_dir = (dest_dir or "").strip()
    if not archive:
        raise ValueError("Archive path is required.")
    if not dest_dir:
        raise ValueError("Restore-into directory is required.")
    qa, qd = shlex.quote(archive), shlex.quote(dest_dir)
    return (
        f"a={qa}; d={qd}; "
        'if [ ! -f "$a" ]; then echo "Archive not found: $a" >&2; exit 1; fi; '
        'mkdir -p "$d" || exit 1; '
        'tar xzf "$a" -C "$d" && echo "Restored $a into $d."'
    )


def cmd_verify_backup(archive: str) -> str:
    """Check a .tar.gz archive's gzip integrity and list its entry count."""
    archive = (archive or "").strip()
    if not archive:
        raise ValueError("Archive path is required.")
    qa = shlex.quote(archive)
    return (
        f"a={qa}; "
        'if [ ! -f "$a" ]; then echo "Archive not found: $a" >&2; exit 1; fi; '
        'if gzip -t "$a" 2>/dev/null; then echo "Gzip integrity: OK"; '
        'else echo "Gzip integrity: FAILED (archive is corrupt)" >&2; exit 1; fi; '
        'n=$(tar tzf "$a" 2>/dev/null | wc -l | tr -d " "); '
        'echo "Archive contains $n entries and is readable end-to-end."'
    )


def cmd_configure_backup_schedule(source: str, dest_dir: str, cron_expr: str) -> str:
    """Install a cron.d job (+ helper script) that tar.gz-backs up `source`
    into `dest_dir` on the given 5-field cron schedule."""
    source = (source or "").strip()
    dest_dir = (dest_dir or "").strip()
    cron_expr = (cron_expr or "").strip()
    if not source or not dest_dir:
        raise ValueError("Source path and destination directory are required.")
    # Critical: these are embedded in the heredoc body below. A newline could
    # otherwise smuggle the heredoc terminator and inject root-run shell.
    _reject_control_chars(source, "Source path")
    _reject_control_chars(dest_dir, "Destination directory")
    if len(cron_expr.split()) != 5 or not _CRON_RE.match(cron_expr):
        raise ValueError("Schedule must be 5 cron fields, e.g. '0 2 * * *'.")
    qs, qd = shlex.quote(source), shlex.quote(dest_dir)
    script = (
        "#!/bin/sh\n"
        f"src={qs}\n"
        f"dest={qd}\n"
        'mkdir -p "$dest"\n'
        'tar czf "$dest/backup-$(basename "$src")-$(date +%Y%m%d-%H%M%S).tar.gz" '
        '-C "$(dirname "$src")" "$(basename "$src")"\n'
    )
    return (
        "cat > /usr/local/sbin/sysible-backup.sh <<'SYS_EOF'\n"
        f"{script}"
        "SYS_EOF\n"
        "chmod +x /usr/local/sbin/sysible-backup.sh && "
        f"printf '%s root /usr/local/sbin/sysible-backup.sh\\n' {shlex.quote(cron_expr)} "
        "> /etc/cron.d/sysible-backup && "
        # /etc/cron.d is only honored when a cron daemon is installed AND its service
        # is running. Debian/Ubuntu ship+enable cron by default, but minimal RHEL/
        # Rocky/Alma, Amazon Linux 2, openSUSE/SLES and Arch frequently have no cron
        # package at all - the drop-in would sit there inert and the backup would
        # silently never fire. Ensure a daemon is present and enabled, installing
        # cronie/cron via the host package manager if needed (service name is crond
        # on RHEL/SUSE, cron on Debian).
        "if ! command -v crond >/dev/null 2>&1 && ! command -v cron >/dev/null 2>&1; then "
        "if command -v dnf >/dev/null 2>&1; then dnf install -y cronie >/dev/null 2>&1; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y cronie >/dev/null 2>&1; "
        "elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install cron >/dev/null 2>&1; "
        "elif command -v apt-get >/dev/null 2>&1; then DEBIAN_FRONTEND=noninteractive apt-get install -y cron >/dev/null 2>&1; "
        "elif command -v pacman >/dev/null 2>&1; then pacman -Sy --needed --noconfirm cronie >/dev/null 2>&1; fi; fi; "
        "systemctl enable --now crond 2>/dev/null || systemctl enable --now cron 2>/dev/null || "
        "systemctl enable --now cronie 2>/dev/null || true; "
        "if ! command -v crond >/dev/null 2>&1 && ! command -v cron >/dev/null 2>&1; then "
        "echo 'WARNING: no cron daemon is installed - the schedule was written to "
        "/etc/cron.d/sysible-backup but will not run until a cron service (cronie/cron) "
        "is installed and started.' >&2; fi; "
        f"printf 'Scheduled backup of %s to %s on cron: %s\\n' {qs} {qd} {shlex.quote(cron_expr)}"
    )


def cmd_create_snapshot(vg: str, lv: str, snap_name: str, size: str) -> str:
    """LVM snapshot of /dev/<vg>/<lv>."""
    vg = (vg or "").strip()
    lv = (lv or "").strip()
    snap_name = (snap_name or "").strip()
    size = (size or "").strip()
    if not (vg and lv and snap_name and size):
        raise ValueError("Volume group, logical volume, snapshot name, and size are all required.")
    qsnap, qsize = shlex.quote(snap_name), shlex.quote(size)
    origin = shlex.quote(f"/dev/{vg}/{lv}")
    return (
        "if ! command -v lvcreate >/dev/null 2>&1; then "
        'echo "LVM tools (lvcreate) not installed on this host." >&2; exit 1; fi; '
        f"lvcreate -s -n {qsnap} -L {qsize} {origin} && "
        f"printf 'Snapshot %s created from %s/%s.\\n' {qsnap} {shlex.quote(vg)} {shlex.quote(lv)}"
    )


def cmd_restore_snapshot(vg: str, snap_name: str) -> str:
    """Merge an LVM snapshot back into its origin (applies on next
    deactivation/reboot of the origin)."""
    vg = (vg or "").strip()
    snap_name = (snap_name or "").strip()
    if not (vg and snap_name):
        raise ValueError("Volume group and snapshot name are required.")
    snap = shlex.quote(f"/dev/{vg}/{snap_name}")
    return (
        "if ! command -v lvconvert >/dev/null 2>&1; then "
        'echo "LVM tools (lvconvert) not installed on this host." >&2; exit 1; fi; '
        f"lvconvert --merge {snap} && "
        "echo 'Snapshot scheduled to merge into its origin (completes when the "
        "origin volume is next deactivated, e.g. at reboot).'"
    )


def cmd_recover_deleted(device: str) -> str:
    """Report which recovery tools are present and the safe procedure -
    deliberately does not auto-run recovery (it must happen with the
    filesystem unmounted to avoid overwriting freed blocks)."""
    device = (device or "").strip()
    if not device:
        raise ValueError("Device or filesystem path is required (e.g. /dev/sdb1).")
    qd = shlex.quote(device)
    return (
        f"dev={qd}; echo \"Target: $dev\"; echo; "
        'if mount | grep -q "$dev"; then '
        'echo "WARNING: $dev appears mounted. Unmount it before attempting recovery "'
        '"- writing to a mounted filesystem overwrites the freed blocks you are trying to recover."; echo; fi; '
        'echo "Available recovery tools:"; '
        'for t in extundelete testdisk photorec debugfs; do '
        'if command -v "$t" >/dev/null 2>&1; then echo "  $t: present"; else echo "  $t: not installed"; fi; done; '
        'echo; echo "Procedure: unmount the filesystem, then (ext3/4) run "'
        '"extundelete $dev --restore-all, or use testdisk/photorec for other filesystems. "'
        '"Install the tool first via Host Software Management if it is missing."'
    )


def cmd_test_disaster_recovery(dest_dir: str) -> str:
    """Read-only DR drill: confirm backups exist in `dest_dir`, report the
    newest one's age, and verify its integrity - without changing anything."""
    dest_dir = (dest_dir or "").strip()
    if not dest_dir:
        raise ValueError("Backup directory is required.")
    qd = shlex.quote(dest_dir)
    return (
        f"d={qd}; "
        'if [ ! -d "$d" ]; then echo "Backup directory not found: $d" >&2; exit 1; fi; '
        'newest=$(ls -1t "$d"/*.tar.gz 2>/dev/null | head -n 1); '
        'count=$(ls -1 "$d"/*.tar.gz 2>/dev/null | wc -l | tr -d " "); '
        'if [ -z "$newest" ]; then echo "DR TEST FAILED: no .tar.gz backups found in $d." >&2; exit 1; fi; '
        'echo "Backups found: $count"; echo "Newest: $newest"; '
        'echo "Last modified: $(date -r "$newest" 2>/dev/null || stat -c %y "$newest" 2>/dev/null)"; '
        'if gzip -t "$newest" 2>/dev/null; then echo "Integrity of newest backup: OK"; '
        'echo "DR TEST PASSED: a recent, readable backup is present and verifiable."; '
        'else echo "DR TEST FAILED: newest backup is corrupt." >&2; exit 1; fi'
    )
