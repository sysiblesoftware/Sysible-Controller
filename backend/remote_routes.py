"""
Remote host management: enroll SSH hosts and run ad-hoc commands on
them. Mounted under /remote and protected by the shared API key
(see backend/app.py).

Design note: enrollment is a one-time, automated handoff from
password auth to key auth. Sysible keeps a single controller-wide
ed25519 keypair (generated lazily on first use, see
_ensure_controller_key()) instead of a key per host - that's the
"one time setup". /enroll-ssh then uses the host's password exactly
once, in memory, to install that key's public half on the target via
paramiko; the password itself is never persisted anywhere. From then
on, exec_remote() authenticates with the stored private key, so no
further manual key handling is needed for that host.

The private key is the one genuinely sensitive secret this module
manages - it lives at CONTROLLER_KEY_PATH, mode 600, root-only, same
convention as backend/auth.py's api_key.txt. Host metadata (name,
ip, user) is persisted to hosts.json.
"""

import io
import json
import os
import tempfile
import posixpath
import re
import select
import shlex
import stat as stat_module

# Ceiling for a single SFTP file download buffered into controller memory (getfo reads
# the whole file into RAM, then getvalue() copies it). Bounds the blast radius of a
# superuser fetching a huge/pseudo file. Override with SYSIBLE_SFTP_DOWNLOAD_MAX_BYTES.
try:
    _SFTP_DOWNLOAD_MAX_BYTES = int(os.getenv("SYSIBLE_SFTP_DOWNLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
except (TypeError, ValueError):
    _SFTP_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from backend.auth import require_superuser

from backend.models.remote_models import (
    AddHostRequest,
    EnrollSSHRequest,
    ExecRequest,
    TerminalResizeRequest,
    TerminalWriteRequest,
)
from backend.models.environment_models import SetEnvironmentRequest

router = APIRouter(prefix="/remote", tags=["remote"])

HOST_FILE = Path(os.getenv("SYSIBLE_HOSTS_FILE", "/opt/sysible/hosts.json"))

REMOTE_KEY_DIR = Path(os.getenv("SYSIBLE_REMOTE_KEY_DIR", "/opt/sysible/remote_keys"))
CONTROLLER_KEY_PATH = REMOTE_KEY_DIR / "controller_ed25519"
CONTROLLER_PUB_KEY_PATH = REMOTE_KEY_DIR / "controller_ed25519.pub"

_SSH_PUBLIC_KEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521)"
    r" [A-Za-z0-9+/=]+(\s+\S+)?$"
)


def _looks_like_ssh_public_key(key: str) -> bool:
    return bool(_SSH_PUBLIC_KEY_RE.match(key.strip()))


# =========================================================
# INTERACTIVE TERMINAL SESSIONS
#
# One real PTY-backed shell per host, kept open across requests so
# sudo prompts, vim, multi-step interactive sessions etc. all behave
# like a genuine terminal instead of the one-shot exec below. Sessions
# live only in this process's memory (keyed by host name) - they
# don't survive a controller restart, which is fine since the GUI
# re-opens one automatically the next time the operator selects that
# host's terminal.
# =========================================================
_TERMINAL_SESSIONS: dict[str, dict] = {}
_TERMINAL_SESSIONS_LOCK = threading.Lock()

TERMINAL_READ_CHUNK = 4096

# How long /terminal/read is allowed to block waiting for output before
# answering "nothing yet" anyway. This is what turns it into a real
# long-poll instead of a bare "drain whatever's buffered" call - new
# remote output now reaches the GUI the instant select() wakes up
# (typically milliseconds) rather than waiting for that side's next
# fixed-interval timer tick. Bounded so a quiet session still gets a
# prompt response (keeps "did the connection close" detection snappy)
# and so a route handler thread can't block indefinitely. Safe to do
# in a plain `def` FastAPI route - Starlette runs these in its worker
# thread pool, so blocking here doesn't stall the asyncio event loop.
TERMINAL_LONG_POLL_S = 0.5

# Sessions are keyed by an opaque session_id (not host name), so a single
# host can have several independent shells open at once. Each open()
# mints a new id; read/write/close address that id. The trade-off of
# per-session (vs per-host) state is that a GUI that dies without calling
# /close leaks its session here. The GUI long-polls /read continuously,
# so an *active* session's last_activity stays fresh; a dead one goes
# quiet and is reaped on the next open() after this timeout.
TERMINAL_IDLE_TIMEOUT_S = 180


def _get_terminal_session(session_id):
    with _TERMINAL_SESSIONS_LOCK:
        return _TERMINAL_SESSIONS.get(session_id)


def _touch_session(session):
    session["last_activity"] = time.time()


def _reap_idle_sessions():
    now = time.time()
    stale = []
    with _TERMINAL_SESSIONS_LOCK:
        for sid in list(_TERMINAL_SESSIONS):
            s = _TERMINAL_SESSIONS[sid]
            if now - s.get("last_activity", now) > TERMINAL_IDLE_TIMEOUT_S:
                stale.append(_TERMINAL_SESSIONS.pop(sid))
    for s in stale:
        _close_session(s)


def _close_session(session):
    try:
        session["channel"].close()
    except Exception:
        pass
    try:
        session["client"].close()
    except Exception:
        pass
    # Revoke this session's per-session key on the host (agent hosts only).
    _queue_ephemeral_revoke(session.get("revoke"))


def _reaper_loop():
    import time as _t
    while True:
        _t.sleep(60)
        try:
            _reap_idle_sessions()
        except Exception:
            pass
        try:
            _reap_pty_sessions()
        except Exception:
            pass


# Reap idle terminal sessions on a TIMER, not only when a new terminal opens.
# Otherwise a browser that dies abruptly (and whose cleanup RPC is lost) leaves
# an orphaned paramiko client + channel + transport thread alive for the whole
# process lifetime if no further terminal is ever opened. daemon=True so it
# never blocks shutdown; started once at import.
_REAPER_THREAD = threading.Thread(target=_reaper_loop, name="sysible-term-reaper", daemon=True)
_REAPER_THREAD.start()


# =========================================================
# PERSISTENT STORAGE
# =========================================================
def load_hosts():
    if HOST_FILE.exists():
        try:
            data = json.loads(HOST_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_hosts(hosts):
    """Atomic write (temp file + os.replace) so a crash / disk-full / concurrent
    writer can't truncate hosts.json into invalid JSON — which load_hosts would
    then read as {}, making the entire SSH-host inventory silently disappear.
    (A residual lost-update race remains between concurrent load->mutate->save
    mutators; it self-heals as the agent re-reports SSH state / the operator
    re-runs, whereas the truncation this fixes was permanent.)"""
    HOST_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(HOST_FILE.parent), prefix=".hosts-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(hosts, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, HOST_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# =========================================================
# CONTROLLER SSH KEY (one keypair, shared across all enrolled
# hosts - generated on first use, then reused forever)
# =========================================================
def _ensure_controller_key() -> str:
    """Make sure the controller's ed25519 keypair exists on disk and
    return the public key text. Generating it is the one "one-time
    setup" step this module needs - everything after this is
    automatic."""
    if CONTROLLER_KEY_PATH.exists() and CONTROLLER_PUB_KEY_PATH.exists():
        return CONTROLLER_PUB_KEY_PATH.read_text().strip()

    REMOTE_KEY_DIR.mkdir(parents=True, exist_ok=True)

    # ssh-keygen refuses to overwrite, so if only one half exists
    # (interrupted previous run) clear both and start clean.
    CONTROLLER_KEY_PATH.unlink(missing_ok=True)
    CONTROLLER_PUB_KEY_PATH.unlink(missing_ok=True)

    proc = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(CONTROLLER_KEY_PATH)],
        capture_output=True,
        text=True,
        timeout=30,  # never let a stuck ssh-keygen (e.g. blocked entropy) hang the worker
    )

    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=proc.stderr or "ssh-keygen failed"
        )

    os.chmod(CONTROLLER_KEY_PATH, 0o600)

    return CONTROLLER_PUB_KEY_PATH.read_text().strip()


