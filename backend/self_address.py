"""Self-recovery for the controller's own address.

The problem this solves, from a real install: the host's network dropped, it came
back on a different subnet with a new DHCP lease, and the controller carried on
advertising the dead one. Agents kept dialling an address nobody answers at, and
Connect — handed `https://<ip>:9000` in its .env at install time — kept doing the
same.

Nothing noticed, and the piece that should have (`db.get_controller_config`'s
`ip_stale`) cannot fire in the deployment that matters. It compares the saved IP
against `detect_local_ips()`, and inside a container that function returns ONLY
`SYSIBLE_CONTROLLER_ADDR` — baked in when the container was created, with the old
address. The saved IP always equals "detected", so the drift is invisible.

So this does not ask the box what its address is. It watches what address the
world actually reaches it at, which works in a container precisely because it
needs nothing from the container's own NICs:

  * every request that arrives carries the address the caller dialled, in Host
    (or X-Forwarded-Host, when the SLOP gateway is in front). An agent built from
    a bundle dials the baked-in address; a browser dials whatever the operator
    typed. Both are first-hand evidence that an address is live.
  * `observe()` tallies those, `assess()` decides, `heal()` acts.

The decision is deliberately slow and one-directional. db.py's own comment is the
reason: a controller is often reached at an address that is NOT a local NIC — NAT,
port-forward, bridge, VPN — and clobbering that strands every agent at once. So an
address is only replaced when the advertised one has gone silent for
STALE_AFTER and a candidate has been seen at least MIN_SIGHTINGS times from at
least MIN_SOURCES distinct peers spanning MIN_SPREAD. One stray request can never
move it.
"""
from __future__ import annotations

import ipaddress
import time

# How long the advertised address must go completely unseen before we will
# consider it dead. Agents heartbeat every ~1.5s (host_agent/agent.py's
# POLL_INTERVAL), so on any fleet at all this is many thousands of missed
# chances to be seen — while still riding out a reboot or a brief outage.
STALE_AFTER = 15 * 60

# What a replacement has to earn. Distinct peers matter more than raw count: one
# busy client retrying in a loop is one opinion, not fifty.
MIN_SIGHTINGS = 20
MIN_SOURCES = 2
MIN_SPREAD = 5 * 60

# Keep the tally small and current.
FORGET_AFTER = 24 * 60 * 60
MAX_TRACKED = 64


def _normalise(host: str) -> str:
    """The dialled address as a bare IPv4/IPv6 literal, or "" if it is not one.

    A name is rejected on purpose. The whole point of the IP-only bundle rule
    (agent_bundle.resolve_controller_addresses) is that a name assumes DNS is
    configured on every managed host; healing ONTO a name would reintroduce
    exactly the fragility the bundle format refuses.
    """
    h = (host or "").strip()
    if not h:
        return ""
    # "[::1]:9000" / "192.168.1.5:9000" / "192.168.1.5"
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        return ""


