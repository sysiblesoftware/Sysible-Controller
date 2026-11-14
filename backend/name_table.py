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
from backend.models.remote_models import HOST_NAME_RE

# A hosts file is a list of machines, not a payload. 4 MB is far past any real
# one and still bounded, so a mount pointed at the wrong file cannot be read
# into memory in its entirety.
MAX_BYTES = 4_000_000

_LOCK = threading.Lock()
_CACHE: dict = {}        # path -> (stamp, table)


def host_hosts_path() -> str:
    """The platform host's /etc/hosts, as mounted into this container."""
    return os.getenv("SYSIBLE_HOST_HOSTS_FILE", "/host/etc/hosts")


def local_hosts_path() -> str:
    """This process's own /etc/hosts.

    Readable in every deployment, with nothing to mount and nothing to set up.
    In a container it holds whatever `extra_hosts:` put there, plus anything
    the operator added -- and NATIVELY it is the platform host's own file, so
    a Controller installed on the metal reads exactly the list the question
    was about without any of the rest of this.
    """
    return os.getenv("SYSIBLE_LOCAL_HOSTS_FILE", "/etc/hosts")


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


# Every place a name can be written down, in the order they win. Declared
# ONCE: lookup, where, sources and the console list all walked their own copy
# of this, and a source added to one of them but not the others is a name the
# console offers and enrollment then cannot resolve.
def _source_specs():
    return (
        ("own", own_table_path(), "This Controller's own list"),
        ("host", host_hosts_path(), "The platform host's /etc/hosts"),
        ("local", local_hosts_path(), "The Controller's own /etc/hosts"),
    )


def lookup(name: str) -> list:
    """Every address `name` is written down as, or []."""
    return where(name)[0]


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
    for key, path, label in _source_specs():
        table = _table_for(path)
        out.append({
            "key": key,
            "path": path,
            "label": label,
            "present": _stamp(path) is not None,
            "names": len(table),
        })
    return out


def where(name: str):
    """Which source answers for `name`, for a console that has to explain itself.

    Returns (addresses, key) with key one of "own", "host" or None. Separate
    from lookup() because lookup's caller wants an answer and this caller
    wants a REASON: "it cannot resolve the name" and "the hosts file was never
    mounted" are the same dead end from the console, and the fix for each is a
    different one.
    """
    key = (name or "").strip().lower()
    if not key:
        return [], None
    for src, path, _label in _source_specs():
        hit = _table_for(path).get(key)
        if hit:
            return list(hit), src
    return [], None


def host_dns_note() -> str:
    """One clause on what the host's resolver looks like from in here."""
    from backend import host_resolver
    cfg = host_resolver.host_config()
    if not cfg["present"]:
        return ("the platform host's resolver config is not mounted "
                f"({host_resolver.host_resolv_paths()[0]})")
    if not cfg["servers"]:
        return ("the platform host's resolver config lists only loopback "
                f"servers ({', '.join(cfg['skipped'])}), which from inside this "
                f"container are this container")
    return (f"its DNS servers ({', '.join(cfg['servers'])}) were asked and did "
            f"not have it")


def diagnose(name: str) -> str:
    """Why `name` is not known here, naming EVERY place that was looked.

    There are four, and DNS through the platform host is now the one most
    likely to be the answer -- the reported name was never in anyone's
    /etc/hosts; it was in DNS. An early return that skipped the DNS clause
    left the one place that mattered unmentioned, which is how a diagnosis
    manages to be accurate and useless at the same time.
    """
    srcs = {s["key"]: s for s in sources()}
    host, own = srcs["host"], srcs["own"]

    def _n(k):
        return f"{k} name" + ("" if k == 1 else "s")

    bits = []
    if own["names"]:
        bits.append("this Controller's own list holds " + _n(own["names"]))
    else:
        bits.append("this Controller's own list is empty")
    if host["present"]:
        bits.append("the platform host's /etc/hosts is mounted and lists "
                    + _n(host["names"]))
    else:
        bits.append(f"nothing is mounted at {host['path']}")
    bits.append(host_dns_note())
    return (f"Right now {bits[0]}, {bits[1]}, and {bits[2]} \u2014 "
            f"nothing here names '{name}'.")


