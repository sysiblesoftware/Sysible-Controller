"""Sysible Relay — the bastion / jump-box SSH transport.

WHAT IT DOES. Administer hosts the controller cannot reach directly: instead of
dialling the host, it opens a single-hop SSH ProxyJump to a bastion, and the
on-bastion relay daemon forwards to permitted internal targets. At the transport
level that is one `ssh -J` (or one paramiko `direct-tcpip` channel), which is why
it drops into every SSH path here without a second connection model.

WHO GETS ROUTED. Either a host that explicitly opted in (`relay` on its record),
or one matching the auto-route allowlist — the CIDRs / domain suffixes BEHIND the
bastion. With no allowlist set, ONLY explicit opt-in routes, so turning the relay
on never silently re-routes a fleet that was already reachable directly.

WHERE THE CONFIG LIVES. The `relay_config` table (single row, the same shape as
`controller_config`), with the matching `SYSIBLE_RELAY_*` environment variables
as the fallback — so a compose deployment can bake it in and the console can
still change it without a recreate. The DB wins when set.

WHAT IS NOT HERE. The relay DAEMON runs on the bastion and ships from the
Sysible-Relay repository; this module never runs it. It can hand an admin the
jump-box setup script (`backend/bastion/`) so the bastion is never configured by
hand.

FAIL SAFE, NOT FAIL OPEN — with one deliberate exception. Transport resolution
never raises: a malformed config degrades to "connect directly" rather than
breaking connection setup for the whole fleet. That is the safe direction at
CONNECT time, but it is the dangerous one at SAVE time, where silently not
routing means going AROUND the bastion. So `validate_config` rejects a
configuration that could not work, loudly, before it is stored.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Any

# The relay's id. Stable, and shared with the Enterprise edition so the bastion
# setup scripts and the docs describe one thing.
RELAY_ID = "sysible-relay"
RELAY_NAME = "Sysible Relay"
DEFAULT_RELAY_USER = "sysible-relay"

# Environment fallbacks, one per config field. Same names as Enterprise, so a
# runbook written against one edition works on the other.
ENV_KEYS = {
    "relay_host": "SYSIBLE_RELAY_HOST",
    "relay_user": "SYSIBLE_RELAY_USER",
    "relay_port": "SYSIBLE_RELAY_PORT",
    "relay_identity": "SYSIBLE_RELAY_IDENTITY",
    "route_allowlist": "SYSIBLE_RELAY_ALLOWLIST",
    "relay_os": "SYSIBLE_RELAY_OS",
}

# Truthy per-host opt-in tokens accepted in ``host["relay"]``.
_TRUE_TOKENS = frozenset({"true", "1", "yes", "on", "enable", "enabled"})

# A single ProxyJump hop: optional ``user@``, a host (IPv4/hostname, or a
# bracketed IPv6 literal), optional ``:port``. No shell metacharacters, no
# leading '-'. remote_routes re-validates before handing anything to ssh -J /
# paramiko; we validate HERE too so this module never emits a value that could
# be misread as an ssh option even if a caller forgets.
_JUMP_HOP_RE = re.compile(
    r"^(?:[A-Za-z0-9._%+\-]+@)?"                       # optional user@
    r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9._\-]+)"          # host: [ipv6] or name/ipv4
    r"(?::[0-9]{1,5})?$"                               # optional :port
)

# Shell/option metacharacters refused in a key path handed to an ssh invocation.
_IDENTITY_BAD = set(" \t\r\n;|&$`<>()\"'*?!\\")


class RelayConfigError(ValueError):
    """A submitted relay configuration cannot work. Carries an operator-facing
    message naming the actual fix."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _env(field: str) -> str:
    return (os.getenv(ENV_KEYS[field], "") or "").strip()


def config() -> dict:
    """The effective relay config: the stored row where a field is set, else the
    matching environment variable, else the default."""
    row = {}
    try:
        from backend import db
        row = db.get_relay_config() or {}
    except Exception:
        row = {}

    def pick(field):
        v = str(row.get(field) or "").strip()
        return v or _env(field)

    return {
        "relay_host": pick("relay_host"),
        "relay_user": pick("relay_user") or DEFAULT_RELAY_USER,
        "relay_port": _coerce_port(pick("relay_port")),
        "relay_identity": pick("relay_identity") or _default_identity(),
        "route_allowlist": pick("route_allowlist"),
        "relay_os": (pick("relay_os") or "auto").lower(),
    }


