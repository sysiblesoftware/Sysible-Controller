"""FILE SYSTEM MANAGEMENT (file/directory-level operations) - split out
of client/api.py to keep individual file sizes manageable. Imported via
`from client._api_filesystem import *` at the bottom of client/api.py.

Covers directory creation/removal, copy/move/rename, ownership and
permissions (including POSIX ACLs), symbolic and hard links, and
archiving/compression. All built on standard coreutils/tar/gzip-family
tools every supported distro already ships - nothing here assumes a
particular filesystem type.

Mount/unmount/resize/repair/fstab/quota management (filesystem- and
storage-level, rather than file-level, and the parts that do need to
be filesystem-type-aware) lives in the sibling client/
_api_filesystem_mount.py module instead. "Check disk usage" and "Find
large files" already exist as cmd_disk_usage()/cmd_find_large_files()
in client/_api_dispatch.py (System Health & Logs) and are reused as-is
there rather than duplicated here.
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


_DANGEROUS_PATHS = {
    "/", "/etc", "/bin", "/sbin", "/usr", "/var", "/boot", "/lib", "/lib64",
    "/dev", "/proc", "/sys", "/root", "/home", "/opt", "/run",
}


def _reject_dangerous_path(path: str, label: str = "Path") -> None:
    """Refuses a recursive/destructive operation against a top-level
    system directory - the kind of mistake (an extra `rm -rf` on the
    wrong path) this tool should make structurally hard to make, not
    just possible to make carefully."""
    normalized = path.rstrip("/") or "/"
    if normalized in _DANGEROUS_PATHS:
        raise ValueError(
            f"{label} '{path}' is a top-level system directory - refusing to run "
            "a recursive/destructive operation against it. Target a specific "
            "subdirectory instead."
        )


_SAFE_USERGROUP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _validate_user_or_group(name: str, label: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError(f"{label} is required.")
    if not _SAFE_USERGROUP_RE.match(name):
        raise ValueError(f"{label} doesn't look like a valid Linux user/group name.")
    return name


_OCTAL_MODE_RE = re.compile(r"^[0-7]{3,4}$")
_SYMBOLIC_MODE_RE = re.compile(r"^[ugoa]*[+\-=][rwxXst]+(,[ugoa]*[+\-=][rwxXst]+)*$")


def _validate_chmod_mode(mode: str, label: str = "Mode") -> str:
    mode = (mode or "").strip()
    if not mode:
        raise ValueError(f"{label} is required (e.g. 755, 0644, or u+x).")
    if not (_OCTAL_MODE_RE.match(mode) or _SYMBOLIC_MODE_RE.match(mode)):
        raise ValueError(
            f"{label} must be an octal mode (e.g. 755, 0644) or a symbolic "
            "clause (e.g. u+x, g-w, a=r)."
        )
    return mode


# --- directories -------------------------------------------------------

def cmd_list_directory(path: str = "", show_hidden: bool = True) -> str:
    """`ls -l` (long listing) of a directory, defaulting to the current
    directory when no path is given. Human-readable sizes; hidden files
    included by default. Read-only - safe to run anywhere."""
    flags = "-lhA" if show_hidden else "-lh"
    if path and path.strip():
        path = _validate_path(path, "Directory path")
        return f"ls {flags} --color=never -- {shlex.quote(path)} 2>&1"
    return f"ls {flags} --color=never 2>&1"


def cmd_create_directory(path: str, mode: str = "") -> str:
    """mkdir -p - safe to call on a path that already exists, and
    creates any missing parent directories along the way."""
    path = _validate_path(path, "Directory path")
    q_path = shlex.quote(path)
    if mode and mode.strip():
        m = _validate_chmod_mode(mode)
        return f"mkdir -p -m {m} {q_path} 2>&1"
    return f"mkdir -p {q_path} 2>&1"


def cmd_remove_directory(path: str, recursive: bool = False) -> str:
    """rmdir for an empty directory, or `rm -rf` if `recursive` is set
    - refuses outright against a top-level system directory either
    way."""
    path = _validate_path(path, "Directory path")
    _reject_dangerous_path(path, "Directory path")
    q_path = shlex.quote(path)
    if recursive:
        return f"rm -rf {q_path} 2>&1"
    return f"rmdir {q_path} 2>&1"


# --- copy / move / rename -----------------------------------------------

def cmd_copy_file(source: str, destination: str, recursive: bool = True) -> str:
    """cp -a (archive mode: preserves permissions/ownership/timestamps/
    symlinks) for a directory tree, or cp -p (preserve mode/ownership/
    timestamps) for a single file."""
    source = _validate_path(source, "Source path")
    destination = _validate_path(destination, "Destination path")
    flag = "-a" if recursive else "-p"
    return f"cp {flag} {shlex.quote(source)} {shlex.quote(destination)} 2>&1"


def cmd_move_file(source: str, destination: str) -> str:
    source = _validate_path(source, "Source path")
    destination = _validate_path(destination, "Destination path")
    return f"mv {shlex.quote(source)} {shlex.quote(destination)} 2>&1"


def cmd_rename_file(path: str, new_name: str) -> str:
    """Renames in place - `new_name` must be a bare filename (no `/`),
    applied alongside the existing parent directory, so this can't
    also relocate the file to a different directory (use Move for
    that)."""
    path = _validate_path(path, "Path")
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("New name is required.")
    if "/" in new_name:
        raise ValueError(
            "New name must be a filename only (no '/') - use Move/Copy to "
            "relocate to a different directory."
        )
    q_path = shlex.quote(path)
    q_name = shlex.quote(new_name)
    return f'_dir=$(dirname {q_path}); mv {q_path} "$_dir"/{q_name} 2>&1'


# --- ownership / permissions / ACLs -------------------------------------

def cmd_change_ownership(path: str, owner: str = "", group: str = "", recursive: bool = False) -> str:
    path = _validate_path(path, "Path")
    owner = (owner or "").strip()
    group = (group or "").strip()
    if not owner and not group:
        raise ValueError("At least one of owner or group is required.")

    spec = ""
    if owner:
        spec += _validate_user_or_group(owner, "Owner")
    if group:
        spec += f":{_validate_user_or_group(group, 'Group')}"

    if recursive:
        _reject_dangerous_path(path)

    flag = "-R " if recursive else ""
    return f"chown {flag}{shlex.quote(spec)} {shlex.quote(path)} 2>&1"


def cmd_change_permissions(path: str, mode: str, recursive: bool = False) -> str:
    path = _validate_path(path, "Path")
    mode = _validate_chmod_mode(mode)

    if recursive:
        _reject_dangerous_path(path)

    flag = "-R " if recursive else ""
    return f"chmod {flag}{mode} {shlex.quote(path)} 2>&1"


def cmd_set_acl(path: str, acl_entries: str, recursive: bool = False) -> str:
    """Sets (replaces, via `setfacl -m`) the given ACL entries, e.g.
    acl_entries = "u:alice:rwx,g:devs:rx". Requires the `acl` package
    and a filesystem mounted with ACL support (the default on most
    modern ext4/xfs setups)."""
    path = _validate_path(path, "Path")
    acl_entries = (acl_entries or "").strip()
    if not acl_entries:
        raise ValueError("ACL entries are required, e.g. u:alice:rwx,g:devs:rx")
    if any(c.isspace() for c in acl_entries):
        raise ValueError("ACL entries must not contain spaces - separate multiple entries with commas.")

    flag = "-R " if recursive else ""
    q_acl = shlex.quote(acl_entries)
    q_path = shlex.quote(path)
    return (
        "if ! command -v setfacl >/dev/null 2>&1; then "
        "echo 'setfacl is not installed on this host (package: acl).' >&2; exit 1; fi; "
        f"setfacl {flag}-m {q_acl} {q_path} 2>&1"
    )


def cmd_show_acl(path: str) -> str:
    """getfacl - read-only, useful alongside Set ACL above to confirm
    what's actually on a path before/after a change."""
    path = _validate_path(path, "Path")
    q_path = shlex.quote(path)
    return (
        "if ! command -v getfacl >/dev/null 2>&1; then "
        "echo 'getfacl is not installed on this host (package: acl).' >&2; exit 1; fi; "
        f"getfacl {q_path} 2>&1"
    )


