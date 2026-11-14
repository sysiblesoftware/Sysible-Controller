"""Resolve a name the way the PLATFORM HOST would, from inside the container.

Asked for in exactly those words: "Make the controller resolve it through the
host."

THE PROBLEM. The Controller runs in a container. A container gets its own
resolver: Docker's embedded DNS, then whatever that forwards to. The machine
it runs on has a different one -- a LAN DNS server handed out by DHCP, a
corporate resolver, a search domain that turns `ubuntu-lab` into
`ubuntu-lab.corp.example.com`. So a name an operator resolves on that machine
every day is simply unknown inside the container, which is what
`getaddrinfo("ubuntu-lab")` kept reporting, correctly and uselessly.

WHAT THIS DOES. It reads the host's own resolver CONFIGURATION, mounted
read-only, and asks those servers itself. Not the container's resolver
pointed somewhere new -- the container cannot rewrite its own
/etc/resolv.conf, which Docker owns and which the Controller's uid cannot
write -- but the same servers, with the same search domains, queried
directly.

WHY TWO FILES. On systemd-resolved hosts -- Ubuntu, Debian, most of the
fleet this manages -- /etc/resolv.conf says

    nameserver 127.0.0.53

which is the stub listener. 127.0.0.53 inside a container is the CONTAINER's
loopback, where nothing is listening, so taking that file at face value
produces a timeout rather than an answer. The real upstream servers are in
/run/systemd/resolve/resolv.conf, and that is the file worth having. Both are
mounted; loopback addresses are skipped wherever they appear, and if every
server is loopback the caller is told that rather than left waiting.

This is a RESOLVER and nothing else: it sends DNS queries and reads replies.
It opens no connection to any host being enrolled.
"""
from __future__ import annotations

import ipaddress
import os
import random
import socket
import struct
import time

# A query is a few hundred bytes and an answer arrives in milliseconds on a
# working network. The budget is per SERVER, and the servers are tried in
# order, so a host listing three dead ones still answers the screen promptly.
TIMEOUT = 1.5
MAX_SERVERS = 3
MAX_SEARCH = 4


def host_resolv_paths() -> list:
    """The host's resolver config, best file first.

    systemd-resolved's upstream list before /etc/resolv.conf, because the
    latter is the stub pointer on exactly the hosts this is for.
    """
    explicit = os.getenv("SYSIBLE_HOST_RESOLV")
    if explicit:
        return [p for p in explicit.split(":") if p]
    return ["/host/run/resolv.conf", "/host/etc/resolv.conf"]


