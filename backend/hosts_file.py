"""Read a hosts file and work out which machines in it are worth enrolling.

Enrollment took one host at a time: a name, an address, a user and a password.
That is the right shape for the first host and the wrong shape for the
fortieth, and the list an operator already has is almost always /etc/hosts.

This module does the reading ONLY. It decides nothing about credentials and it
touches no machine; the caller hands each candidate to the existing
/remote/enroll-ssh route, which keeps its own superuser gate, its duplicate-IP
check and its host limit. There is deliberately no bulk enrollment ENDPOINT --
a second path onto a host is a second place for those checks to be forgotten.

The format is the one every Unix uses:

    IP_address   canonical_hostname   [alias ...]

with # starting a comment. What makes it worth parsing carefully is everything
in a real /etc/hosts that is NOT a machine you want to enroll: the loopback
block, the IPv6 multicast boilerplate Debian ships, the host's own name, and
the entries that are already managed.
"""
from __future__ import annotations

import ipaddress
import re

# Names that mean "this machine" and never name something to enroll.
_SELF_NAMES = {
    "localhost", "localhost.localdomain", "localhost4", "localhost6",
    "localhost4.localdomain4", "localhost6.localdomain6",
    "ip6-localhost", "ip6-loopback",
}

# The IPv6 boilerplate Debian and Ubuntu put in every /etc/hosts.
_IP6_BOILERPLATE = {
    "ip6-localnet", "ip6-mcastprefix", "ip6-allnodes",
    "ip6-allrouters", "ip6-allhosts",
}

# What the ENROLLMENT ROUTE will accept, imported rather than restated. Every
# name this module offers is posted straight back to that route, so a second
# idea of "a usable name" here would mean a screen that proposes a host and an
# API that then refuses it -- with nothing to say which of the two was wrong.
from backend.models.remote_models import HOST_ADDR_RE, HOST_NAME_RE  # noqa: E402

# Why a line is not an enrollable machine. The caller SHOWS these rather than
# dropping the line silently: "it skipped something and I don't know what" is
# the complaint a filter earns when it does not say what it filtered.
SKIP_REASONS = {
    "loopback": "loopback address",
    "self": "names this machine",
    "boilerplate": "IPv6 multicast boilerplate",
    "unspecified": "unspecified address",
    "multicast": "multicast address",
    "link-local": "link-local address",
    "no-name": "address with no hostname",
    "bad-address": "not an IP address",
    "bad-name": "not a usable hostname",
    "duplicate": "same address as an earlier line",
}


def _classify(addr: str):
    """Why this address cannot be a host to enroll, or None."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "bad-address"
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    if ip.is_link_local:
        return "link-local"
    return None


def _short(name: str) -> str:
    return name.split(".", 1)[0]


def parse(text: str, max_lines: int = 20000) -> list[dict]:
    """Every line of `text`, in order, as a dict.

    Each carries: line (1-based), raw, address, names, name (the label to
    enroll it under), enrollable, and — when it is not — skip/skip_reason.

    EVERY line comes back, including the skipped ones. The caller is a screen
    that has to show an operator why their 40-line file produced 12 candidates;
    returning only the 12 makes that unanswerable.
    """
    out: list[dict] = []
    seen_addrs: dict[str, int] = {}
    # A short name is nicer to work with than web-01.corp.example.com, but only
    # while it is unambiguous. Counted over the whole file first, because
    # whether `web-01` is a safe label for line 3 depends on line 40.
    short_counts: dict[str, int] = {}
    parsed: list[tuple[int, str, str, list[str]]] = []

    for lineno, raw in enumerate(text.splitlines()[:max_lines], start=1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        fields = body.split()
        addr, names = fields[0], fields[1:]
        parsed.append((lineno, raw.rstrip(), addr, names))
        if _classify(addr) is None and names:
            usable = [n for n in names
                      if n.lower() not in _SELF_NAMES
                      and n.lower() not in _IP6_BOILERPLATE]
            if usable:
                short_counts[_short(usable[0]).lower()] = \
                    short_counts.get(_short(usable[0]).lower(), 0) + 1

    for lineno, raw, addr, names in parsed:
        row = {"line": lineno, "raw": raw, "address": addr, "names": names,
               "name": "", "enrollable": False, "skip": None, "skip_reason": None}

        bad = _classify(addr)
        if bad:
            row["skip"] = bad
            row["skip_reason"] = SKIP_REASONS[bad]
            out.append(row)
            continue

        lowered = [n.lower() for n in names]
        if not names:
            row["skip"], row["skip_reason"] = "no-name", SKIP_REASONS["no-name"]
            out.append(row)
            continue
        if all(n in _SELF_NAMES for n in lowered):
            row["skip"], row["skip_reason"] = "self", SKIP_REASONS["self"]
            out.append(row)
            continue
        if all(n in _IP6_BOILERPLATE or n in _SELF_NAMES for n in lowered):
            row["skip"] = "boilerplate"
            row["skip_reason"] = SKIP_REASONS["boilerplate"]
            out.append(row)
            continue

        usable = [n for n in names
                  if n.lower() not in _SELF_NAMES and n.lower() not in _IP6_BOILERPLATE]
        canonical = usable[0]
        label = _short(canonical)
        # The short form only when nothing else in the file shares it. Two
        # machines both arriving as "web-01" is a name collision the operator
        # would have to notice and undo by hand.
        if short_counts.get(label.lower(), 0) > 1 or not HOST_NAME_RE.match(label):
            label = canonical
        if not HOST_NAME_RE.match(label) or not HOST_ADDR_RE.match(addr):
            row["skip"], row["skip_reason"] = "bad-name", SKIP_REASONS["bad-name"]
            out.append(row)
            continue

        # Normalised, so 10.0.0.1 and 010.0.0.1 are one machine.
        key = str(ipaddress.ip_address(addr))
        if key in seen_addrs:
            row["skip"] = "duplicate"
            row["skip_reason"] = (f"{SKIP_REASONS['duplicate']} (line "
                                  f"{seen_addrs[key]})")
            row["name"] = label
            out.append(row)
            continue
        seen_addrs[key] = lineno

        row["name"] = label
        row["enrollable"] = True
        out.append(row)

    return out


def candidates(text: str) -> list[dict]:
    """Just the rows worth offering."""
    return [r for r in parse(text) if r["enrollable"]]