# Controller-side SSH known_hosts backing trust-on-first-use (TOFU)
# verification of managed hosts. The FIRST time the controller connects to a
# host (SSH enrollment - where the operator supplied the password - or the
# agent-channel-authenticated key install) its host key is recorded here; every
# LATER connection is checked against that pinned key, so a host that presents a
# DIFFERENT key (a possible man-in-the-middle, or a rebuilt box) is refused
# instead of being silently re-trusted the way paramiko's AutoAddPolicy did.
# One file shared by every SSH path - the paramiko sites below and exec_remote's
# OpenSSH CLI (via -o UserKnownHostsFile) - so a key pinned by one is honoured
# by all. Same dir/permissions (0600, root-only) convention as the controller
# private key.
KNOWN_HOSTS_PATH = REMOTE_KEY_DIR / "known_hosts"


def _ensure_known_hosts_file():
    REMOTE_KEY_DIR.mkdir(parents=True, exist_ok=True)
    if not KNOWN_HOSTS_PATH.exists():
        KNOWN_HOSTS_PATH.touch()
    try:
        os.chmod(KNOWN_HOSTS_PATH, 0o600)
    except OSError:
        pass


def _forget_known_host(ip: str):
    """Drop any pinned host-key entry for `ip` from the TOFU known_hosts, so a
    legitimately rebuilt or reassigned machine can re-enroll at the same IP
    without tripping the changed-key check. Called when a host is removed.
    Entries are written unhashed (HashKnownHosts=no / paramiko's plain format),
    so a textual match on the first field is sufficient."""
    if not ip or not KNOWN_HOSTS_PATH.exists():
        return
    try:
        kept = []
        for line in KNOWN_HOSTS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            names = line.split(" ", 1)[0].split(",")
            if ip in names or f"[{ip}]:22" in names:
                continue
            kept.append(line)
        KNOWN_HOSTS_PATH.write_text("".join(l + "\n" for l in kept))
        os.chmod(KNOWN_HOSTS_PATH, 0o600)
    except OSError:
        pass