def _default_identity() -> str:
    """The conventional controller relay key. Defaulted rather than left blank so
    the turnkey path needs no path typed into the form — the key is minted on
    first use at exactly this location."""
    try:
        from backend.relay_keys import DEFAULT_RELAY_KEY
        return DEFAULT_RELAY_KEY
    except Exception:
        return ""


def configured() -> bool:
    """True when a bastion is set. There is no separate enable switch: an unset
    relay_host IS "off", which removes a state where the relay looks configured
    but silently routes nothing."""
    try:
        return bool(config().get("relay_host"))
    except Exception:
        return False


def get_config(relay_id: str | None = None) -> dict:
    """Config for a relay id. Only ours resolves."""
    if relay_id and str(relay_id).strip() != RELAY_ID:
        return {}
    return config()


def save_config(updates: dict, actor: str = "system") -> dict:
    """Validate and store relay settings. Validation runs on the POST-APPLY view,
    so a change to one field is judged against the others it will be used with."""
    from backend import db
    current = config()
    merged = dict(current)
    for field in ENV_KEYS:
        if field in (updates or {}):
            merged[field] = _clean((updates or {}).get(field))
    validate_config(merged)
    db.set_relay_config({f: merged.get(f) for f in ENV_KEYS}, actor)
    return config()


def ensure_identity() -> str:
    """Make sure the configured relay private key exists, and return its path.
    Never regenerates an existing key — a bastion has already authorized the
    public half, so minting a new one would lock the controller out of every host
    behind it."""
    path = _clean(config().get("relay_identity"))
    if not path:
        return ""
    try:
        from backend.relay_keys import ensure_ed25519_key
        ensure_ed25519_key(path, RELAY_ID)
    except Exception:
        pass
    return path


