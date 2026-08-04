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


from client._validators import validate_int_range as _validate_int_range
from client._validators import validate_user_or_group as _validate_user_or_group


def _validate_path(path: str, label: str = "Path") -> str:
    path = (path or "").strip()
    if not path:
        raise ValueError(f"{label} is required.")
    # Reject NUL and CR/LF: a mount point / device flows into an /etc/fstab line
    # (written with printf), so an embedded newline would append an extra fstab
    # entry — a distinct filesystem mounted at boot the operator didn't intend.
    if "\x00" in path or "\n" in path or "\r" in path:
        raise ValueError(f"{label} contains an invalid character.")
    return path


def _reject_critical_mount(path: str, allow_critical: bool, label: str = "Mount") -> None:
    """Refuses unmounting / fstab-removing a system-critical mount (see
    client/system_paths) unless `allow_critical` is set - which the front ends
    only pass after a superuser confirms the warning. A hard block for
    sysadmins (they can never set the flag)."""
    if allow_critical:
        return
    from client import system_paths
    reason = system_paths.system_critical_reason(path)
    if reason:
        raise ValueError(
            f"{label}: {reason} Only a superuser can do this, after confirming the warning."
        )



_SAFE_USERGROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")



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


def cmd_unmount_filesystem(target: str, force: bool = False,
                           allow_critical: bool = False) -> str:
    """`target` can be either the mount point or the underlying
    device. Refuses to unmount a system-critical mount (see
    client/system_paths) unless `allow_critical` is set - which the UIs only
    pass after a superuser confirms the warning."""
    target = _validate_path(target, "Mount point or device")
    _reject_critical_mount(target, allow_critical, "Unmount target")
    flag = "-f " if force else ""
    return f"umount {flag}{shlex.quote(target)} 2>&1"


_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
# Exclude CR/LF as well as NUL and '/': a share name flows into a persisted /etc/fstab
# line, where an embedded newline would append an attacker-controlled second entry.
_SHARE_RE = re.compile(r"^[^\x00/\r\n]+$")
# A filesystem type is a single bare token (ext4, xfs, nfs4, cifs, vfat, …).
_FSTYPE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_mount_options(options: str) -> str:
    """Vet mount options before they're inlined into the mount command AND written
    to /etc/fstab. shlex.quote already blocks shell injection in the mount command,
    but an fstab line is whitespace-delimited and newline-terminated: a space would
    split the option into extra fstab fields and a NEWLINE would append an ATTACKER-
    CONTROLLED second fstab entry (persisted, run at every boot as root). Mount
    options are always comma-separated tokens with no whitespace, so reject any."""
    opts = (options or "").strip()
    if not opts:
        return opts
    for ch in opts:
        if ch.isspace() or ch == "\x00":
            raise ValueError("Mount options must be a comma-separated list with no "
                             "spaces or newlines (e.g. rw,noexec,vers=3).")
    return opts


def cmd_mount_nfs(server: str, export_path: str, mount_point: str,
                  options: str = "", persist: bool = False) -> str:
    """Mount an NFS export (server:/export) at `mount_point`, optionally
    persisting it to /etc/fstab."""
    server = (server or "").strip()
    export_path = (export_path or "").strip()
    mount_point = _validate_path(mount_point, "Mount point")
    if not _HOST_RE.match(server):
        raise ValueError("NFS server must be a hostname or IP.")
    if not export_path.startswith("/"):
        raise ValueError("NFS export path should start with '/' (e.g. /exports/data).")
    # export_path is persisted into an /etc/fstab line; reject CR/LF/NUL so it can't
    # append a second, attacker-controlled fstab entry.
    if "\x00" in export_path or "\n" in export_path or "\r" in export_path:
        raise ValueError("NFS export path contains an invalid character.")
    opts = _validate_mount_options(options) or "defaults"
    src = f"{server}:{export_path}"
    q_src, q_mnt, q_opts = shlex.quote(src), shlex.quote(mount_point), shlex.quote(opts)
    cmd = (
        "if ! command -v mount.nfs >/dev/null 2>&1 && ! ls /sbin/mount.nfs* >/dev/null 2>&1; then "
        "echo 'NFS client not installed - install nfs-common (Debian/Ubuntu) or nfs-utils (RHEL/SUSE) first.' >&2; exit 1; fi; "
        f"mkdir -p {q_mnt} && mount -t nfs -o {q_opts} {q_src} {q_mnt} && printf 'Mounted %s at %s.\\n' {q_src} {q_mnt}"
    )
    if persist:
        fstab_line = f"{src} {mount_point} nfs {opts} 0 0"
        cmd += (
            f" && {{ grep -qsF {shlex.quote(mount_point + ' nfs')} /etc/fstab "
            f"|| printf '%s\\n' {shlex.quote(fstab_line)} >> /etc/fstab; echo 'Persisted to /etc/fstab.'; }}"
        )
    return cmd