def _new_ssh_client():
    """A paramiko SSHClient wired for TOFU host-key verification against
    KNOWN_HOSTS_PATH instead of the old AutoAddPolicy() (which accepted any key,
    every time, with no verification - the MITM gap this closes).

    Behaviour: a host already in known_hosts is verified - paramiko raises
    BadHostKeyException and the connection is refused if the presented key
    doesn't match the pinned one. A first-seen host is pinned (its key recorded,
    0600) and allowed - trust on first use. The policy class is defined here
    rather than at module scope because paramiko is an optional import that may
    not be installed."""
    import paramiko

    class _PinOnFirstUse(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            client.get_host_keys().add(hostname, key.get_name(), key)
            try:
                client.save_host_keys(str(KNOWN_HOSTS_PATH))
                os.chmod(KNOWN_HOSTS_PATH, 0o600)
            except OSError:
                pass

    _ensure_known_hosts_file()
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(str(KNOWN_HOSTS_PATH))
    except OSError:
        pass
    client.set_missing_host_key_policy(_PinOnFirstUse())
    return client


@router.get("/controller-key")
def get_controller_key():
    """Public key text only - safe to display/copy in the GUI for
    advanced/manual installs (e.g. baking it into a host's image)."""
    return {"public_key": _ensure_controller_key()}


# =========================================================
# AGENT -> SSH TERMINAL AUTO-ENROLLMENT
#
# An agent already runs as root on its host and polls the controller
# for queued commands, but that command channel is one-shot (no
# interactive shell). To give every agent host a *real* terminal, the
# controller queues a single root command (built by
# agent_ssh_enable_command below) that installs the controller's own
# SSH public key into root's authorized_keys and reports whether an
# SSH server is actually running. If it is, the controller registers
# the host as an SSH connection too (register_agent_ssh_host) and
# Remote Administration shows it as "Agent + SSH" with a live PTY.
#
# Deliberately non-invasive: it never installs packages or starts
# services. If sshd isn't running it just reports SYSIBLE_SSHD=stopped
# and the controller leaves the host agent-only and records
# "sshd_missing" so the GUI can tell the operator to install/start it.
# The per-host state is tracked in agent_ssh_state.json (next to
# hosts.json) so enrollment doesn't re-fire the command on every
# heartbeat. See backend/app.py for where this is triggered/consumed.
# =========================================================
AGENT_SSH_MARKER = "SYSIBLE_SSHD="

_AGENT_SSH_STATE_FILE = HOST_FILE.parent / "agent_ssh_state.json"

# hosts.json and agent_ssh_state.json are both mutated with load->mutate->save
# and are reached from the heartbeat path (via _maybe_enroll_agent_ssh /
# _consume_ssh_enable_result), which FastAPI runs in a threadpool — so
# concurrent heartbeats otherwise lost each other's updates (atomic-write only
# stops truncation, not lost updates). Hold these across the whole
# load->mutate->save in every mutator. RLock: some mutators nest (a host edit
# that also touches SSH state). Single process, so a plain lock suffices.
_HOSTS_LOCK = threading.RLock()
_SSH_STATE_LOCK = threading.RLock()


def agent_ssh_enable_command(public_key: str) -> str:
    """Root shell one-liner the agent runs: install the controller key
    into root's authorized_keys (idempotently) and report whether an
    SSH server is up. Never touches packages or services."""
    # shlex.quote rather than the old strip-quotes-and-single-quote trick, to
    # match the enroll_ssh install path and be safe by construction regardless
    # of what's in the key (it's the controller's own pubkey, but defense in
    # depth costs nothing here).
    key = shlex.quote(public_key.strip())
    return (
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh; "
        f"grep -qxF {key} /root/.ssh/authorized_keys 2>/dev/null || "
        f"echo {key} >> /root/.ssh/authorized_keys; "
        "chmod 600 /root/.ssh/authorized_keys; "
        "if systemctl is-active --quiet sshd 2>/dev/null "
        "|| systemctl is-active --quiet ssh 2>/dev/null "
        "|| pgrep -x sshd >/dev/null 2>&1; then "
        f"echo {AGENT_SSH_MARKER}running; else echo {AGENT_SSH_MARKER}stopped; fi"
    )


def ssh_host_exists(name: str) -> bool:
    return name in load_hosts()


def _norm_ip(ip) -> str:
    return (ip or "").strip()


def _ip_owner(ip: str, exclude_name=None):
    """If `ip` is already managed - by another SSH host record or by an
    enrolled agent - return the name it's known by, else None. This is what
    makes it impossible to enroll the same physical machine twice (the same
    box reached two ways would otherwise show up as two separate rows). IP is
    the one identifier that pins the machine regardless of what name each
    path used."""
    ip = _norm_ip(ip)
    if not ip:
        return None
    for n, h in (load_hosts() or {}).items():
        if n != exclude_name and _norm_ip(h.get("ip")) == ip:
            return n
    try:
        from backend.db import list_agents
        for a in list_agents():
            owner = a.get("hostname") or a.get("host_id")
            if owner != exclude_name and _norm_ip(a.get("ip")) == ip:
                return owner
    except Exception:
        pass
    return None


def register_agent_ssh_host(name: str, ip: str, environment: str = ""):
    """Add/refresh an SSH host record for an agent host that now accepts
    the controller key, so Remote Administration can open a real
    terminal to it. Connects as root with the shared controller key,
    exactly like any manually-enrolled SSH host.

    The agent's own hostname is the canonical identity for the box, so if a
    manually-enrolled SSH host already exists at this IP under a DIFFERENT
    name, we do NOT delete it — see the security note below. Returns True if a
    record was written, False if it was skipped because the IP already belongs
    to another host."""
    with _HOSTS_LOCK:
        hosts = load_hosts()
        ip_n = _norm_ip(ip)
        # SECURITY: `ip` originates from the agent's own request body and is never
        # verified against the socket peer (agents are commonly NAT'd, so the peer
        # address isn't the agent's real IP). A malicious agent could therefore
        # report a VICTIM host's IP and, under the old "delete any record at this
        # IP" behaviour, erase or repoint the victim's inventory entry (fleet DoS
        # / mislabelling). So never delete or overwrite a DIFFERENT-named record
        # here: if this IP is already owned by another host, skip the auto-SSH
        # record and surface the conflict for an admin to resolve, rather than
        # silently destroying data. A same-name refresh (its own record, or the
        # host's IP changing) still updates in place.
        for n, h in hosts.items():
            if n != name and ip_n and _norm_ip(h.get("ip")) == ip_n:
                return False
        hosts[name] = {
            "ip": ip,
            "user": "root",
            "key_path": str(CONTROLLER_KEY_PATH),
            "environment": environment or "",
        }
        save_hosts(hosts)
        return True


def forget_agent_ssh_host(name: str = None, ip: str = None, match_all: bool = False):
    """Remove the auto-created SSH record for an agent host (and forget its
    pinned host key) when that agent is disenrolled. Without this the SSH
    record register_agent_ssh_host() created lingers in hosts.json as an
    orphan — showing up as a separate, usually 'Unassigned' host everywhere
    that lists merged hosts (fleet health, Sysible Connect, the tools).

    match_all=False (default) matches by record name OR IP — a lenient cleanup
    that also catches a host renamed after auto-enrollment. match_all=True
    requires BOTH name AND IP to match, for the security-sensitive
    delete-a-specific-agent path: a rogue agent that reused a valid host's
    hostname at a different IP must not be able to wipe the valid host's SSH
    record / un-pin its known_hosts key. Returns the number of records removed."""
    with _HOSTS_LOCK:
        hosts = load_hosts()
        ip_n = _norm_ip(ip) if ip else ""
        if match_all:
            victims = [
                n for n, h in hosts.items()
                if name and n == name and ip_n and _norm_ip(h.get("ip")) == ip_n
            ]
        else:
            victims = [
                n for n, h in hosts.items()
                if (name and n == name) or (ip_n and _norm_ip(h.get("ip")) == ip_n)
            ]
        for n in victims:
            removed = hosts.pop(n, None)
            if removed and removed.get("ip"):
                _forget_known_host(removed["ip"])
        if victims:
            save_hosts(hosts)
        return len(victims)


def sync_agent_ssh_environment(name: str = None, ip: str = None, environment: str = ""):
    """Keep an agent host's auto-created SSH record tagged with the same
    environment as the agent, so reassigning the agent's environment doesn't
    leave the SSH side stale (the merged view prefers the agent's value, but an
    out-of-sync SSH record still misleads anything that reads it directly).
    Matches by name first, then IP. No-op if there's no SSH record. Returns the
    number of records updated."""
    with _HOSTS_LOCK:
        hosts = load_hosts()
        ip_n = _norm_ip(ip) if ip else ""
        updated = 0
        for n, h in hosts.items():
            if (name and n == name) or (ip_n and _norm_ip(h.get("ip")) == ip_n):
                if h.get("environment", "") != (environment or ""):
                    h["environment"] = environment or ""
                    updated += 1
        if updated:
            save_hosts(hosts)
        return updated


def _load_agent_ssh_state():
    if _AGENT_SSH_STATE_FILE.exists():
        try:
            data = json.loads(_AGENT_SSH_STATE_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_agent_ssh_state(state):
    _AGENT_SSH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_AGENT_SSH_STATE_FILE.parent), prefix=".sshstate-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, _AGENT_SSH_STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_agent_ssh_state(host_id: str):
    """Return the recorded SSH-terminal auto-enroll state for an agent
    host: a dict like {"status": "pending"|"enabled"|"sshd_missing"|
    "error", "task_id": int} - or None if never attempted."""
    return _load_agent_ssh_state().get(host_id)


def get_all_agent_ssh_states():
    """Return {host_id: state} for EVERY agent from a SINGLE file read, so the
    dashboard roster indexes in-memory instead of calling get_agent_ssh_state()
    per host in a loop (which re-read and re-parsed the whole file each time —
    the O(N^2) that made GET /agents slow at fleet scale)."""
    return _load_agent_ssh_state()


def set_agent_ssh_state(host_id: str, value):
    with _SSH_STATE_LOCK:
        state = _load_agent_ssh_state()
        if value is None:
            state.pop(host_id, None)
        else:
            state[host_id] = value
        _save_agent_ssh_state(state)


# =========================================================
# HOST MANAGEMENT
# =========================================================
@router.post("/hosts", dependencies=[Depends(require_superuser)])
def add_host(body: AddHostRequest):
    from backend.edition import enforce_host_limit
    enforce_host_limit(body.name)

    owner = _ip_owner(body.ip, exclude_name=body.name)
    if owner:
        raise HTTPException(
            status_code=409,
            detail=f"{body.ip} is already managed as '{owner}'.")

    with _HOSTS_LOCK:
        hosts = load_hosts()
        hosts[body.name] = {
            "ip": body.ip,
            "user": body.user,
            "key_path": str(CONTROLLER_KEY_PATH),
            "environment": body.environment or ""
        }
        save_hosts(hosts)

    return {"added": True, "host": body.name}


@router.get("/hosts")
def list_hosts():
    return load_hosts()


@router.get("/agent-bundle")
def download_agent_bundle():
    """Mint a fresh one-time AGENT enrollment bundle (zip) for a trusted machine peer
    to install on a host it owns — e.g. SLEP enrolling the VMs it just built. This is
    the agent (pull) enrollment path: the target runs run_agent.sh from the zip and
    self-enrolls over its own outbound channel, so nothing here reaches into the host
    and no human superuser console token is needed (unlike POST /hosts, which is the
    Sysible-Connect SSH-transport path). Authenticated by the machine API key — the
    whole /remote router requires X-API-Key. Each call bakes a NEW single-use token,
    so a caller fetches one bundle PER host it enrolls."""
    from backend.db import get_controller_config
    from backend.agent_bundle import mint_agent_bundle, bundle_addresses
    config = get_controller_config()
    addresses = bundle_addresses(config)
    if not addresses:
        raise HTTPException(
            status_code=409,
            detail="The controller has no configured address, so an agent bundle "
                   "can't be built. Set one in Controller Configuration first.")
    filename, zip_bytes = mint_agent_bundle(addresses, config["port"])
    return Response(
        content=zip_bytes, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/hosts/{name}", dependencies=[Depends(require_superuser)])
def delete_host(name: str):
    with _HOSTS_LOCK:
        hosts = load_hosts()
        removed = hosts.pop(name, None)
        save_hosts(hosts)
    # Forget the pinned SSH host key too, so a rebuilt box can re-enroll at the
    # same IP without a manual known_hosts edit.
    if removed and removed.get("ip"):
        _forget_known_host(removed["ip"])
    return {"deleted": True}


@router.delete("/hosts", dependencies=[Depends(require_superuser)])
def delete_all_hosts():
    """Forget EVERY standalone SSH host record (and un-pin each host key) in one shot —
    the "remove all SSH hosts" cleanup for phasing SSH connections out. This clears the
    SSH connection inventory only, including the legacy "Agent + SSH" shadow records;
    agent enrollments themselves are untouched (they live in a separate table). Superuser
    only. New agent enrollments no longer create SSH shadows, so this stays clear."""
    with _HOSTS_LOCK:
        hosts = load_hosts()
        names = list(hosts.keys())
        ips = [h.get("ip") for h in hosts.values() if isinstance(h, dict)]
        save_hosts({})
    for ip in ips:
        if ip:
            try:
                _forget_known_host(ip)
            except Exception:
                pass
    return {"deleted": len(names), "hosts": names}


@router.post("/hosts/{name}/environment", dependencies=[Depends(require_superuser)])
def set_host_environment(name: str, body: SetEnvironmentRequest):
    """Re-tag an already-connected SSH host's environment without
    re-running the connect flow - mirrors POST /agents/{host_id}/environment
    for agent hosts, so both host kinds use the same reassignment UX."""
    with _HOSTS_LOCK:
        hosts = load_hosts()

        if name not in hosts:
            raise HTTPException(status_code=404, detail="host not found")

        hosts[name]["environment"] = body.environment
        save_hosts(hosts)

    return {"host": name, "environment": body.environment}


# =========================================================
# CONNECT HOST VIA SSH PASSWORD (route kept as /enroll-ssh for
# compatibility - the GUI calls this "Connect Host", reserving
# "enroll" for the token-based host_agent flow) - one click, fully
# automated: the password is used exactly once (in memory) to
# install the controller's public key, then discarded. After this
# the host is reachable by exec_remote() with no further setup.
# =========================================================
@router.post("/enroll-ssh", dependencies=[Depends(require_superuser)])
def enroll_ssh(body: EnrollSSHRequest):
    from backend.edition import enforce_host_limit
    enforce_host_limit(body.name)

    # Refuse to enroll a machine that's already managed at this IP (by an
    # agent or another SSH host). Same physical box, two records = the
    # duplicate rows we want to make impossible.
    owner = _ip_owner(body.ip, exclude_name=body.name)
    if owner:
        raise HTTPException(
            status_code=409,
            detail=(f"{body.ip} is already managed as '{owner}'. Remove that host "
                    f"first if you want to re-enroll it under a different name."))

    public_key = _ensure_controller_key()

    if not _looks_like_ssh_public_key(public_key):
        raise HTTPException(status_code=500, detail="generated controller key looks malformed")

    try:
        import paramiko
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="paramiko is not installed - password-based SSH enrollment is unavailable"
        )

    client = _new_ssh_client()

    quoted_key = shlex.quote(public_key)

    install_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"echo {quoted_key} >> ~/.ssh/authorized_keys && "
        "sort -u -o ~/.ssh/authorized_keys ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys"
    )

    try:
        client.connect(
            body.ip,
            username=body.username,
            password=body.password,
            timeout=10,
            banner_timeout=15, auth_timeout=15,
        )

        stdin, stdout, stderr = client.exec_command(install_cmd)
        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            raise HTTPException(status_code=400, detail=stderr.read().decode(errors="replace"))

    except paramiko.BadHostKeyException as e:
        # This IP is already pinned in known_hosts with a DIFFERENT key than the
        # host is now presenting. Either the IP now points at a different
        # machine (rebuilt/reassigned), or someone is intercepting the
        # connection. Refuse rather than silently re-trust.
        raise HTTPException(
            status_code=409,
            detail=(f"Host key for {body.ip} does not match the key pinned on first "
                    f"contact - possible man-in-the-middle, or the machine at this IP "
                    f"was rebuilt/reassigned. If you trust the change, remove the pinned "
                    f"entry for this IP from {KNOWN_HOSTS_PATH} and enroll again. ({e})"))
    except paramiko.AuthenticationException:
        raise HTTPException(status_code=401, detail="SSH authentication failed")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Could not reach host: {e}")
    except paramiko.SSHException as e:
        # Covers anything else paramiko can raise that isn't a plain
        # connectivity (OSError) or auth (AuthenticationException)
        # problem - e.g. protocol negotiation failure, wrong port
        # answering with a non-SSH service. Without this it would
        # propagate as an unhandled 500 with no useful detail.
        raise HTTPException(status_code=400, detail=f"SSH error: {e}")
    finally:
        client.close()

    # Key is installed and working - now persist the host record so
    # exec_remote() (and the GUI's host list) knows about it.
    with _HOSTS_LOCK:
        hosts = load_hosts()
        hosts[body.name] = {
            "ip": body.ip,
            "user": body.username,
            "key_path": str(CONTROLLER_KEY_PATH),
            "environment": body.environment or ""
        }
        save_hosts(hosts)

    return {"enrolled": True, "host": body.name}