def parse_resolv(text: str) -> tuple:
    """(nameservers, search domains) from a resolv.conf body."""
    servers, search = [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "nameserver" and len(parts) > 1:
            if parts[1] not in servers:
                servers.append(parts[1])
        elif parts[0] in ("search", "domain") and len(parts) > 1:
            for d in parts[1:]:
                if d not in search:
                    search.append(d)
    return servers, search


def in_container() -> bool:
    """Whether this process is in a container, which decides what 127.x means.

    Docker creates /.dockerenv in every container it starts. SYSIBLE_IN_CONTAINER
    overrides it for anything that does not -- and for the tests, which have to
    exercise both answers.
    """
    forced = (os.getenv("SYSIBLE_IN_CONTAINER") or "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    return os.path.exists("/.dockerenv")


def _usable(servers: list) -> tuple:
    """(usable, skipped).

    A LOOPBACK nameserver depends entirely on where this is running. In a
    container 127.0.0.53 is the CONTAINER's loopback, where nothing is
    listening, and taking it at face value buys a timeout instead of an
    answer. Installed NATIVELY on the platform host it is systemd-resolved,
    which is precisely the right server to ask -- the one that knows the
    search domains, the per-link resolvers and the local names. Same address,
    opposite meanings, so this cannot be decided by looking at the address.
    """
    containerised = in_container()
    usable, skipped = [], []
    for s in servers:
        try:
            ip = ipaddress.ip_address(s)
        except ValueError:
            skipped.append(s)
            continue
        (skipped if (containerised and ip.is_loopback) else usable).append(s)
    return usable, skipped


def host_config() -> dict:
    """What the host's resolver is set up to do, as this container can see it."""
    for path in host_resolv_paths():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(64_000)
        except OSError:
            continue
        servers, search = parse_resolv(text)
        if not servers:
            continue
        usable, skipped = _usable(servers)
        return {"path": path, "servers": usable, "skipped": skipped,
                "search": search[:MAX_SEARCH], "present": True}
    return {"path": "", "servers": [], "skipped": [], "search": [], "present": False}


# ---- a very small DNS client ----------------------------------------------
def _encode(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("idna") if any(ord(c) > 127 for c in label) else label.encode()
        if not 0 < len(b) < 64:
            raise ValueError(f"bad label in {name!r}")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _skip_name(buf: bytes, i: int) -> int:
    """Past a name, following nothing: a compression pointer ends it."""
    while i < len(buf):
        n = buf[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:          # pointer: two bytes, and the name ends
            return i + 2
        i += 1 + n
    raise ValueError("truncated name")


def _parse_answers(buf: bytes, qid: int) -> list:
    if len(buf) < 12:
        raise ValueError("short reply")
    rid, flags, qd, an = struct.unpack(">HHHH", buf[:8])
    if rid != qid:
        raise ValueError("reply id does not match the query")
    if flags & 0x000F:                # RCODE: NXDOMAIN, SERVFAIL, REFUSED...
        return []
    i = 12
    for _ in range(qd):
        i = _skip_name(buf, i) + 4    # + QTYPE, QCLASS
    out = []
    for _ in range(an):
        i = _skip_name(buf, i)
        rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", buf[i:i + 10])
        i += 10
        if rtype == 1 and rclass == 1 and rdlen == 4:          # A
            out.append(".".join(str(b) for b in buf[i:i + 4]))
        elif rtype == 28 and rclass == 1 and rdlen == 16:      # AAAA
            out.append(str(ipaddress.ip_address(buf[i:i + 16])))
        i += rdlen
    return out


def query(name: str, server: str, timeout: float = TIMEOUT, port: int = 53) -> list:
    """Ask ONE server for `name`. [] for "no answer", never an exception.

    `port` exists because the tests run a real DNS server to ask. Nothing in
    the product passes it: resolv.conf has no syntax for a port -- `#` is its
    comment character -- so 53 is the only port a nameserver line can mean.
    """
    qid = random.randrange(0, 0xFFFF)
    try:
        question = _encode(name) + struct.pack(">HH", 1, 1)
    except (ValueError, UnicodeError):
        return []
    packet = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + question
    try:
        fam = socket.AF_INET6 if ipaddress.ip_address(server).version == 6 \
            else socket.AF_INET
    except ValueError:
        return []
    try:
        with socket.socket(fam, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(packet, (server, port))
            while True:
                buf, _addr = s.recvfrom(4096)
                try:
                    return _parse_answers(buf, qid)
                except ValueError:
                    continue          # someone else's packet; keep waiting
    except (OSError, socket.timeout):
        return []


def resolve(name: str) -> tuple:
    """(addresses, how) using the host's resolver. ([], "") when it cannot.

    `how` names what answered, for a screen that has to explain itself: the
    server and, when a search domain was what made it work, the name that was
    actually asked for.
    """
    name = (name or "").strip().rstrip(".")
    if not name:
        return [], ""
    cfg = host_config()
    if not cfg["servers"]:
        return [], ""
    # The bare name first, then each search domain -- the order a stub
    # resolver uses, so this agrees with what the host would have done.
    candidates = [name]
    if "." not in name:
        candidates += [f"{name}.{d}" for d in cfg["search"]]
    for server in cfg["servers"][:MAX_SERVERS]:
        for candidate in candidates:
            got = query(candidate, server)
            if got:
                via = f"the platform host's DNS ({server})"
                if candidate != name:
                    via += f", as {candidate}"
                return got, via
    return [], ""