def is_routable_for_fleet(addr: str) -> bool:
    """Could a managed host plausibly reach us here?

    Loopback is the operator on the box itself. Link-local is an address that
    appeared because DHCP failed, which is the very failure we are recovering
    from — healing onto it would make things worse. Everything else, private or
    public, is fair game: a NAT'd controller's real address is public, and a LAN
    one's is RFC1918.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified
                or ip.is_multicast or ip.is_reserved)


def observe(store: dict, addr: str, peer: str, now: float | None = None) -> None:
    """Record that `peer` reached us at `addr`. Cheap enough for the hot path."""
    addr = _normalise(addr)
    if not addr or not is_routable_for_fleet(addr):
        return
    now = time.time() if now is None else now
    e = store.get(addr)
    if e is None:
        if len(store) >= MAX_TRACKED:
            _forget_oldest(store)
        e = store[addr] = {"first": now, "last": now, "count": 0, "peers": []}
    e["last"] = now
    e["count"] += 1
    # A bounded set of distinct peers, kept as a list so the whole store stays
    # JSON — it is persisted between restarts.
    if peer and peer not in e["peers"] and len(e["peers"]) < 16:
        e["peers"].append(peer)


def _forget_oldest(store: dict) -> None:
    oldest = min(store, key=lambda k: store[k]["last"])
    store.pop(oldest, None)


def prune(store: dict, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for addr in [a for a, e in store.items() if now - e["last"] > FORGET_AFTER]:
        store.pop(addr, None)


def assess(store: dict, advertised: str, now: float | None = None) -> dict:
    """Should the advertised address be replaced, and with what?

    Returns {"stale": bool, "candidate": str|"", "reason": str}. Pure — it reads
    the tally and nothing else, so the policy is testable without a controller.
    """
    now = time.time() if now is None else now
    adv = _normalise(advertised)

    seen = store.get(adv) if adv else None
    if seen and now - seen["last"] <= STALE_AFTER:
        return {"stale": False, "candidate": "",
                "reason": f"{adv} was reached {int(now - seen['last'])}s ago"}

    if not adv:
        return {"stale": False, "candidate": "", "reason": "no address is advertised"}

    # The advertised address is silent. Is anything else demonstrably working?
    best, best_count = "", 0
    for addr, e in store.items():
        if addr == adv:
            continue
        if now - e["last"] > STALE_AFTER:
            continue
        if e["count"] < MIN_SIGHTINGS or len(e["peers"]) < MIN_SOURCES:
            continue
        if e["last"] - e["first"] < MIN_SPREAD:
            continue
        if e["count"] > best_count:
            best, best_count = addr, e["count"]

    silence = "never seen" if not seen else f"silent for {int(now - seen['last'])}s"
    if not best:
        return {"stale": True, "candidate": "",
                "reason": f"{adv} is {silence}, and nothing else has earned replacing it"}
    return {"stale": True, "candidate": best,
            "reason": (f"{adv} is {silence}; {best} answered {best_count} times "
                       f"from {len(store[best]['peers'])} hosts")}


# ---------------------------------------------------------------- persistence
#
# The tally lives in the controller's own SQLite file so it survives a restart —
# otherwise every `sysiblectl controller restart` would reset the evidence and the
# MIN_SPREAD window could never close. One row, one JSON blob: this is a handful
# of addresses, not a time series, and keeping it opaque means no migration when
# the shape changes.
_TABLE = """
CREATE TABLE IF NOT EXISTS address_sightings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT
)
"""


def load(db) -> dict:
    import json
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(_TABLE)
        cur.execute("SELECT data FROM address_sightings WHERE id=1")
        row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return {}
    try:
        got = json.loads(row[0])
        return got if isinstance(got, dict) else {}
    except ValueError:
        return {}


def save(db, store: dict) -> None:
    import json
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(_TABLE)
        cur.execute("INSERT INTO address_sightings (id, data) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (json.dumps(store),))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------- acting
def heal(db, tls, config: dict, candidate: str) -> dict:
    """Move the controller onto `candidate`, and make the TLS cert say so.

    Order matters. The certificate is reissued FIRST: a config that advertises an
    address the cert does not cover is worse than the stale address we started
    with, because every agent then fails the TLS name check instead of merely
    dialling a dead host. If the reissue fails we leave the config alone and say
    why — a controller that is unreachable is recoverable, one that is reachable
    but untrusted looks like an attack.
    """
    old = config.get("ip") or ""
    note = ""
    try:
        if tls is not None and tls.current_is_self_signed():
            hostnames = [h for h in ("localhost",) if h]
            tls.regenerate_self_signed(hostnames=hostnames, ips=[candidate])
            note = "self-signed certificate reissued for " + candidate
        elif tls is not None:
            # An operator-installed certificate is never silently replaced — we
            # cannot mint one they would trust. Heal the address and say plainly
            # that the cert still needs their attention.
            note = ("the installed certificate still names the old address — "
                    "reissue it for " + candidate)
    except Exception as exc:  # noqa: BLE001
        return {"healed": False, "old": old, "new": candidate,
                "note": f"left the address alone: could not reissue TLS ({exc})"}

    db.set_controller_config(config.get("hostname") or "", candidate, "ip",
                             config.get("port") or 9000)
    return {"healed": True, "old": old, "new": candidate, "note": note}


def from_request(headers, client_host: str) -> tuple[str, str]:
    """(address dialled, peer that dialled it) for one request.

    Behind the SLOP gateway every request arrives from the gateway, so the direct
    peer is the same IP each time and MIN_SOURCES would never be met. Take the
    RIGHTMOST X-Forwarded-For hop — the one our own proxy appended — exactly as
    backend/app.py and the SLOP IdP already derive the client. Earlier hops are
    attacker-supplied, so a client could otherwise manufacture distinct peers and
    vote an address in by itself.
    """
    get = headers.get
    addr = (get("x-forwarded-host") or get("host") or "")
    xff = (get("x-forwarded-for") or "").split(",")[-1].strip()
    return addr, (xff or client_host or "")
