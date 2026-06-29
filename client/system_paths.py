"""
Single source of truth for **system-critical files and mounts** — the paths
whose deletion (rm/rmdir), unmounting, or fstab-entry removal can break a host.

Used to warn (and, for non-superusers, block) destructive File System
Management operations in both front ends:
  - the desktop GUI (client/file_system_management_page.py),
  - the web console (webgui/server.py + webgui/frontend ToolPage),
  - and as a backstop inside the cmd_* builders themselves
    (client/_api_filesystem*.py), so nothing slips through regardless of UI.

`system_critical_reason(path)` returns a human-readable reason string when a
path is critical, or None when it's safe. Matching is purely lexical (no
filesystem access): a path is critical if it equals one of CRITICAL_EXACT, or
sits at/under one of CRITICAL_PREFIXES.
"""
import posixpath

# --- exact critical paths -------------------------------------------------
# Top-level and well-known system directories + standard mount points. These
# are also the mounts whose unmount / fstab removal is dangerous.
_CRITICAL_DIRS = {
    "/",
    "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
    "/usr", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64", "/usr/local",
    "/etc", "/etc/systemd", "/etc/ssh", "/etc/pam.d", "/etc/sudoers.d",
    "/etc/selinux", "/etc/default", "/etc/security",
    "/var", "/var/lib", "/var/log", "/var/run", "/var/lib/dpkg", "/var/lib/rpm",
    "/boot", "/boot/efi", "/boot/grub", "/boot/grub2",
    "/dev", "/proc", "/sys", "/run", "/run/systemd",
    "/root", "/home", "/srv", "/opt", "/tmp", "/mnt", "/media",
}

# Individual files whose removal disables boot, login, name resolution,
# privilege, or the init system.
_CRITICAL_FILES = {
    # accounts / privilege
    "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow",
    "/etc/sudoers", "/etc/login.defs", "/etc/securetty",
    "/etc/pam.conf", "/etc/nsswitch.conf",
    # filesystems / mounts
    "/etc/fstab", "/etc/crypttab", "/etc/mtab",
    # networking / identity
    "/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/etc/host.conf",
    "/etc/machine-id", "/etc/os-release", "/etc/networks", "/etc/exports",
    # shells / environment / boot config
    "/etc/profile", "/etc/bashrc", "/etc/bash.bashrc", "/etc/environment",
    "/etc/inittab", "/etc/sysctl.conf",
    "/etc/default/grub", "/boot/grub/grub.cfg", "/boot/grub2/grub.cfg",
    # ssh / selinux
    "/etc/ssh/sshd_config", "/etc/selinux/config",
    # the init system itself
    "/sbin/init", "/usr/sbin/init",
    "/lib/systemd/systemd", "/usr/lib/systemd/systemd",
    # the controller's own install + this host's agent
    "/opt/sysible",
}

CRITICAL_EXACT = _CRITICAL_DIRS | _CRITICAL_FILES

# --- critical prefixes ----------------------------------------------------
# Anything at or beneath these locations is treated as critical to delete.
# Trailing slash is required so "/etc" matches "/etc/..." but not "/etcfoo".
CRITICAL_PREFIXES = (
    "/boot/", "/etc/", "/dev/", "/proc/", "/sys/", "/run/systemd/",
    "/bin/", "/sbin/", "/lib/", "/lib64/",
    "/usr/bin/", "/usr/sbin/", "/usr/lib/", "/usr/lib64/",
    "/var/lib/dpkg/", "/var/lib/rpm/",
)


def normalize(path):
    """Lexically normalize an absolute path for comparison: collapse `.`/`..`
    and duplicate slashes, strip a trailing slash (except for root). Returns
    "" for an empty/blank input. Relative paths are returned normalized but
    won't match the absolute critical sets."""
    p = (path or "").strip()
    if not p:
        return ""
    p = posixpath.normpath(p)
    return p


def system_critical_reason(path):
    """Return a human-readable reason if `path` is a system-critical file or
    mount, else None."""
    p = normalize(path)
    if not p:
        return None
    if p in CRITICAL_EXACT:
        if p in _CRITICAL_FILES:
            return f"'{p}' is a critical system file — removing it can break boot, login, or networking."
        return f"'{p}' is a critical system directory / mount point — removing or unmounting it can break the host."
    for pref in CRITICAL_PREFIXES:
        if (p + "/").startswith(pref):
            return f"'{p}' is under the system-critical location {pref.rstrip('/')} — changes here can break the host."
    return None


def is_system_critical(path):
    return system_critical_reason(path) is not None