# =========================================================
# SSH EXECUTION (key-based - the target must already have the
# controller's public key installed via /enroll-ssh or out-of-band)
# =========================================================
# "This failed because it needs more privilege" — mirrors the agent's
# _looks_like_privilege_error so SSH dispatch escalates the SAME, safe way (retry
# under sudo ONLY on a genuine privilege error, never on an ordinary failure).
_SSH_PRIV_ERR = re.compile(
    r"permission denied|operation not permitted|must be run as root|must be root|"
    r"not in the sudoers|a terminal is required|no tty present|password is required|"
    r"not allowed to execute|are not allowed|superuser privileges|run with superuser|"
    r"unless you are root",  # pacman (Arch): "you cannot perform this operation unless you are root"
    re.I)


def _ssh_argv(key_path, target, remote_cmd):
    return [
        "ssh", "-i", key_path,
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}",
        "-o", "HashKnownHosts=no", "-o", "ConnectTimeout=10",
        # "--" ends option parsing so a `target` like "-oProxyCommand=..." can't be
        # read as an ssh option (which would run code on THIS controller as root).
        # Defence-in-depth behind the charset validation on user/ip at ingest — the
        # host record's user@ip flows straight into `target` here.
        "--", target, remote_cmd,
    ]


def _as_admin_remote(ssh_user: str, admin: str, cmd: str, elevate=False, password=False) -> str:
    """Remote command that runs `cmd` AS the initiating admin (per-user) — the
    same least-privilege model as agent hosts and the SSH terminal: `runuser`
    from a root SSH login, else the login user's `sudo -u`. `elevate` runs it
    under the admin's OWN sudo (for a privileged op; `-S` reads their password
    from stdin). Exits 126 with a clear message if the admin has no local account
    here, rather than silently running as the SSH login user."""
    u = shlex.quote(admin)
    inner_c = shlex.quote(cmd)
    if elevate:
        inner = f"sudo -S -p '' bash -c {inner_c}" if password else f"sudo -n bash -c {inner_c}"
    else:
        inner = f"bash -c {inner_c}"
    switch = f"runuser -u {u} -- {inner}" if ssh_user == "root" else f"sudo -n -u {u} -- {inner}"
    # NB: emit the username as its own shlex-quoted word ({u}), never the raw value —
    # interpolating {admin} into this echo would let a crafted username (e.g. one holding
    # $(...) / backticks) execute as the SSH login user (root) on hosts where the admin has
    # no local account. Admin usernames are also charset-validated at ingest; this is the
    # in-depth backstop.
    return (f"id {u} >/dev/null 2>&1 || {{ echo \"[sysible] user\" {u} \"does not exist on this "
            f"host - create it (with the sudo policy you want) so commands run as that role\" >&2; "
            f"exit 126; }}; {switch}")


@router.post("/hosts/{name}/exec")
def exec_remote(name: str, body: ExecRequest, request: Request):
    hosts = load_hosts()

    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")

    # Read-only auditors may NEVER run a command on a host — logged or not.
    # This block is unconditional (not gated on the client-supplied body.log):
    # a caller could otherwise set log=False to skip both this check AND the
    # audit record below and execute arbitrary commands as an auditor with no
    # trail. _reject_auditor is a no-op when no admin token is present, so the
    # genuine internal read-only sweeps (posture, fleet-health, user-list sync)
    # — which are dispatched tokenless — still pass through. Mirrors the agent
    # dispatch path's unconditional auditor block in app.py.
    _reject_auditor(request)

    # Activity feed: record admin-initiated SSH exec (identity from token),
    # unless this is a background/internal read (body.log=False, e.g. the
    # user-list sync) which isn't an operator action.
    admin = _resolve_admin_username(request)
    if admin and body.log:
        from backend.db import log_activity
        log_activity(admin, name, body.description or ("ran: " + body.cmd[:80]), body.cmd)

    host = hosts[name]
    ssh_user = host["user"]
    target = f"{ssh_user}@{host['ip']}"
    key_path = host.get("key_path") or str(CONTROLLER_KEY_PATH)

    # Share the one TOFU trust store with the paramiko paths above.
    _ensure_known_hosts_file()

    def _run(remote_cmd, stdin=None):
        # errors="replace": an SSH host can emit non-UTF-8 bytes (a binary/log
        # cat, a locale-encoded message). Strict decoding (the text=True default)
        # would raise UnicodeDecodeError and 500 the whole dispatch; replace keeps
        # it a normal result. Matches the other decode sites in this module.
        return subprocess.run(_ssh_argv(key_path, target, remote_cmd),
                              capture_output=True, text=True, errors="replace",
                              input=stdin, timeout=60)

    try:
        if admin:
            # Per-user (attributed) dispatch: run AS the initiating admin, then
            # escalate via THEIR own sudo only on a genuine privilege error —
            # exactly like the agent's _run_as_user (no unsafe blind retry). This
            # aligns SSH hosts with agent hosts: an operator is constrained by
            # their per-user account + sudo, not handed the SSH login user (root).
            result = _run(_as_admin_remote(ssh_user, admin, body.cmd))
            combined = (result.stderr or "") + "\n" + (result.stdout or "")
            if result.returncode not in (0, 126) and _SSH_PRIV_ERR.search(combined):
                if body.become_password:
                    result = _run(_as_admin_remote(ssh_user, admin, body.cmd, elevate=True, password=True),
                                  stdin=body.become_password + "\n")
                else:
                    result = _run(_as_admin_remote(ssh_user, admin, body.cmd, elevate=True))
        else:
            # Tokenless / internal (background reads, e.g. user-list sync, the
            # fleet-health/query probes) run as the SSH login user — the SSH
            # analogue of the agent's tokenless=root path.
            result = _run(body.cmd)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Command timed out")

    return {
        "host": name,
        "cmd": body.cmd,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "code": result.returncode,
    }