def cmd_mount_cifs(server: str, share: str, mount_point: str, username: str = "",
                   password: str = "", options: str = "", persist: bool = False) -> str:
    """Mount a CIFS/SMB share (//server/share) at `mount_point`. Credentials
    are written to a root-only file, never passed on the command line. With
    persist, the credentials file is kept and referenced from /etc/fstab;
    otherwise it's a temp file removed right after mounting."""
    server = (server or "").strip()
    share = (share or "").strip()
    mount_point = _validate_path(mount_point, "Mount point")
    username = (username or "").strip()
    if not _HOST_RE.match(server):
        raise ValueError("CIFS server must be a hostname or IP.")
    if not _SHARE_RE.match(share):
        raise ValueError("Share name is required (the part after //server/).")
    unc = f"//{server}/{share}"
    opts = _validate_mount_options(options)
    extra = f",{opts}" if opts else ""   # for the fstab line only (whole line is shlex-quoted below)
    q_unc, q_mnt = shlex.quote(unc), shlex.quote(mount_point)
    q_user, q_pass = shlex.quote(username or "guest"), shlex.quote(password or "")
    # User-supplied mount options must NOT be inlined into the mount command
    # raw - a space/';' in them would otherwise run arbitrary code as root.
    # Carry them through a single-quoted shell var and append to -o only if set.
    q_opts = shlex.quote(opts)
    pre = (
        "if ! command -v mount.cifs >/dev/null 2>&1 && ! ls /sbin/mount.cifs >/dev/null 2>&1; then "
        "echo 'CIFS client not installed - install cifs-utils first.' >&2; exit 1; fi; "
        f"mkdir -p {q_mnt}; "
    )
    if persist:
        cred_path = f"/etc/sysible-cifs/{mount_point.strip('/').replace('/', '-') or 'root'}.cred"
        q_cred = shlex.quote(cred_path)
        fstab_line = f"{unc} {mount_point} cifs credentials={cred_path}{extra} 0 0"
        return (
            pre +
            f"mkdir -p /etc/sysible-cifs && cred={q_cred}; "
            f"printf 'username=%s\\npassword=%s\\n' {q_user} {q_pass} > \"$cred\" && chmod 600 \"$cred\"; "
            f"O={q_opts}; mount -t cifs -o \"credentials=$cred${{O:+,$O}}\" {q_unc} {q_mnt} && "
            f"printf 'Mounted %s at %s.\\n' {q_unc} {q_mnt} && "
            f"{{ grep -qsF {shlex.quote(mount_point + ' cifs')} /etc/fstab "
            f"|| printf '%s\\n' {shlex.quote(fstab_line)} >> /etc/fstab; echo 'Persisted to /etc/fstab.'; }}"
        )
    return (
        pre +
        "cred=$(mktemp) && chmod 600 \"$cred\"; "
        f"printf 'username=%s\\npassword=%s\\n' {q_user} {q_pass} > \"$cred\"; "
        f"O={q_opts}; mount -t cifs -o \"credentials=$cred${{O:+,$O}}\" {q_unc} {q_mnt}; rc=$?; rm -f \"$cred\"; "
        f"if [ \"$rc\" -eq 0 ]; then printf 'Mounted %s at %s.\\n' {q_unc} {q_mnt}; else echo 'CIFS mount failed.' >&2; exit \"$rc\"; fi"
    )


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
        '*) echo "Unsupported or undetected filesystem type (\\"$_fst\\") - supported: ext2/ext3/ext4, xfs, btrfs." >&2; exit 1;; '
        "esac"
    )


# --- repair ------------------------------------------------------------------

