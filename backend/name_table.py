"""The names this Controller can resolve, beyond what its own resolver knows.

Enrollment lets an operator type a host NAME and leave the address blank. That
works when the Controller can resolve the name -- and the Controller usually
runs in a container, whose /etc/hosts is its own, not the platform host's. So
an operator with `192.168.155.135  ubuntu-lab` sitting in the host's
/etc/hosts types `ubuntu-lab`, and the container, correctly and uselessly,
reports that it has never heard of it.

This module is the fix: a name->address table assembled from files the
Controller CAN see.

    /host/etc/hosts     the platform host's own /etc/hosts, bind-mounted
                        read-only by the compose file. Zero configuration --
                        the names the host knows are the names the console
                        offers.

    /data/name_table    the Controller's OWN table, in the persisted volume,
                        written from the console. This is the one that works
                        when there is no mount to make: a Controller on a
                        different machine from the hosts it manages, or one
                        whose operator keeps the list somewhere other than
                        /etc/hosts.

The Controller's own table WINS over the mounted file, which wins over DNS --
the same precedence a machine gives its own /etc/hosts, and for the same
reason: what the operator wrote down beats what the network says.

It is a LOOKUP TABLE and nothing else. It never dials a host, and a name that
is not in it is not an error here; the caller decides what a miss means.
"""
from __future__ import annotations

import os
import threading

from backend import hosts_file

# A hosts file is a list of machines, not a payload. 4 MB is far past any real
# one and still bounded, so a mount pointed at the wrong file cannot be read
# into memory in its entirety.
MAX_BYTES = 4_000_000

_LOCK = threading.Lock()
_CACHE: dict = {}        # path -> (stamp, table)


def host_hosts_path() -> str:
    """The platform host's /etc/hosts, as mounted into this container."""
    return os.getenv("SYSIBLE_HOST_HOSTS_FILE", "/host/etc/hosts")


def own_table_path() -> str:
    """The Controller's own table, in the volume that survives an update."""
    explicit = os.getenv("SYSIBLE_NAME_TABLE")
    if explicit:
        return explicit
    # The same home the rest of the app uses. In the container the image
    # sets it to /data; natively it is /opt/sysible, which is writable.
    return os.path.join(os.getenv("SYSIBLE_HOME", "/opt/sysible"), "name_table")


def build(text: str) -> dict:
    """A hosts-file body as {lowercased name: [address, ...]}.

    Only addresses that could name a machine to manage. A loopback,
    link-local, multicast or unspecified address is dropped rather than
    recorded: `127.0.1.1 myhost` is the Debian convention for a machine's own
    name, and resolving it would point enrollment back at the Controller
    itself -- which, with SSH listening, is a connection that could even
    succeed.
    """
    table: dict = {}
    for raw in text.splitlines():
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        fields = body.split()
        addr, names = fields[0], fields[1:]
        if not names:
            continue
        if hosts_file._classify(addr) is not None:
            continue
        for name in names:
            key = name.lower()
            if not hosts_file.HOST_NAME_RE.match(name):
                continue
            addrs = table.setdefault(key, [])
            if addr not in addrs:
                addrs.append(addr)
    return table


def _stamp(path: str):
    """Enough of a file's identity to know the cached parse is still good."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    # The inode matters as much as the mtime: an editor that writes a new file
    # and renames it over the old one leaves the mtime of a DIFFERENT inode,
    # and a bind-mounted single file does not follow that rename at all.
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _table_for(path: str) -> dict:
    stamp = _stamp(path)
    if stamp is None:
        return {}
    with _LOCK:
        hit = _CACHE.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_BYTES)
    except OSError:
        return {}
    table = build(text)
    with _LOCK:
        _CACHE[path] = (stamp, table)
    return table


def lookup(name: str) -> list:
    """Every address `name` is written down as, or []."""
    key = (name or "").strip().lower()
    if not key:
        return []
    for path in (own_table_path(), host_hosts_path()):
        hit = _table_for(path).get(key)
        if hit:
            return list(hit)
    return []


def save(text: str) -> int:
    """Replace the Controller's own table. Returns how many names it holds.

    Written to a neighbouring temp file and renamed, so a process reading the
    table while it is replaced sees the whole old one or the whole new one and
    never a half-written line.
    """
    table = build(text)
    path = own_table_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    with _LOCK:
        _CACHE.pop(path, None)
    return len(table)


def read_own() -> str:
    """The Controller's own table as written, or ""."""
    try:
        with open(own_table_path(), encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_BYTES)
    except OSError:
        return ""


def sources() -> list:
    """What the console shows: each file, whether it is there, how many names.

    The point of showing BOTH is that "the Controller cannot resolve this
    name" and "the host's /etc/hosts is not mounted" look identical from the
    console otherwise, and the fix for each is different.
    """
    out = []
    for key, path, label in (
        ("own", own_table_path(), "This Controller's own list"),
        ("host", host_hosts_path(), "The platform host's /etc/hosts"),
    ):
        table = _table_for(path)
        out.append({
            "key": key,
            "path": path,
            "label": label,
            "present": _stamp(path) is not None,
            "names": len(table),
        })
    return out
