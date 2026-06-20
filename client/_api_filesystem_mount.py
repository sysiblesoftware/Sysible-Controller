"""FILE SYSTEM MANAGEMENT (mount/storage-level operations) - the
filesystem-type-aware half of File System Management, split out of
client/api.py to keep individual file sizes manageable. Imported via
`from client._api_filesystem_mount import *` at the bottom of
client/api.py.

File/directory-level operations (create/remove dirs, copy/move/
rename, ownership/permissions/ACLs, links, archive/compress) live in
the sibling client/_api_filesystem.py module instead - those are
universal coreutils operations with no filesystem-type assumption.
Everything here (resize, repair) does need to know whether it's
dealing with ext2/3/4, xfs, or btrfs, since each uses a different tool
with different mount-state requirements.
"""
import re
import shlex


def _validate_path(path: str, label: str = "Path") -> str:
    path = (path or "").strip()
    if not path:
        raise ValueError(f"{label} is required.")
    if "\x00" in path:
        raise ValueError(f"{label} contains an invalid character.")
    return path


def _validate_int_range(value, lo: int, hi: int, label: str) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number.")
    if not (lo <= n <= hi):
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return n


_SAFE_USERGROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _validate_user_or_group(name: str, label: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{label} is required.")
    if not _SAFE_USERGROUP_RE.match(name):
        raise ValueError(f"{label} doesn't look like a valid Linux user/group name.")
    return name


# --- mount / unmount -----------------------------------------------------

def cmd_mount_filesystem(device: str, mount_point: str, fstype: str = "", options: str = "") -> str:
    """Creates the mount point directory if it doesn't already exist,
    then mounts `device` there. `fstype`/`options` are optional - left
    blank, mount auto-detects the filesystem type and uses defaults."""
    device = _validate_path(device, "Device or source")
    mount_point = _validate_path(mount_point, "Mount point")
    q_dev = shlex.quote(device)
    q_mnt = shlex.quote(mount_point)

    parts = [f"mkdir -p {q_mnt}", "&&", "mount"]
    fstype = (fstype or "").strip()
    if fstype:
        parts += ["-t", shlex.quote(fstype)]
    options = (options or "").strip()
    if options:
        parts += ["-o", shlex.quote(options)]
    parts += [q_dev, q_mnt]
    return " ".join(parts) + " 2>&1"


def cmd_unmount_filesystem(target: str, force: bool = False) -> str:
    """`target` can be either the mount point or the underlying
    device."""
    target = _validate_path(target, "Mount point or device")
    flag = "-f " if force else ""
    return f"umount {flag}{shlex.quote(target)} 2>&1"


# --- resize ----------------------------------------------------------------

def cmd_resize_filesystem(target: str, new_size: str = "") -> str:
    """Grows (or, for ext, optionally shrinks) a filesystem in place.
    `target` can be a device (works for all three) or a mount point
    (required for xfs/btrfs, which can only be resized while mounted
    and can only grow, never shrink). `new_size` is passed straight
    through (e.g. "10G", "+5G") - leave it blank to grow to fill the
    whole underlying block device/partition.

    Filesystem type is auto-detected so the right tool gets called:
    resize2fs for ext2/3/4, xfs_growfs for xfs, `btrfs filesystem
    resize` for btrfs - anything else is reported as unsupported
    rather than silently failing.
    """
    target = _validate_path(target, "Device or mount point")
    q_target = shlex.quote(target)
    size = (new_size or "").strip()
    q_size = shlex.quote(size) if size else ""
    btrfs_size = q_size if size else "max"

    return (
        f"_fst=$(findmnt -no FSTYPE {q_target} 2>/dev/null || lsblk -no FSTYPE {q_target} 2>/dev/null); "
        f"_mnt=$(findmnt -no TARGET {q_target} 2>/dev/null); "
        'case "$_fst" in '
        f"ext2|ext3|ext4) command -v resize2fs >/dev/null 2>&1 && resize2fs {q_target} {q_size} 2>&1 "
        "|| echo 'resize2fs not installed on this host (package: e2fsprogs).' >&2;; "
        f'xfs) [ -n "$_mnt" ] && command -v xfs_growfs >/dev/null 2>&1 && xfs_growfs "$_mnt" 2>&1 '
        "|| echo 'xfs_growfs not installed, or target is not a mounted xfs filesystem (xfs can only grow while mounted).' >&2;; "
        f'btrfs) [ -n "$_mnt" ] && command -v btrfs >/dev/null 2>&1 && btrfs filesystem resize {btrfs_size} "$_mnt" 2>&1 '
        "|| echo 'btrfs-progs not installed, or target is not a mounted btrfs filesystem (btrfs can only resize while mounted).' >&2;; "
        '*) echo "Unsupported or undetected filesystem type (\\"$_fst\\")" - supported: ext2/ext3/ext4, xfs, btrfs." >&2; exit 1;; '
        "esac"
    )


# --- repair ------------------------------------------------------------------

def cmd_repair_filesystem(device: str, auto_yes: bool = True) -> str:
    """Runs fsck against `device`. Refuses outright if it's currently
    mounted - fsck on a mounted filesystem can corrupt it further;
    unmount it first (Unmount Filesystem above)."""
    device = _validate_path(device, "Device")
    q_dev = shlex.quote(device)
    flag = "-y" if auto_yes else "-n"
    return (
        f"if findmnt -no TARGET {q_dev} >/dev/null 2>&1; then "
        "echo 'Refusing to fsck - target is currently mounted. Unmount it first.' >&2; exit 1; fi; "
        f"fsck {flag} {q_dev} 2>&1"
    )


# --- /etc/fstab --------------------------------------------------------------

def cmd_show_fstab() -> str:
    return "cat /etc/fstab 2>&1"


def cmd_add_fstab_entry(
    device: str, mount_point: str, fstype: str,
    options: str = "defaults", dump: int = 0, pass_num: int = 0,
) -> str:
    """Appends one line to /etc/fstab, after backing it up to a
    timestamped copy and refusing if an entry for that mount point
    already exists (remove it first via Remove fstab Entry if you
    want to replace it)."""
    device = _validate_path(device, "Device or source")
    mount_point = _validate_path(mount_point, "Mount point")
    fstype = (fstype or "").strip()
    if not fstype:
        raise ValueError("Filesystem type is required (e.g. ext4, xfs, nfs).")
    options = (options or "").strip() or "defaults"
    dump = _validate_int_range(dump, 0, 1, "Dump field")
    pass_num = _validate_int_range(pass_num, 0, 9, "Pass field")

    line = f"{device}\t{mount_point}\t{fstype}\t{options}\t{dump}\t{pass_num}"
    q_line = shlex.quote(line)
    q_mnt = shlex.quote(mount_point)

    return (
        f"if grep -qF {q_mnt} /etc/fstab 2>/dev/null; then "
        "echo 'An /etc/fstab entry for that mount point already exists - remove it first if you want to replace it.' >&2; exit 1; fi; "
        f"cp /etc/fstab /etc/fstab.bak.$(date +%s) "
        f"&& echo {q_line} >> /etc/fstab "
        f"&& echo 'Added to /etc/fstab:' && grep -F {q_line} /etc/fstab"
    )


def cmd_remove_fstab_entry(mount_point: str) -> str:
    """Removes the line whose mount-point field (2nd column) exactly
    matches `mount_point` - by field, not substring, so removing
    "/data" won't also match "/data2". Backs up /etc/fstab first."""
    mount_point = _validate_path(mount_point, "Mount point")
    q_mnt = shlex.quote(mount_point)
    return (
        f"cp /etc/fstab /etc/fstab.bak.$(date +%s) && "
        f"awk -v mnt={q_mnt} '$2 != mnt' /etc/fstab > /tmp/fstab.new.$$ "
        f"&& mv /tmp/fstab.new.$$ /etc/fstab && echo 'Removed any /etc/fstab entry for' {q_mnt}"
    )


# --- quotas --------------------------------------------------------------------

def cmd_enable_quotas(mount_point: str) -> str:
    """quotacheck + quotaon for a filesystem already mounted with
    usrquota/grpquota (set that in /etc/fstab and remount first)."""
    mount_point = _validate_path(mount_point, "Mount point")
    q_mnt = shlex.quote(mount_point)
    return (
        "if ! command -v quotacheck >/dev/null 2>&1 || ! command -v quotaon >/dev/null 2>&1; then "
        "echo 'quotacheck/quotaon not installed on this host (package: quota).' >&2; exit 1; fi; "
        f"quotacheck -ugm {q_mnt} 2>&1 && quotaon {q_mnt} 2>&1"
    )


def cmd_show_quotas(mount_point: str = "") -> str:
    """repquota for one filesystem, or every quota-enabled filesystem
    (`repquota -a`) if `mount_point` is left blank."""
    mount_point = (mount_point or "").strip()
    target = shlex.quote(mount_point) if mount_point else "-a"
    return (
        "if ! command -v repquota >/dev/null 2>&1; then "
        "echo 'repquota is not installed on this host (package: quota).' >&2; exit 1; fi; "
        f"repquota {target} 2>&1"
    )


def cmd_set_user_quota(
    username: str, mount_point: str,
    block_soft: int, block_hard: int, inode_soft: int = 0, inode_hard: int = 0,
) -> str:
    """Block limits in 1K blocks, inode limits in file counts - 0
    means unlimited for either pair. Requires the target filesystem
    already mounted with usrquota and quotacheck/quotaon already run
    on it (Enable Quotas above)."""
    username = _validate_user_or_group(username, "Username")
    mount_point = _validate_path(mount_point, "Mount point")
    block_soft = _validate_int_range(block_soft, 0, 2_147_483_647, "Block soft limit")
    block_hard = _validate_int_range(block_hard, 0, 2_147_483_647, "Block hard limit")
    inode_soft = _validate_int_range(inode_soft, 0, 2_147_483_647, "Inode soft limit")
    inode_hard = _validate_int_range(inode_hard, 0, 2_147_483_647, "Inode hard limit")
    q_user = shlex.quote(username)
    q_mnt = shlex.quote(mount_point)
    return (
        "if ! command -v setquota >/dev/null 2>&1; then "
        "echo 'setquota is not installed on this host (package: quota).' >&2; exit 1; fi; "
        f"setquota -u {q_user} {block_soft} {block_hard} {inode_soft} {inode_hard} 0 {q_mnt} 2>&1 "
        "&& echo 'Quota updated.'"
    )