def public_key() -> str | None:
    """The OpenSSH PUBLIC key line a bastion must authorize, or None.

    Tries ``<identity>.pub`` first (the normal ssh-keygen layout), then derives it
    from the private key. Only ever returns PUBLIC key material — the private key
    is never read into a response."""
    path = _clean(config().get("relay_identity"))
    if not path:
        return None
    try:
        if os.path.isfile(path + ".pub"):
            with open(path + ".pub", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
    except OSError:
        pass
    try:
        from cryptography.hazmat.primitives import serialization as _ser
        with open(path, "rb") as fh:
            key = _ser.load_ssh_private_key(fh.read(), password=None)
        return (key.public_key().public_bytes(
            _ser.Encoding.OpenSSH, _ser.PublicFormat.OpenSSH).decode()
            + f" {RELAY_ID}")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def resolve_ssh_proxy(host: Any) -> dict | None:
    """Return ``{"jump": ..., "identity"?: ...}`` when *host* must be reached
    through the bastion, or ``None`` when it is directly reachable.

    Never raises: any error yields None. A relay fault must degrade to a direct
    connection, not break transport for hosts that never needed the relay.
    """
    try:
        host = dict(host or {})
        cfg = config()
        relay_host = _clean(cfg.get("relay_host"))
        if not relay_host:
            return None                       # Not configured — route nothing.
        if not should_route(host, cfg):
            return None

        user = _clean(cfg.get("relay_user")) or DEFAULT_RELAY_USER
        port = _coerce_port(cfg.get("relay_port"))
        jump = f"{user}@{relay_host}" if user else relay_host
        if port and port != 22:
            jump = f"{jump}:{port}"

        # Refuse to emit a jump we know is malformed (a relay_host with a space,
        # or a leading '-' ssh could read as an option). Fail safe: treat the
        # host as directly reachable rather than emit a bad spec.
        if not valid_jump(jump):
            return None

        out = {"jump": jump}
        # Only forward an identity path safe to hand to ssh. Existence is
        # re-checked by the caller; this is the shape check.
        identity = _safe_identity(cfg.get("relay_identity"))
        if identity:
            out["identity"] = identity
        return out
    except Exception:
        return None


def should_route(host: dict, cfg: dict | None = None) -> bool:
    """Route if the host explicitly opted in, or matches the allowlist. With no
    allowlist set, ONLY explicit per-host opt-in routes — so turning the relay on
    never silently re-routes a fleet that was reachable directly."""
    cfg = cfg if cfg is not None else config()
    if _opted_in((host or {}).get("relay")):
        return True
    cidrs, suffixes = parse_allowlist(cfg.get("route_allowlist"))
    if not cidrs and not suffixes:
        return False
    if _ip_in_cidrs((host or {}).get("ip"), cidrs):
        return True
    return _name_has_suffix((host or {}).get("name"), suffixes)


def _opted_in(relay_meta: Any) -> bool:
    """Per-host opt-in: ``host['relay']`` equals our id or is truthy."""
    if relay_meta is None or relay_meta is False:
        return False
    if relay_meta is True:
        return True
    token = str(relay_meta).strip().lower()
    return token == RELAY_ID or token in _TRUE_TOKENS


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
def all_relays() -> list:
    """The configured relay endpoints for the accountability inventory. Drop-in
    for the old ``plugins.all_relays()``; a single relay is supported, so this is
    a one- or zero-element list. Never raises."""
    try:
        cfg = config()
        relay_host = _clean(cfg.get("relay_host"))
        if not relay_host:
            return []
        spec: dict[str, Any] = {
            "id": RELAY_ID,
            "host": relay_host,
            "port": _coerce_port(cfg.get("relay_port")),
            "user": _clean(cfg.get("relay_user")) or DEFAULT_RELAY_USER,
            "os_hint": (_clean(cfg.get("relay_os")) or "auto").lower(),
        }
        identity = _clean(cfg.get("relay_identity"))
        if identity:
            spec["identity"] = identity
        return [spec]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Validation — the one place the relay is allowed to refuse
# --------------------------------------------------------------------------- #
def validate_config(cfg: dict) -> None:
    """Reject a relay configuration that cannot work, with a message naming the
    fix. ``cfg`` is the post-apply view, so a change to one field is judged
    against the others it will be used with.

    This is the one place the relay is deliberately strict. Everywhere else a bad
    value degrades to "no relay", which at connect time is safe; at save time it
    means the controller quietly reaches hosts AROUND the bastion an operator
    believes they are going through.
    """
    ident = _clean(cfg.get("relay_identity"))
    if ident and ident.lower().endswith(".pub"):
        # By far the most common mistake, and it fails as a silent auth error at
        # connect time. Catch it where the operator can see it.
        raise RelayConfigError(
            "The relay identity must be the PRIVATE key path, not the public key — "
            f"remove the '.pub' (use '{ident[:-4]}'). The controller authenticates "
            "to the relay with the private key.")
    if ident and not _safe_identity(ident):
        raise RelayConfigError(
            "The relay identity has characters that aren't allowed in a key path — "
            "no spaces, shell metacharacters, or a leading '-'.")

    host = _clean(cfg.get("relay_host"))
    if not host:
        return                                  # blank host = relay off
    user = _clean(cfg.get("relay_user")) or DEFAULT_RELAY_USER
    port = _coerce_port(cfg.get("relay_port"))
    jump = f"{user}@{host}"
    if port and port != 22:
        jump = f"{jump}:{port}"
    if not valid_jump(jump):
        raise RelayConfigError(
            "The relay host / user / port don't form a valid SSH endpoint. "
            "Use [user@]host[:port] with no spaces or shell metacharacters; "
            "bracket IPv6 literals (e.g. [2001:db8::1]).")

    # The relay is the jump box the controller connects THROUGH. Pointing it at
    # the controller itself makes every connection auth-fail against an account
    # that does not exist there — a confusing failure worth naming precisely.
    if _is_loopback_host(host):
        raise RelayConfigError(
            "The relay host is a loopback address. It must be the BASTION / jump "
            "box (the machine where you ran install-*-bastion), not the controller "
            "itself or localhost.")
    if host.strip().strip("[]").lower() in _controller_local_addresses():
        raise RelayConfigError(
            "The relay host is THIS controller's own address. A relay is the jump "
            "box the controller connects THROUGH — set it to the bastion's "
            "IP/hostname (the machine where you ran install-*-bastion).")


# --------------------------------------------------------------------------- #
# Bastion setup scripts — shipped in-tree so the controller can serve them and
# an admin never copies one to the jump box by hand.
# --------------------------------------------------------------------------- #
_BASTION_SCRIPTS = {
    "windows": "install-windows-bastion.ps1",
    "linux": "install-linux-bastion.sh",
}


def bastion_script(os_family: str) -> str | None:
    """The setup script text for 'windows' | 'linux', or None. Never raises."""
    name = _BASTION_SCRIPTS.get((os_family or "").strip().lower())
    if not name:
        return None
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bastion", name)
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _coerce_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 22
    return port if 1 <= port <= 65535 else 22


def valid_jump(jump: Any) -> bool:
    """True if *jump* is a syntactically valid single ProxyJump hop with an
    in-range port. Rejects a leading '-' (ssh could read it as an option) and,
    crucially, a ``:port`` outside 1..65535 — the regex's ``[0-9]{1,5}`` alone
    would happily accept ``host:99999``."""
    if not jump or not isinstance(jump, str) or jump.startswith("-"):
        return False
    if not _JUMP_HOP_RE.match(jump):
        return False
    if ":" in jump:
        # A bracketed IPv6 literal's inner colons end with ']', so its rsplit
        # tail is never all-digits and is correctly skipped here.
        tail = jump.rsplit(":", 1)[1]
        if tail.isdigit() and not (1 <= int(tail) <= 65535):
            return False
    return True


def valid_jump_chain(spec: Any) -> str | None:
    """Cleaned ``user@host:port[,...]`` chain if EVERY hop is well-formed, else
    None. This is the guard remote_routes applies to anything bound for ssh -J."""
    if not spec or not isinstance(spec, str):
        return None
    spec = spec.strip()
    if len(spec) > 512:
        return None
    hops = [h.strip() for h in spec.split(",")]
    if not hops or any(not valid_jump(h) for h in hops):
        return None
    return ",".join(hops)


def _safe_identity(path: Any) -> str:
    """*path* only if it is safe as an ssh identity: no leading '-', no
    whitespace, no shell metacharacters. Anything else -> "" so we emit no
    identity rather than a token that could be misread."""
    p = _clean(path)
    if not p or p.startswith("-"):
        return ""
    return "" if any(c in _IDENTITY_BAD for c in p) else p


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _controller_local_addresses() -> set:
    """Best-effort set of THIS host's own addresses/names, to catch an operator
    who set the relay to the controller instead of the bastion. Never raises and
    does no real network I/O — the UDP 'connect' only fixes routing, sends
    nothing."""
    addrs = {"127.0.0.1", "::1", "localhost"}
    try:
        hn = socket.gethostname()
        if hn:
            addrs.add(hn.lower())
            for info in socket.getaddrinfo(hn, None):
                sockaddr = info[4]
                if sockaddr and sockaddr[0]:
                    addrs.add(str(sockaddr[0]).lower())
    except Exception:
        pass
    for family, peer in ((socket.AF_INET, ("192.0.2.1", 9)),
                         (socket.AF_INET6, ("2001:db8::1", 9))):
        try:
            s = socket.socket(family, socket.SOCK_DGRAM)
            try:
                s.connect(peer)
                addrs.add(str(s.getsockname()[0]).lower())
            finally:
                s.close()
        except Exception:
            pass
    return {a for a in addrs if a}


def parse_allowlist(raw: Any):
    """Split an allowlist string into (cidr_networks, name_suffixes). Tokens that
    parse as an IP network become CIDRs; everything else is a case-insensitive
    domain/name suffix (a leading dot optional)."""
    cidrs, suffixes = [], []
    if not raw:
        return cidrs, suffixes
    for token in re.split(r"[\s,;]+", str(raw)):
        token = token.strip()
        if not token:
            continue
        try:
            cidrs.append(ipaddress.ip_network(token, strict=False))
            continue
        except ValueError:
            pass
        suffix = token.lower().lstrip(".")
        if suffix:                  # skip "." / ".." which would match ANY name
            suffixes.append(suffix)
    return cidrs, suffixes


def _ip_in_cidrs(ip: Any, cidrs) -> bool:
    if not ip or not cidrs:
        return False
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return any(addr in net for net in cidrs)


def _name_has_suffix(name: Any, suffixes) -> bool:
    if not name or not suffixes:
        return False
    lowered = str(name).strip().lower()
    return any(lowered == s or lowered.endswith("." + s) for s in suffixes)