# --- links ---------------------------------------------------------------

def cmd_create_symlink(target: str, link_path: str) -> str:
    target = _validate_path(target, "Target path")
    link_path = _validate_path(link_path, "Link path")
    return f"ln -s {shlex.quote(target)} {shlex.quote(link_path)} 2>&1"


def cmd_create_hardlink(target: str, link_path: str) -> str:
    """Hard links require `target` and `link_path` to be on the same
    filesystem - ln will fail with a clear "Invalid cross-device link"
    error otherwise, which is surfaced as-is rather than guessed at
    ahead of time."""
    target = _validate_path(target, "Target path")
    link_path = _validate_path(link_path, "Link path")
    return f"ln {shlex.quote(target)} {shlex.quote(link_path)} 2>&1"


# --- archive / compress ----------------------------------------------------

def _split_for_tar(path: str):
    """(dir, basename) so an archive stores a relative entry name
    rather than the full absolute path (avoids tar's "Removing leading
    '/'" warning and keeps the archive relocatable)."""
    path = path.rstrip("/") or "/"
    if "/" not in path:
        return ".", path
    d, b = path.rsplit("/", 1)
    return (d or "/"), b


_ARCHIVE_FLAGS = {"none": "-cf", "gzip": "-czf", "bzip2": "-cjf", "xz": "-cJf"}