# =========================================================
# SSH INTERACTIVE TERMINAL (persistent PTY via invoke_shell - this is
# what the GUI's Terminal panel actually drives now, in place of the
# one-shot exec above, so sudo password prompts, vim, top, and other
# interactive programs all work as they would in a real terminal)
# =========================================================
def _become_user_command(ssh_user: str, target: str) -> str:
    """Shell command that turns the SSH login shell into an interactive
    shell for `target` (the controller admin's username), so the terminal
    runs as that user with their own host sudo rights - matching how
    dispatched commands run. From a root SSH session this is runuser (no
    password); from a non-root session it's that user's sudo. If `target`
    doesn't exist on the host, it falls back to a normal shell with a note
    rather than killing the session.

    The inner setup (run AS the target) gives the shell a valid HOME (the
    user's real home, or /tmp if it has none - many AD/role accounts don't),
    a UTF-8 locale (so readline is 8-bit clean and typed characters don't
    render as replacement boxes), and TERM, then exec's an interactive bash.
    This also avoids `runuser -l`'s noisy "cannot change directory to
    /home/<user>" warning when the home is missing."""
    t = shlex.quote(target)
    inner = (
        f'h=$(getent passwd {t} | cut -d: -f6); [ -n "$h" ] && [ -d "$h" ] || h=/tmp; '
        'cd "$h" 2>/dev/null || cd /tmp; export HOME="$h"; '
        'export TERM="${TERM:-xterm}"; export LANG="${LANG:-C.UTF-8}"; '
        'export LC_ALL="${LC_ALL:-$LANG}"; exec bash -i'
    )
    inner_q = shlex.quote(inner)
    if ssh_user == "root":
        switch = f"exec runuser -u {t} -- /bin/sh -c {inner_q}"
    else:
        switch = f"exec sudo -u {t} /bin/sh -c {inner_q}"
    # {t} is shlex-quoted; never interpolate the raw {target} here — a single quote in the
    # value would break out of this echo and run as the SSH login user (root).
    return (
        f"if id {t} >/dev/null 2>&1; then {switch}; "
        f"else echo '[sysible] user' {t} 'does not exist on this host - opening a "
        f"normal shell instead.'; exec bash -l 2>/dev/null || exec sh; fi"
    )


def _resolve_admin_username(request: Request):
    """The logged-in admin's username from their token, or None."""
    token = request.headers.get("X-Sysible-Admin-Token")
    if not token:
        return None
    from backend.db import resolve_admin_token
    admin = resolve_admin_token(token)
    return admin["username"] if admin else None


def _reject_auditor(request: Request):
    """Read-only 'auditor' accounts must never run a command or open a shell on
    an SSH host. The agent dispatch path already blocks this in backend/app.py;
    the SSH exec/terminal path resolves identity but never checked the role, so
    a token-bearing auditor could execute here. Enforce it controller-side too
    (defence-in-depth alongside the BFF's require_operator gate)."""
    token = request.headers.get("X-Sysible-Admin-Token")
    if not token:
        return
    from backend.db import resolve_admin_token
    admin = resolve_admin_token(token)
    if admin and admin.get("role") == "auditor":
        raise HTTPException(status_code=403, detail="Auditor accounts are read-only.")


# =========================================================
# AGENT-HOSTED PTY BRIDGE (terminals with NO inbound SSH)
#
# The agent only ever connects OUTBOUND to the controller, so on a fleet where
# the controller can't reach the host (NAT / firewall / non-routable IP) an SSH
# terminal — which needs controller→host connectivity — can't work at all.
# Instead the agent, already running on the host, opens the shell LOCALLY on a
# PTY and streams it to the controller over its existing outbound channel:
#   - the agent POSTs shell output up to  /agents/{id}/pty/{sid}/output
#   - the agent long-polls input/resize/close from /agents/{id}/pty/{sid}/io
# The controller buffers both directions here so the browser terminal endpoints
# (read/write/resize/close) drive it exactly like an SSH session.
# =========================================================
_PTY_LOCK = threading.Lock()
_PTY = {}   # session_id -> {host_id, out[], inq[], cols, rows, closed, ended, last}

# Cap the per-session output buffer. The agent streams shell output faster than
# a browser drains it (or a compromised agent floods it deliberately), and the
# buffer would otherwise grow without bound in controller RAM. Keep the most
# recent bytes (terminal scrollback semantics) and drop the oldest once over the
# cap, so a flood can't OOM the controller. Env-tunable.
try:
    _PTY_OUT_MAX = int(os.getenv("SYSIBLE_MAX_PTY_BUFFER_BYTES", str(4 * 1024 * 1024)))
except ValueError:
    _PTY_OUT_MAX = 4 * 1024 * 1024


def pty_create(host_id, cols=80, rows=24, owner=None):
    sid = uuid.uuid4().hex
    with _PTY_LOCK:
        _PTY[sid] = {"host_id": host_id, "out": [], "inq": [], "cols": cols, "rows": rows,
                     "closed": False, "ended": False, "last": time.time(), "owner": owner}
    return sid


def _terminal_owner(session_id):
    """The admin username that opened this terminal session (PTY or SSH), or
    None for a legacy/unowned session."""
    with _PTY_LOCK:
        s = _PTY.get(session_id)
        if s is not None:
            return s.get("owner")
    with _TERMINAL_SESSIONS_LOCK:
        s = _TERMINAL_SESSIONS.get(session_id)
        return s.get("owner") if s else None


def _check_terminal_owner(session_id, request):
    """Bind a live terminal to the operator who opened it. session_ids never
    leave the BFF process today (uuid4, server-side), so this is defence-in-depth
    against a future path that routes terminal I/O with a user token or a leaked
    id: if a token is presented it MUST match the session's owner and must not be
    an auditor. A tokenless call (the current BFF pump, gated by the controller
    API key) is allowed — the API key is the trust boundary there."""
    owner = _terminal_owner(session_id)
    if owner is None:
        return
    token = request.headers.get("X-Sysible-Admin-Token")
    if not token:
        return
    from backend.db import resolve_admin_token
    admin = resolve_admin_token(token)
    if not admin or admin.get("role") == "auditor" or admin.get("username") != owner:
        raise HTTPException(status_code=403,
                            detail="This terminal session belongs to another operator.")


def pty_is_session(session_id):
    with _PTY_LOCK:
        return session_id in _PTY


def pty_push_output(session_id, host_id, data, ended=False):
    """Agent -> controller: append shell output. Returns True if the browser has
    closed the session, which tells the agent to stop and kill the shell."""
    with _PTY_LOCK:
        s = _PTY.get(session_id)
        if not s or s["host_id"] != host_id:
            return True
        s["last"] = time.time()
        if data:
            s["out"].append(data)
            # Bound the buffer: if it has outgrown the cap (browser not draining,
            # or a flood), coalesce and keep only the most recent bytes.
            total = sum(len(c) for c in s["out"])
            if total > _PTY_OUT_MAX:
                s["out"] = ["".join(s["out"])[-_PTY_OUT_MAX:]]
        if ended:
            s["ended"] = True
        return s["closed"]