def cmd_repair_filesystem(device: str, auto_yes: bool = True) -> str:
    """Checks/repairs the filesystem on `device`, dispatching to the tool that
    actually repairs that filesystem type. A bare `fsck` is only a front-end and
    execs a do-NOTHING stub for XFS and btrfs (fsck.xfs / fsck.btrfs print a
    notice and exit 0 without checking anything), so running plain fsck on the
    default root fs of RHEL/Rocky/Alma (XFS) or Fedora/openSUSE (btrfs) would
    report success while repairing nothing. Dispatch by detected type instead:
    e2fsck for ext*, xfs_repair for XFS, `btrfs check` for btrfs, fsck.fat for
    FAT/vfat, generic fsck otherwise. Refuses if the device is currently mounted
    (checking a mounted filesystem can corrupt it further; unmount it first)."""
    device = _validate_path(device, "Device")
    q_dev = shlex.quote(device)
    if auto_yes:
        ext, xfs, btrfs, fat, gen = "e2fsck -y", "xfs_repair", "btrfs check --repair", "fsck.fat -a", "fsck -y"
    else:
        ext, xfs, btrfs, fat, gen = "e2fsck -n", "xfs_repair -n", "btrfs check", "fsck.fat -n", "fsck -n"
    return (
        f"if findmnt -no TARGET {q_dev} >/dev/null 2>&1; then "
        "echo 'Refusing to check/repair - target is currently mounted. Unmount it first.' >&2; exit 1; fi; "
        f"t=$(blkid -o value -s TYPE {q_dev} 2>/dev/null); "
        f'[ -z "$t" ] && t=$(lsblk -dno FSTYPE {q_dev} 2>/dev/null | head -n1); '
        'case "$t" in '
        f"ext2|ext3|ext4) {ext} {q_dev} 2>&1;; "
        f"xfs) if command -v xfs_repair >/dev/null 2>&1; then {xfs} {q_dev} 2>&1; "
        "else echo 'xfs_repair not installed on this host (package: xfsprogs).' >&2; exit 1; fi;; "
        f"btrfs) if command -v btrfs >/dev/null 2>&1; then {btrfs} {q_dev} 2>&1; "
        "else echo 'btrfs tools not installed on this host (package: btrfs-progs).' >&2; exit 1; fi;; "
        f"vfat|fat|msdos) {fat} {q_dev} 2>&1;; "
        "'') echo 'Could not determine the filesystem type - refusing to guess. "
        "Verify the device path, or format it first.' >&2; exit 1;; "
        f"*) {gen} {q_dev} 2>&1;; "
        "esac"
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
    # fstype and options are written verbatim into an /etc/fstab line (tab-delimited,
    # newline-terminated). shlex.quote on the whole line blocks shell injection but NOT
    # an embedded newline, which would append a second fstab entry. A filesystem type is
    # a single bare token; options are comma-separated with no whitespace/newlines
    # (reuse the same guard the mount builders apply).
    if not _FSTYPE_RE.match(fstype):
        raise ValueError("Filesystem type must be a single token (e.g. ext4, xfs, nfs).")
    options = _validate_mount_options(options) or "defaults"
    dump = _validate_int_range(dump, 0, 1, "Dump field")
    pass_num = _validate_int_range(pass_num, 0, 9, "Pass field")

    line = f"{device}\t{mount_point}\t{fstype}\t{options}\t{dump}\t{pass_num}"
    q_line = shlex.quote(line)
    q_mnt = shlex.quote(mount_point)

    return (
        # Match the mount point against the SECOND field exactly (like
        # cmd_remove_fstab_entry), not a substring anywhere on the line - a plain
        # `grep -F /data` also matched a device column, a comment, or a longer
        # mount point (/data2), wrongly refusing a legitimate new entry.
        f"if awk -v m={q_mnt} '$2==m{{f=1}} END{{exit !f}}' /etc/fstab 2>/dev/null; then "
        "echo 'An /etc/fstab entry for that mount point already exists - remove it first if you want to replace it.' >&2; exit 1; fi; "
        f"cp /etc/fstab /etc/fstab.bak.$(date +%s) "
        f"&& echo {q_line} >> /etc/fstab "
        f"&& echo 'Added to /etc/fstab:' && grep -F {q_line} /etc/fstab"
    )


def cmd_remove_fstab_entry(mount_point: str, allow_critical: bool = False) -> str:
    """Removes the line whose mount-point field (2nd column) exactly
    matches `mount_point` - by field, not substring, so removing
    "/data" won't also match "/data2". Backs up /etc/fstab first. Refuses a
    system-critical mount (see client/system_paths) unless `allow_critical` is
    set - which the UIs only pass after a superuser confirms the warning."""
    mount_point = _validate_path(mount_point, "Mount point")
    _reject_critical_mount(mount_point, allow_critical, "fstab mount point")
    q_mnt = shlex.quote(mount_point)
    return (
        f"cp /etc/fstab /etc/fstab.bak.$(date +%s) && "
        f"awk -v mnt={q_mnt} '$2 != mnt' /etc/fstab > /tmp/fstab.new.$$ "
        f"&& mv /tmp/fstab.new.$$ /etc/fstab && echo 'Removed any /etc/fstab entry for' {q_mnt}"
    )


# --- quotas --------------------------------------------------------------------

def cmd_enable_quotas(mount_point: str) -> str:
    """quotacheck + quotaon for a filesystem already mounted with
    usrquota/grpquota (set that in /etc/fstab and remount first).

    XFS is the exception: it accounts quotas in-kernel and does NOT use the
    quotacheck/quotaon workflow (quotacheck skips/errors on XFS). XFS quotas are
    turned on via the uquota/gquota mount options, so on an XFS mount this reports
    what to do instead of running the ext-only tools that would fail there. XFS is
    the default root fs on RHEL/Rocky/Alma/Fedora, so this branch matters."""
    mount_point = _validate_path(mount_point, "Mount point")
    q_mnt = shlex.quote(mount_point)
    return (
        f"t=$(findmnt -no FSTYPE {q_mnt} 2>/dev/null); "
        'if [ "$t" = xfs ]; then '
        "echo 'XFS manages quotas in-kernel: add uquota,gquota (or prjquota) to the "
        "/etc/fstab options for this mount and remount (or reboot). quotacheck/quotaon "
        "are not used on XFS; use xfs_quota to set limits.' >&2; exit 1; fi; "
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