def cmd_create_archive(source_path: str, archive_path: str, compression: str = "gzip") -> str:
    """Bundles a file or directory tree into one archive. compression:
    "none" (.tar), "gzip" (.tar.gz), "bzip2" (.tar.bz2), or "xz"
    (.tar.xz)."""
    source_path = _validate_path(source_path, "Source path")
    archive_path = _validate_path(archive_path, "Archive path")
    flag = _ARCHIVE_FLAGS.get(compression)
    if flag is None:
        raise ValueError(f"Unknown compression '{compression}' - use none, gzip, bzip2, or xz.")
    src_dir, src_base = _split_for_tar(source_path)
    return (
        f"tar {flag} {shlex.quote(archive_path)} "
        f"-C {shlex.quote(src_dir)} {shlex.quote(src_base)} 2>&1"
    )


def cmd_extract_archive(archive_path: str, destination_dir: str) -> str:
    """tar -xf auto-detects compression (.tar/.tar.gz/.tar.bz2/.tar.xz)
    on its own - no need to ask which kind it is."""
    archive_path = _validate_path(archive_path, "Archive path")
    destination_dir = _validate_path(destination_dir, "Destination directory")
    q_dest = shlex.quote(destination_dir)
    return f"mkdir -p {q_dest} && tar -xf {shlex.quote(archive_path)} -C {q_dest} 2>&1"


_COMPRESS_TOOLS = {"gzip": "gzip", "bzip2": "bzip2", "xz": "xz"}


def cmd_compress_file(path: str, method: str = "gzip", keep_original: bool = True) -> str:
    """Single-file, in-place compression - distinct from Archive Files
    above, which bundles many files/a directory into one .tar first.
    method: "gzip", "bzip2", "xz", or "zip"."""
    path = _validate_path(path, "File path")
    q_path = shlex.quote(path)

    if method == "zip":
        return (
            "if ! command -v zip >/dev/null 2>&1; then "
            "echo 'zip is not installed on this host.' >&2; exit 1; fi; "
            f"zip {q_path}.zip {q_path} 2>&1"
        )

    tool = _COMPRESS_TOOLS.get(method)
    if tool is None:
        raise ValueError(f"Unknown compression method '{method}' - use gzip, bzip2, xz, or zip.")
    keep_flag = "-k " if keep_original else ""
    return (
        f"if ! command -v {tool} >/dev/null 2>&1; then "
        f"echo '{tool} is not installed on this host.' >&2; exit 1; fi; "
        f"{tool} {keep_flag}{q_path} 2>&1"
    )


def cmd_decompress_file(path: str, keep_original: bool = True) -> str:
    """Auto-detects method from the filename extension (.gz/.bz2/.xz/
    .zip)."""
    path = _validate_path(path, "File path")
    q_path = shlex.quote(path)
    keep_flag = "-k " if keep_original else ""
    return (
        f'case "$(printf %s {q_path} | tr "[:upper:]" "[:lower:]")" in '
        f"*.gz) command -v gunzip >/dev/null 2>&1 && gunzip {keep_flag}{q_path} 2>&1 "
        "|| echo 'gunzip not installed on this host.' >&2;; "
        f"*.bz2) command -v bunzip2 >/dev/null 2>&1 && bunzip2 {keep_flag}{q_path} 2>&1 "
        "|| echo 'bunzip2 not installed on this host.' >&2;; "
        f"*.xz) command -v unxz >/dev/null 2>&1 && unxz {keep_flag}{q_path} 2>&1 "
        "|| echo 'unxz not installed on this host.' >&2;; "
        f"*.zip) command -v unzip >/dev/null 2>&1 && unzip -o {q_path} 2>&1 "
        "|| echo 'unzip not installed on this host.' >&2;; "
        '*) echo "Could not detect a compression type from the filename - expected .gz/.bz2/.xz/.zip." >&2; exit 1;; '
        "esac"
    )