def pty_read_output(session_id):
    """Browser read: drain buffered output; closed once the shell ended or the
    session was closed. Returns None if this isn't a PTY session."""
    with _PTY_LOCK:
        s = _PTY.get(session_id)
        if not s:
            return None
        s["last"] = time.time()
        data = "".join(s["out"])
        s["out"] = []
        return {"data": data, "closed": s["closed"] or s["ended"]}


def pty_queue_input(session_id, msg):
    """Browser -> controller: queue a keystroke/resize/close for the agent."""
    with _PTY_LOCK:
        s = _PTY.get(session_id)
        if not s:
            return False
        s["last"] = time.time()
        if msg.get("t") == "close":
            s["closed"] = True
        else:
            s["inq"].append(msg)
        return True


def pty_take_input(session_id, host_id, wait=25.0):
    """Agent long-poll: return queued input/resize msgs (and the closed flag),
    waiting briefly for something to arrive so typing stays responsive."""
    import time as _t
    deadline = _t.time() + wait
    while True:
        with _PTY_LOCK:
            s = _PTY.get(session_id)
            if not s or s["host_id"] != host_id:
                return [], True
            if s["inq"] or s["closed"]:
                msgs = s["inq"]
                s["inq"] = []
                return msgs, s["closed"]
        if _t.time() >= deadline:
            return [], False
        _t.sleep(0.08)


def _reap_pty_sessions():
    now = time.time()
    with _PTY_LOCK:
        for sid in list(_PTY):
            s = _PTY[sid]
            if s["ended"] or (now - s["last"]) > TERMINAL_IDLE_TIMEOUT_S:
                s["closed"] = True   # signal the agent to stop
                # Drop the session (and its buffered output). Fast path: a cleanly
                # ended session goes 30s after it ended. Staleness path: a session
                # whose agent went offline BEFORE POSTing ended=True (it never
                # does) previously leaked forever — `closed` got set but the entry
                # was never popped. Now anything idle past the timeout + grace is
                # reaped regardless of `ended`, so a dead session can't accumulate.
                if (s["ended"] and (now - s["last"]) > 30) or (now - s["last"]) > TERMINAL_IDLE_TIMEOUT_S + 30:
                    _PTY.pop(sid, None)


# =========================================================
# EPHEMERAL, PER-SESSION SSH ACCESS (agent-provisioned terminals)
#
# For an AGENT host we don't rely on a standing controller key in the host's
# authorized_keys. Instead, each terminal open asks the agent (already root,
# already authenticated over its task channel) to install a throwaway key just
# for this session, we connect with it, and we ask the agent to revoke it when
# the session closes. This makes the terminal work as long as the agent is
# online — no dependency on a persistent SSH enrollment — and leaves no standing
# root credential on the host. A crash-safe backstop: each key line carries an
# expiry in its comment, and every grant prunes ephemeral lines whose expiry has
# passed, so a controller that dies mid-session can't leave a usable key behind.
# =========================================================
_EPHEMERAL_LINE_TTL = 6 * 3600     # seconds before an unrevoked key line is pruned
_EPHEMERAL_GRANT_TIMEOUT = 15.0    # seconds to wait for the agent to install the key


def _resolve_agent_target(name: str):
    """If `name` is an enrolled agent host (matched by host_id, else hostname),
    return (host_id, ip, hostname); else None. Agent hosts get just-in-time
    terminal access through the agent rather than a standing SSH key."""
    try:
        from backend.db import list_agents
        agents = list_agents()
    except Exception:
        return None
    by_name = None
    for a in agents:
        if a.get("host_id") == name and a.get("ip"):
            return a.get("host_id"), a.get("ip"), a.get("hostname")
        if by_name is None and a.get("hostname") == name and a.get("ip"):
            by_name = (a.get("host_id"), a.get("ip"), a.get("hostname"))
    return by_name


def _generate_ephemeral_keypair():
    """Throwaway ed25519 keypair (the controller's own key type, which hosts
    already accept). Returns (paramiko_pkey, pub_body) where pub_body is
    'ssh-ed25519 AAAA…' with no comment. The private key is written to a 0700
    temp dir only long enough for paramiko to load it, then removed — it never
    persists and lives only in the session's memory afterwards."""
    import paramiko
    import tempfile
    import shutil
    d = tempfile.mkdtemp(prefix="sysible-eph-")
    try:
        os.chmod(d, 0o700)
        kp = os.path.join(d, "k")
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", kp],
                       check=True, timeout=30, capture_output=True)
        pkey = paramiko.Ed25519Key.from_private_key_file(kp)
        with open(kp + ".pub") as f:
            parts = f.read().split()
        return pkey, parts[0] + " " + parts[1]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _ephemeral_grant_command(user: str, pub_line: str) -> str:
    """Root sh one-liner that installs this session's key into <user>'s
    authorized_keys — so the controller logs in AS that operator (not root), and
    a host with root SSH disabled is never in the way. The agent runs as root, so
    it can write into any user's ~/.ssh; it fixes ownership, perms, and (on
    SELinux hosts) the file context so sshd's StrictModes accepts the file.

    Prunes expired sysible-ephemeral entries first (comment ends -<expiry-epoch>),
    so a controller that died mid-session leaves nothing usable. Reports
    SYSIBLE_GRANT_NOUSER if the account doesn't exist on the host, or
    SYSIBLE_GRANT_OK once the key is verified present."""
    parts = pub_line.split()
    b64 = parts[2] if len(parts) > 2 else pub_line
    qu = shlex.quote(user)
    q = shlex.quote(pub_line)
    qb = shlex.quote(b64)
    awk = ("awk -v now=\"$now\" '/sysible-ephemeral-/"
           "{n=split($NF,a,\"-\");e=a[n];if(e ~ /^[0-9]+$/ && e+0<now)next}{print}'")
    return "\n".join([
        f'U={qu}',
        'h=$(getent passwd "$U" | cut -d: -f6)',
        '[ -n "$h" ] || { echo SYSIBLE_GRANT_NOUSER; exit 0; }',
        'D="$h/.ssh"; K="$D/authorized_keys"',
        'mkdir -p "$D"; touch "$K"',
        'now=$(date +%s)',
        'tmp=$(mktemp "$D/.ak.XXXXXX") || exit 1',
        f'{awk} "$K" > "$tmp"',
        'mv "$tmp" "$K"',
        f'printf "%s\\n" {q} >> "$K"',
        # Own it by the user (the agent is root, so fresh files are root-owned;
        # sshd StrictModes would then reject them) and restore the SELinux label.
        'g=$(id -gn "$U" 2>/dev/null || echo "$U")',
        'chown "$U":"$g" "$D" "$K" 2>/dev/null || chown "$U" "$D" "$K" 2>/dev/null || true',
        'chmod 700 "$D"; chmod 600 "$K"',
        'command -v restorecon >/dev/null 2>&1 && restorecon -R "$D" 2>/dev/null || true',
        # Confirm the key actually landed, so SYSIBLE_GRANT_OK is proof it's there.
        f'grep -qF {qb} "$K" && echo SYSIBLE_GRANT_OK || echo SYSIBLE_GRANT_FAIL',
    ])


def _ephemeral_revoke_command(user: str, tag: str) -> str:
    """Root sh one-liner: delete this session's key line from <user>'s
    authorized_keys (tag is hex-only, safe in a sed address)."""
    qu = shlex.quote(user)
    return ("\n".join([
        f'U={qu}',
        'h=$(getent passwd "$U" | cut -d: -f6)',
        f'[ -n "$h" ] && K="$h/.ssh/authorized_keys" && [ -f "$K" ] && sed -i "/{tag}/d" "$K"',
        'echo SYSIBLE_REVOKE_OK',
    ]))


def _run_agent_task_sync(host_id, command, kind, timeout):
    """Queue a root command on the agent and block (bounded) until it reports
    back. Returns {"status", "stdout", "stderr"}; status is 'done' on success,
    'timeout' if the agent didn't answer in time (offline / slow poll)."""
    from backend.db import queue_task, get_task_result
    tid = queue_task(host_id, command, kind=kind)   # run_as=None -> runs as the root agent
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = get_task_result(tid)
        if r and r.get("status") in ("done", "timed_out"):
            stdout = stderr = ""
            raw = r.get("result")
            if raw:
                try:
                    d = json.loads(raw)
                    if isinstance(d, dict):
                        stdout = d.get("stdout") or ""
                        stderr = d.get("stderr") or ""
                    else:
                        stdout = str(raw)
                except (ValueError, TypeError):
                    stdout = raw
            return {"status": r["status"], "stdout": stdout, "stderr": stderr}
        time.sleep(0.25)
    return {"status": "timeout", "stdout": "", "stderr": ""}


