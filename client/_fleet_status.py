"""Fleet status gathering for the DESKTOP GUI: a health snapshot and a
read-only posture/compliance sweep, mirroring the web console's BFF aggregation
(webgui/server.py) but driven straight off client.api + the dispatch helpers so
the PySide6 dashboard has the same data the browser console does.

The probes are READ-ONLY and dispatched WITHOUT an admin token (tokenless), so
the controller runs them as the agent itself (root) - the same way the web
console gathers them. That means they don't need the viewing admin to have a
local account on every host, get the full (root-only) picture, and aren't
attributed as operator actions in the activity feed.

Pure Python (no Qt) so the parsing is unit-testable and the gather functions can
be called from a background QThread without touching the GUI.
"""
import concurrent.futures
import datetime
import time

from client import api
from client._api_dispatch import list_merged_hosts, run_on_entry, poll_entry_result

_POLL_TIMEOUT = 25.0   # seconds to wait for an agent task to report back


# ---------------------------------------------------------------------------
# Tokenless (root) read-only dispatch
# ---------------------------------------------------------------------------
def _dispatch_root(entry, command):
    """Run a read-only command on one merged-host entry AS ROOT (tokenless) and
    return {stdout, stderr, error}, polling an agent task to completion. Clears
    the password-sudo flag on a copy of the entry so run_on_entry doesn't
    fail-fast (these run as root, no become password needed)."""
    pe = {**entry, "requires_sudo_password": False}
    if entry.get("agent_entry"):
        pe["agent_entry"] = {**entry["agent_entry"], "requires_sudo_password": False}
    api.set_admin_token_override(None)   # no admin token -> controller runs as root
    try:
        outcome = run_on_entry(pe, command, kind="command", description=None)
    except Exception as e:
        return {"stdout": "", "stderr": "", "error": str(e)}
    finally:
        api.clear_admin_token_override()

    if outcome.get("error"):
        return {"stdout": "", "stderr": "", "error": outcome["error"]}
    if outcome.get("sync"):
        return {"stdout": outcome.get("stdout", ""), "stderr": outcome.get("stderr", ""),
                "error": outcome.get("error")}

    task_id = outcome.get("task_id")
    if task_id is None:
        return {"stdout": "", "stderr": "", "error": "failed to queue task"}
    deadline = time.time() + _POLL_TIMEOUT
    while time.time() < deadline:
        r = poll_entry_result(pe, task_id)
        if r is not None:
            return {"stdout": r.get("stdout", ""), "stderr": r.get("stderr", ""),
                    "error": r.get("error")}
        time.sleep(1.0)
    return {"stdout": "", "stderr": "", "error": "timed out waiting for host"}