def entries() -> list:
    """Every name this Controller can resolve, with where it came from.

    One row per NAME, not per file: a name written in two places resolves to
    one address, and a list that showed both would be showing a choice the
    resolver does not offer. The row that wins is the row that is shown, and
    it carries `shadows` when a lower source also had that name -- so an
    operator whose edit appears to do nothing can see what is covering it.
    """
    rows, seen = [], {}
    for key, path, label in _source_specs():
        table = _table_for(path)
        for name in sorted(table):
            if name in seen:
                seen[name]["shadows"].append(label)
                continue
            row = {
                "name": name,
                "addresses": list(table[name]),
                "address": table[name][0],
                "source": key,
                "source_label": label,
                "editable": key == "own",   # only our own file is ours to change
                "shadows": [],
            }
            seen[name] = row
            rows.append(row)
    rows.sort(key=lambda r: r["name"])
    return rows


def _rewrite(mutate) -> int:
    """Read the own list, let `mutate` change its lines, write it back."""
    lines = read_own().splitlines()
    lines = mutate(lines)
    text = "\n".join(lines).rstrip("\n")
    if text:
        text += "\n"
    return save(text)


def add_entry(name: str, address: str) -> int:
    """Put one name->address in this Controller's own list.

    Replaces any line of the own list that already names it, rather than
    appending a second: two lines for one name is a file whose meaning depends
    on which one the parser reaches first, and nobody should have to know.
    """
    name = (name or "").strip()
    address = (address or "").strip()
    if not HOST_NAME_RE.match(name):
        raise ValueError(f"'{name}' is not a usable host name.")
    bad = _classify_addr(address)
    if bad:
        raise ValueError(bad)

    def mutate(lines):
        kept = []
        for raw in lines:
            body = raw.split("#", 1)[0].strip()
            fields = body.split()
            if len(fields) > 1 and any(n.lower() == name.lower() for n in fields[1:]):
                # Drop just this name; the line may carry others.
                others = [n for n in fields[1:] if n.lower() != name.lower()]
                if others:
                    kept.append(" ".join([fields[0]] + others))
                continue
            kept.append(raw)
        kept.append(f"{address}  {name}")
        return kept

    return _rewrite(mutate)


def remove_entry(name: str) -> int:
    """Take one name out of this Controller's own list.

    Refuses a name that is not IN that list. Rewriting our file and reporting
    success for a name that comes from somewhere else would tell an operator
    it was removed while it still resolves -- and the next thing they do is
    distrust the list rather than the button.
    """
    name = (name or "").strip().lower()
    if not name:
        raise ValueError("Give a name to remove.")
    if name not in build(read_own()):
        _, src = where(name)
        if src:
            label = dict((k, lbl) for k, _p, lbl in _source_specs())[src]
            raise ValueError(
                f"'{name}' is not in this Controller's own list -- it comes "
                f"from {label}, which this console does not write.")
        raise ValueError(f"'{name}' is not in this Controller's own list.")

    def mutate(lines):
        kept = []
        for raw in lines:
            body = raw.split("#", 1)[0].strip()
            fields = body.split()
            if len(fields) > 1 and any(n.lower() == name for n in fields[1:]):
                others = [n for n in fields[1:] if n.lower() != name]
                if others:
                    kept.append(" ".join([fields[0]] + others))
                continue
            kept.append(raw)
        return kept

    return _rewrite(mutate)


def _classify_addr(address: str):
    """Why this address cannot be written down here, or None."""
    if not address:
        return "Give an address for the name to point at."
    bad = hosts_file._classify(address)
    if bad == "bad-address":
        return f"'{address}' is not an IP address."
    if bad:
        return (f"{hosts_file.SKIP_REASONS[bad].capitalize()}: an address like "
                f"that never names a machine to manage.")
    if not hosts_file.HOST_ADDR_RE.match(address):
        return f"'{address}' is not an address this Controller could connect to."
    return None