def _queue_ephemeral_revoke(revoke):
    """Best-effort: queue the revoke of a session's ephemeral key. If the agent
    is offline the task waits; the key's embedded expiry is the backstop."""
    if not revoke:
        return
    try:
        from backend.db import queue_task
        queue_task(revoke["host_id"],
                   _ephemeral_revoke_command(revoke.get("user", "root"), revoke["tag"]),
                   kind="ssh_revoke")
    except Exception:
        pass


@router.post("/hosts/{name}/terminal/open")
def open_terminal(name: str, request: Request):
    # An interactive shell is never a read-only action — auditors are blocked
    # controller-side (the BFF websocket also rejects them), defence-in-depth.
    _reject_auditor(request)
    # Each open mints a brand-new, independent session so a host can have
    # several shells at once; reap sessions abandoned by a dead GUI first.
    _reap_idle_sessions()

    # AGENT host → the agent hosts the shell locally and streams it over its own
    # outbound channel (no inbound SSH to the host at all). Create the bridge
    # session, ask the agent to attach, and return immediately — output starts
    # flowing as soon as the agent's next poll picks up the pty_open task.
    _agent_tgt = _resolve_agent_target(name)
    if _agent_tgt:
        host_id = _agent_tgt[0]
        # Reject the open if the agent isn't currently polling. Otherwise the
        # pty_open task queues, the offline agent never picks it up, and the
        # browser shows "Connected." then a dead cursor for minutes until the idle
        # reaper closes it — with no error. Fail fast with a clear reason instead.
        from backend.db import list_agents as _list_agents
        _rec = next((a for a in _list_agents() if a.get("host_id") == host_id), None)
        _ls = (_rec or {}).get("last_seen") or 0
        if time.time() - _ls > float(os.getenv("SYSIBLE_TERMINAL_ONLINE_WINDOW", "30")):
            raise HTTPException(
                status_code=503,
                detail="This host's agent isn't checking in right now, so an interactive "
                       "terminal can't attach. Try again once it's back online.")
        from backend.db import queue_task
        # Run the shell as the operator (their token identifies them), not root;
        # the agent falls back to a root shell only if that user doesn't exist.
        who = _resolve_admin_username(request) or ""
        session_id = pty_create(host_id, owner=(who or None))
        queue_task(host_id, json.dumps({"session_id": session_id, "user": who,
                                        "cols": 80, "rows": 24}), kind="pty_open")
        return {"host": name, "session_id": session_id, "opened": True, "via": "agent"}

    # Non-agent (pure SSH / Connect) host: the standing controller-key SSH path.
    try:
        import paramiko
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="paramiko is not installed - interactive terminal is unavailable"
        )
    hosts = load_hosts()
    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")
    host = hosts[name]
    ip = host["ip"]
    ssh_user = host.get("user", "root")
    connect_kwargs = {"key_filename": host.get("key_path") or str(CONTROLLER_KEY_PATH)}

    # Run the terminal as the controller admin (their token identifies them), so
    # it behaves like dispatched commands: as <admin> on the host with that
    # user's own sudo. Falls back to the SSH login shell when no admin identity
    # is presented or the admin is already the SSH user.
    admin_user = _resolve_admin_username(request)
    become = None
    if admin_user and admin_user != ssh_user:
        become = _become_user_command(ssh_user, admin_user)

    client = _new_ssh_client()
    session_id = uuid.uuid4().hex
    try:
        connect_kwargs.setdefault("banner_timeout", 15)
        connect_kwargs.setdefault("auth_timeout", 15)
        client.connect(ip, username=ssh_user, timeout=10, **connect_kwargs)
        # Keepalive on the interactive session: an idle PTY whose TCP is silently
        # dropped (NAT timeout, network blip) would otherwise linger until the 180s
        # idle reaper. Server-alive probes surface a dead peer within ~30s.
        try:
            _tr = client.get_transport()
            if _tr is not None:
                _tr.set_keepalive(30)
        except Exception:
            pass
        if become:
            channel = client.get_transport().open_session()
            channel.get_pty(term="xterm", width=120, height=32)
            channel.exec_command(become)
        else:
            channel = client.invoke_shell(term="xterm", width=120, height=32)
        channel.settimeout(0.0)  # non-blocking - /terminal/read polls instead of blocking
    except paramiko.BadHostKeyException as e:
        client.close()
        raise HTTPException(
            status_code=409,
            detail=(f"Host key for {ip} does not match the key pinned on first "
                    f"contact - possible man-in-the-middle, or the host was rebuilt. "
                    f"If you trust the change, remove its entry from {KNOWN_HOSTS_PATH} "
                    f"and reconnect. ({e})"))
    except paramiko.AuthenticationException:
        client.close()
        raise HTTPException(status_code=401, detail="SSH authentication failed")
    except OSError as e:
        client.close()
        raise HTTPException(status_code=400, detail=f"Could not reach host: {e}")
    except paramiko.SSHException as e:
        client.close()
        raise HTTPException(status_code=400, detail=f"SSH error: {e}")

    with _TERMINAL_SESSIONS_LOCK:
        _TERMINAL_SESSIONS[session_id] = {
            "client": client,
            "channel": channel,
            "lock": threading.Lock(),
            "name": name,
            "last_activity": time.time(),
            "owner": admin_user,
        }

    return {"host": name, "session_id": session_id, "opened": True}


@router.post("/terminal/{session_id}/write")
def write_terminal(session_id: str, body: TerminalWriteRequest, request: Request):
    _check_terminal_owner(session_id, request)
    if pty_is_session(session_id):
        pty_queue_input(session_id, {"t": "i", "d": body.data})
        return {"session_id": session_id, "written": len(body.data)}
    session = _get_terminal_session(session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="no open terminal session - call /terminal/open first"
        )

    _touch_session(session)
    with session["lock"]:
        try:
            session["channel"].send(body.data)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Could not write to terminal: {e}")

    return {"session_id": session_id, "written": len(body.data)}


@router.get("/terminal/{session_id}/read")
def read_terminal(session_id: str, request: Request):
    _check_terminal_owner(session_id, request)
    if pty_is_session(session_id):
        # Agent-hosted PTY: drain the controller-side output buffer, with a short
        # long-poll so an idle shell doesn't spin the browser's read loop.
        import time as _t
        deadline = _t.time() + TERMINAL_LONG_POLL_S
        while True:
            r = pty_read_output(session_id)
            if r is None:
                return {"session_id": session_id, "data": "", "closed": True}
            if r["data"] or r["closed"] or _t.time() >= deadline:
                return {"session_id": session_id, "data": r["data"], "closed": r["closed"]}
            _t.sleep(0.08)

    session = _get_terminal_session(session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="no open terminal session - call /terminal/open first"
        )

    _touch_session(session)
    channel = session["channel"]
    chunks = []

    # Wait for output to arrive WITHOUT holding the session lock.
    #
    # This wait must stay outside `session["lock"]`. The lock serializes
    # real channel I/O (the recv drain below and /terminal/write's
    # send()), but select() only watches for readability - it consumes
    # nothing - so it doesn't need the lock. Holding the lock across
    # this blocking wait was the bug behind "the SSH terminal blinks but
    # won't accept typing": the GUI runs a continuous, back-to-back read
    # loop, so this long-poll kept the lock held almost permanently,
    # leaving /terminal/write unable to acquire it to send a keystroke.
    # And because an idle shell emits no output, every wait ran the full
    # TERMINAL_LONG_POLL_S before releasing - while a keystroke can't
    # echo until it's been sent, and couldn't be sent until the read
    # released the lock, so the two sides starved each other.
    #
    # channel.recv_ready()/recv_stderr_ready() are non-mutating
    # readiness checks (safe unlocked), and channel.fileno() is
    # paramiko's internal event pipe made for exactly this kind of
    # select() wait, not a real socket.
    try:
        if not channel.recv_ready() and not channel.recv_stderr_ready():
            select.select([channel], [], [], TERMINAL_LONG_POLL_S)
    except OSError:
        pass

    with session["lock"]:
        try:
            while channel.recv_ready():
                chunks.append(channel.recv(TERMINAL_READ_CHUNK).decode(errors="replace"))
            while channel.recv_stderr_ready():
                chunks.append(channel.recv_stderr(TERMINAL_READ_CHUNK).decode(errors="replace"))
        except OSError:
            pass

    # Same transport-aliveness gap as /terminal/open's reuse check:
    # channel.closed alone misses a transport that died mid-session
    # (host rebooted, network dropped) without this side ever calling
    # .close() - that left the read loop above silently long-polling
    # forever with nothing to show for it instead of ever reporting
    # "closed" so the UI can surface "[remote session ended...]".
    transport = session["client"].get_transport()
    closed = (
        channel.closed
        or channel.exit_status_ready()
        or transport is None
        or not transport.is_active()
    )

    return {"session_id": session_id, "data": "".join(chunks), "closed": closed}