# ---------------------------------------------------------------------------
# Parsers (kept identical in shape to webgui/server.py)
# ---------------------------------------------------------------------------
def parse_sysmetrics(text):
    """Parse the one-line `SYSMETRICS|k=v|...` snapshot into a dict, or None."""
    for line in (text or "").splitlines():
        if line.startswith("SYSMETRICS|"):
            d = {}
            for kv in line.split("|")[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    d[k] = v

            def num(k, cast):
                try:
                    return cast(d[k])
                except (KeyError, TypeError, ValueError):
                    return None
            units = d.get("units", "-")
            return {
                "verdict": d.get("verdict", "OK"),
                "disk": num("disk", int), "mount": d.get("mount", "/"),
                "mem": num("mem", int), "load1": num("load1", float),
                "cores": num("cores", int) or 1, "failed": num("failed", int) or 0,
                "uptime": num("uptime", int) or 0,
                "sysd": d.get("sysd", ""),
                "units": [] if units in ("-", "", None) else units.split(","),
                "oom": num("oom", int) or 0,
            }
    return None


def parse_posture(text):
    """Parse the `POSTURE|<cat>.<key>=<value>` stream into {category: {key: value}}."""
    flat = {}
    for line in (text or "").splitlines():
        if line.startswith("POSTURE|"):
            body = line[len("POSTURE|"):]
            if "=" in body:
                k, v = body.split("=", 1)
                flat[k.strip()] = v
    if not flat:
        return None
    nested = {}
    for k, v in flat.items():
        cat, _, sub = k.partition(".")
        nested.setdefault(cat, {})[sub or cat] = v
    return nested


_EOL_TABLE = {
    "ubuntu": {"16.04": "2021-04-30", "18.04": "2023-05-31", "20.04": "2025-05-31",
               "22.04": "2027-04-30", "24.04": "2029-05-31"},
    "debian": {"9": "2022-06-30", "10": "2024-06-30", "11": "2026-08-31", "12": "2028-06-30"},
    "centos": {"7": "2024-06-30", "8": "2021-12-31"},
    "rhel": {"7": "2024-06-30", "8": "2029-05-31", "9": "2032-05-31"},
    "rocky": {"8": "2029-05-31", "9": "2032-05-31"},
    "almalinux": {"8": "2029-05-31", "9": "2032-05-31"},
    "fedora": {"38": "2024-05-21", "39": "2024-11-12", "40": "2025-05-13", "41": "2025-11-19"},
    "opensuse-leap": {"15.4": "2023-12-31", "15.5": "2024-12-31", "15.6": "2025-12-31"},
    "sles": {"12": "2024-10-31", "15": "2031-07-31"},
}


def eol_status(distro, version):
    """True (EOL), False (supported), or None (unknown) for a distro/version."""
    if not distro or not version:
        return None
    table = _EOL_TABLE.get((distro or "").lower())
    if not table:
        return None
    eol = table.get(version) or table.get(version.split(".")[0])
    if not eol:
        return None
    try:
        return datetime.date.today().isoformat() > eol
    except Exception:
        return None


def posture_flags(p):
    """Curated high-ticket compliance signals: True (problem), False, or None."""
    if not p:
        return {}
    g = lambda c, k, d=None: (p.get(c) or {}).get(k, d)

    def as_int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    rl = (g("ssh", "permit_root_login") or "").strip().lower()
    ssh_root = (rl == "yes") if rl else None
    se = (g("mac", "selinux") or "").strip().lower()
    aa = (g("mac", "apparmor") or "").strip().lower()
    mac_off = (se != "enforcing" and aa != "enabled") if (se or aa) else None
    fw = g("fw", "active")
    sync = (g("time", "synced") or "").strip().lower()
    uid0 = as_int(g("users", "uid0_count"))
    emptypw = as_int(g("users", "empty_pw_count"))
    cert30 = as_int(g("cert", "expiring_30d"))
    return {
        "reboot_required": (g("reboot", "required") == "1") if g("reboot", "required") is not None else None,
        "ssh_root_login": ssh_root,
        "firewall_disabled": (fw == "0") if fw is not None else None,
        "mac_not_enforcing": mac_off,
        "eol_os": eol_status(g("os", "distro"), g("os", "version")),
        "risky_accounts": ((uid0 or 0) > 1 or (emptypw or 0) > 0)
        if (uid0 is not None or emptypw is not None) else None,
        "cert_expiring": (cert30 > 0) if cert30 is not None else None,
        "time_unsynced": (sync in ("no", "false", "0")) if sync else None,
    }


# The dashboard's high-ticket signal labels, in display order.
SIGNAL_LABELS = [
    ("reboot_required", "Reboot required"),
    ("ssh_root_login", "SSH root login enabled"),
    ("firewall_disabled", "Firewall disabled"),
    ("mac_not_enforcing", "SELinux/AppArmor not enforcing"),
    ("eol_os", "EOL / unsupported OS"),
    ("risky_accounts", "UID-0 / empty-password accounts"),
    ("cert_expiring", "TLS cert expiring < 30 days"),
    ("time_unsynced", "Time not synchronized"),
]


# ---------------------------------------------------------------------------
# Gatherers
# ---------------------------------------------------------------------------
def _last_seen():
    try:
        return {a.get("host_id"): a.get("last_seen") for a in api.get_agents()}
    except Exception:
        return {}


def _agent_id_of(e):
    if e["kind"] == "agent":
        return e["id"]
    if e["kind"] == "merged":
        return (e.get("agent_entry") or {}).get("id") or e["id"]
    return None


def gather_fleet_health(max_workers=12):
    """Per-host health snapshot (parsed cmd_metrics_snapshot). Offline agents are
    reported without probing. Returns a list of host dicts."""
    entries = list_merged_hosts(agent_only=False)
    last_seen = _last_seen()
    now = time.time()
    cmd = api.cmd_metrics_snapshot()

    def probe(e):
        base = {"id": e.get("id"), "host": e.get("label"),
                "environment": e.get("environment") or "Unassigned"}
        aid = _agent_id_of(e)
        ls = last_seen.get(aid) if aid else None
        online = (bool(ls and (now - ls) <= 20)) if aid else None
        if aid is not None and not online:
            return {**base, "online": False, "ok": False, "verdict": "OFFLINE", "error": "offline"}
        r = _dispatch_root(e, cmd)
        m = parse_sysmetrics((r.get("stdout") or "") + "\n" + (r.get("stderr") or ""))
        return {**base, "online": True if m else online,
                "ok": (not r.get("error")) and m is not None,
                "error": None if m else (r.get("error") or "no metrics returned"),
                **(m or {})}

    if not entries:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(entries)))) as ex:
        return list(ex.map(probe, entries))


def _posture_host(e, cmd, last_seen, now):
    base = {"id": e.get("id"), "host": e.get("label"),
            "environment": e.get("environment") or "Unassigned"}
    aid = _agent_id_of(e)
    ls = last_seen.get(aid) if aid else None
    online = (bool(ls and (now - ls) <= 20)) if aid else None
    if aid is not None and not online:
        return {**base, "online": False, "ok": False, "error": "offline",
                "posture": None, "flags": {}, "limited": False}
    r = _dispatch_root(e, cmd)
    p = parse_posture((r.get("stdout") or "") + "\n" + (r.get("stderr") or ""))
    limited = bool(p) and (p.get("meta") or {}).get("privileged") == "0"
    return {**base, "online": True if p else online,
            "ok": (not r.get("error")) and p is not None,
            "error": None if p else (r.get("error") or "no posture returned"),
            "posture": p, "flags": posture_flags(p), "limited": limited}


def gather_fleet_posture(max_workers=12):
    """Read-only posture sweep across the fleet. Returns a list of host dicts
    each carrying parsed posture, the high-ticket flags, and a 'limited' marker."""
    entries = list_merged_hosts(agent_only=False)
    last_seen = _last_seen()
    now = time.time()
    cmd = api.cmd_posture_snapshot()
    if not entries:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(entries)))) as ex:
        return list(ex.map(lambda e: _posture_host(e, cmd, last_seen, now), entries))


def gather_host_posture(host_id):
    """Full read-only posture for one host (the drill-down's Refresh)."""
    entries = list_merged_hosts(agent_only=False)
    entry = next((e for e in entries if str(e.get("id")) == str(host_id)), None)
    if entry is None:
        return None
    return _posture_host(entry, api.cmd_posture_snapshot(), _last_seen(), time.time())