@router.post("/terminal/{session_id}/close")
def close_terminal(session_id: str, request: Request):
    _check_terminal_owner(session_id, request)
    if pty_is_session(session_id):
        pty_queue_input(session_id, {"t": "close"})   # agent kills the shell on its next poll
        return {"session_id": session_id, "closed": True}

    with _TERMINAL_SESSIONS_LOCK:
        session = _TERMINAL_SESSIONS.pop(session_id, None)

    if session is not None:
        _close_session(session)

    return {"session_id": session_id, "closed": True}


@router.post("/terminal/{session_id}/resize")
def resize_terminal(session_id: str, body: TerminalResizeRequest, request: Request):
    _check_terminal_owner(session_id, request)
    cols = max(8, min(500, body.cols))
    rows = max(4, min(300, body.rows))
    if pty_is_session(session_id):
        pty_queue_input(session_id, {"t": "r", "cols": cols, "rows": rows})
        return {"session_id": session_id, "cols": cols, "rows": rows}

    session = _get_terminal_session(session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="no open terminal session - call /terminal/open first"
        )

    _touch_session(session)
    with session["lock"]:
        try:
            session["channel"].resize_pty(width=cols, height=rows)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Could not resize terminal: {e}")

    return {"session_id": session_id, "cols": cols, "rows": rows}


# =========================================================
# FILE TRANSFER (SFTP - key-based, same controller key as exec/terminal
# above). Each call opens its own short-lived SSH+SFTP connection and
# closes it when done, rather than keeping one open like the terminal
# sessions do - uploads/downloads are one-shot, not an ongoing
# interactive session, so there's nothing to keep alive between calls.
# =========================================================
def _connect_sftp(name: str):
    """Open a fresh SSH connection + SFTP client for an enrolled SSH
    host, using the same stored controller key as exec_remote()/
    open_terminal() above - never password auth. Caller is responsible
    for closing both the returned sftp client and ssh client."""
    hosts = load_hosts()

    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")

    try:
        import paramiko
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="paramiko is not installed - file transfer is unavailable"
        )

    host = hosts[name]
    key_path = host.get("key_path") or str(CONTROLLER_KEY_PATH)

    client = _new_ssh_client()

    try:
        client.connect(
            host["ip"],
            username=host.get("user", "root"),
            key_filename=key_path,
            timeout=10,
            banner_timeout=15, auth_timeout=15,
        )
        sftp = client.open_sftp()
    except paramiko.BadHostKeyException as e:
        client.close()
        raise HTTPException(
            status_code=409,
            detail=(f"Host key for {host['ip']} does not match the key pinned on first "
                    f"contact - possible man-in-the-middle, or the host was rebuilt. "
                    f"If you trust the change, remove its entry from {KNOWN_HOSTS_PATH} "
                    f"and retry. ({e})"))
    except paramiko.AuthenticationException:
        client.close()
        raise HTTPException(status_code=401, detail="SSH authentication failed")
    except OSError as e:
        client.close()
        raise HTTPException(status_code=400, detail=f"Could not reach host: {e}")
    except paramiko.SSHException as e:
        client.close()
        raise HTTPException(status_code=400, detail=f"SSH error: {e}")

    return client, sftp


def _resolve_remote_upload_path(sftp, remote_path: str, filename: str) -> str:
    """If `remote_path` is an existing remote directory, upload into it
    under the original filename rather than failing/overwriting the
    directory itself - mirrors how every desktop SFTP client treats
    "drop a file onto a folder"."""
    remote_path = (remote_path or "").strip() or "."

    try:
        st = sftp.stat(remote_path)
    except (FileNotFoundError, OSError):
        return remote_path

    if stat_module.S_ISDIR(st.st_mode):
        # Basename the client-supplied filename so a crafted "../" can't place the
        # file outside the chosen directory on the remote host.
        safe = posixpath.basename((filename or "").strip()) or "uploaded_file"
        return posixpath.join(remote_path.rstrip("/") or "/", safe)

    return remote_path


@router.post("/hosts/{name}/files/upload", dependencies=[Depends(require_superuser)])
async def upload_file(
    name: str,
    request: Request,
    remote_path: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload one local file to an SSH-enrolled host over SFTP.
    `remote_path` may be a full destination path, or an existing
    remote directory (the original filename is appended in that
    case)."""
    client, sftp = _connect_sftp(name)

    try:
        data = await file.read()
        full_path = _resolve_remote_upload_path(
            sftp, remote_path, file.filename or "uploaded_file"
        )
        sftp.putfo(io.BytesIO(data), full_path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Upload failed: {e}")
    finally:
        sftp.close()
        client.close()

    # Attribute the transfer in the activity feed (identity from the token) — an
    # SFTP write as the SSH login user is a privileged, superuser-only action.
    admin = _resolve_admin_username(request)
    if admin:
        from backend.db import log_activity
        log_activity(admin, name, f"Uploaded file to {full_path} (SFTP)")

    return {"host": name, "uploaded": True, "remote_path": full_path, "size": len(data)}


@router.get("/hosts/{name}/files/download", dependencies=[Depends(require_superuser)])
def download_file(name: str, path: str, request: Request):
    """Download one file from an SSH-enrolled host over SFTP. Returns
    the raw bytes with a Content-Disposition header, same convention
    as the agent-bundle and portal-file-pool downloads in backend/app.py."""
    client, sftp = _connect_sftp(name)

    try:
        try:
            st = sftp.stat(path)
        except (FileNotFoundError, OSError):
            raise HTTPException(status_code=404, detail="Remote file not found")

        if stat_module.S_ISDIR(st.st_mode):
            raise HTTPException(status_code=400, detail="That remote path is a directory, not a file")
        # Require a REGULAR file — a device/pipe/proc pseudo-file (e.g. /dev/zero,
        # /proc/kcore) reports size 0 but streams unbounded, hanging the read.
        if not stat_module.S_ISREG(st.st_mode):
            raise HTTPException(status_code=400, detail="That remote path is not a regular file")
        # Bound the in-memory read so a huge remote file can't exhaust controller RAM
        # (getfo buffers the whole file, then getvalue() copies it again).
        _max = _SFTP_DOWNLOAD_MAX_BYTES
        if getattr(st, "st_size", 0) and st.st_size > _max:
            raise HTTPException(status_code=413,
                                detail=f"Remote file is {st.st_size} bytes, over the "
                                       f"{_max}-byte download limit.")

        buf = io.BytesIO()
        sftp.getfo(path, buf)
        data = buf.getvalue()
        if len(data) > _max:   # mis-declared size — cut it off
            raise HTTPException(status_code=413,
                                detail=f"Remote file exceeds the {_max}-byte download limit.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")
    finally:
        sftp.close()
        client.close()

    # Attribute the read in the activity feed (superuser-only SFTP as root).
    admin = _resolve_admin_username(request)
    if admin:
        from backend.db import log_activity
        log_activity(admin, name, f"Downloaded file {path} (SFTP)")

    filename = posixpath.basename(path.rstrip("/")) or "download"

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
