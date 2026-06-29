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
import posixpath
import re
import select
import shlex
import stat as stat_module
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


# =========================================================
# PERSISTENT STORAGE
# =========================================================
def load_hosts():
    if HOST_FILE.exists():
        try:
            return json.loads(HOST_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_hosts(hosts):
    HOST_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOST_FILE.write_text(json.dumps(hosts, indent=2))
    os.chmod(HOST_FILE, 0o600)


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
        text=True
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
    name, drop it - it's the same machine, and keeping both is the duplicate
    we're trying to prevent."""
    hosts = load_hosts()
    ip_n = _norm_ip(ip)
    for n in [n for n, h in hosts.items() if n != name and _norm_ip(h.get("ip")) == ip_n and ip_n]:
        del hosts[n]
    hosts[name] = {
        "ip": ip,
        "user": "root",
        "key_path": str(CONTROLLER_KEY_PATH),
        "environment": environment or "",
    }
    save_hosts(hosts)


def forget_agent_ssh_host(name: str = None, ip: str = None):
    """Remove the auto-created SSH record for an agent host (and forget its
    pinned host key) when that agent is disenrolled. Without this the SSH
    record register_agent_ssh_host() created lingers in hosts.json as an
    orphan — showing up as a separate, usually 'Unassigned' host everywhere
    that lists merged hosts (fleet health, Sysible Connect, the tools). Matches
    by record name first, then by IP, so it cleans up even if the host was
    renamed after auto-enrollment. Returns the number of records removed."""
    hosts = load_hosts()
    ip_n = _norm_ip(ip) if ip else ""
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
            return json.loads(_AGENT_SSH_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_agent_ssh_state(state):
    _AGENT_SSH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AGENT_SSH_STATE_FILE.write_text(json.dumps(state, indent=2))
    try:
        os.chmod(_AGENT_SSH_STATE_FILE, 0o600)
    except OSError:
        pass


def get_agent_ssh_state(host_id: str):
    """Return the recorded SSH-terminal auto-enroll state for an agent
    host: a dict like {"status": "pending"|"enabled"|"sshd_missing"|
    "error", "task_id": int} - or None if never attempted."""
    return _load_agent_ssh_state().get(host_id)


def set_agent_ssh_state(host_id: str, value):
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


@router.delete("/hosts/{name}", dependencies=[Depends(require_superuser)])
def delete_host(name: str):
    hosts = load_hosts()
    removed = hosts.pop(name, None)
    save_hosts(hosts)
    # Forget the pinned SSH host key too, so a rebuilt box can re-enroll at the
    # same IP without a manual known_hosts edit.
    if removed and removed.get("ip"):
        _forget_known_host(removed["ip"])
    return {"deleted": True}


@router.post("/hosts/{name}/environment", dependencies=[Depends(require_superuser)])
def set_host_environment(name: str, body: SetEnvironmentRequest):
    """Re-tag an already-connected SSH host's environment without
    re-running the connect flow - mirrors POST /agents/{host_id}/environment
    for agent hosts, so both host kinds use the same reassignment UX."""
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
            timeout=10
        )

        stdin, stdout, stderr = client.exec_command(install_cmd)
        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            raise HTTPException(status_code=400, detail=stderr.read().decode())

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
@router.post("/hosts/{name}/exec")
def exec_remote(name: str, body: ExecRequest, request: Request):
    hosts = load_hosts()

    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")

    # Activity feed: record admin-initiated SSH exec (identity from token),
    # unless this is a background/internal read (body.log=False, e.g. the
    # user-list sync) which isn't an operator action.
    admin = _resolve_admin_username(request)
    if admin and body.log:
        from backend.db import log_activity
        log_activity(admin, name, body.description or ("ran: " + body.cmd[:80]), body.cmd)

    host = hosts[name]
    target = f"{host['user']}@{host['ip']}"
    key_path = host.get("key_path") or str(CONTROLLER_KEY_PATH)

    # Share the one TOFU trust store with the paramiko paths above, rather than
    # using root's default ~/.ssh/known_hosts: a key pinned during enrollment or
    # a terminal session is then honoured here too (and vice versa).
    # accept-new = pin a first-seen host, but refuse a CHANGED key (exit 255).
    _ensure_known_hosts_file()

    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", key_path,
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}",
                "-o", "HashKnownHosts=no",
                "-o", "ConnectTimeout=10",
                target, body.cmd
            ],
            capture_output=True,
            text=True,
            timeout=60
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Command timed out")

    return {
        "host": name,
        "cmd": body.cmd,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "code": result.returncode
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
    return (
        f"if id {t} >/dev/null 2>&1; then {switch}; "
        f"else echo '[sysible] user {target} does not exist on this host - opening a "
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


@router.post("/hosts/{name}/terminal/open")
def open_terminal(name: str, request: Request):
    hosts = load_hosts()

    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")

    # Each open mints a brand-new, independent session - never reuse an
    # existing one - so a host can have several shells open at once.
    # Opportunistically reap sessions abandoned by a dead GUI first.
    _reap_idle_sessions()

    try:
        import paramiko
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="paramiko is not installed - interactive terminal is unavailable"
        )

    host = hosts[name]
    key_path = host.get("key_path") or str(CONTROLLER_KEY_PATH)
    ssh_user = host.get("user", "root")

    # Run the terminal as the controller admin (their token identifies them),
    # so it behaves like dispatched commands: as <admin> on the host, with
    # that user's own sudo. Falls back to the SSH login shell when no admin
    # identity is presented or the admin is already the SSH user.
    admin_user = _resolve_admin_username(request)
    become = None
    if admin_user and admin_user != ssh_user:
        become = _become_user_command(ssh_user, admin_user)

    client = _new_ssh_client()

    try:
        client.connect(
            host["ip"],
            username=ssh_user,
            key_filename=key_path,
            timeout=10,
        )
        if become:
            # Start the session as the admin user via an interactive PTY exec
            # rather than the SSH user's default shell.
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
            detail=(f"Host key for {host['ip']} does not match the key pinned on first "
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

    session_id = uuid.uuid4().hex
    with _TERMINAL_SESSIONS_LOCK:
        _TERMINAL_SESSIONS[session_id] = {
            "client": client,
            "channel": channel,
            "lock": threading.Lock(),
            "name": name,
            "last_activity": time.time(),
        }

    return {"host": name, "session_id": session_id, "opened": True}


@router.post("/terminal/{session_id}/write")
def write_terminal(session_id: str, body: TerminalWriteRequest):
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
def read_terminal(session_id: str):
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
def close_terminal(session_id: str):
    with _TERMINAL_SESSIONS_LOCK:
        session = _TERMINAL_SESSIONS.pop(session_id, None)

    if session is not None:
        _close_session(session)

    return {"session_id": session_id, "closed": True}


@router.post("/terminal/{session_id}/resize")
def resize_terminal(session_id: str, body: TerminalResizeRequest):
    session = _get_terminal_session(session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="no open terminal session - call /terminal/open first"
        )

    _touch_session(session)
    cols = max(8, min(500, body.cols))
    rows = max(4, min(300, body.rows))
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
        return posixpath.join(remote_path.rstrip("/") or "/", filename)

    return remote_path


@router.post("/hosts/{name}/files/upload")
async def upload_file(
    name: str,
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

    return {"host": name, "uploaded": True, "remote_path": full_path, "size": len(data)}


@router.get("/hosts/{name}/files/download")
def download_file(name: str, path: str):
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

        buf = io.BytesIO()
        sftp.getfo(path, buf)
        data = buf.getvalue()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")
    finally:
        sftp.close()
        client.close()

    filename = posixpath.basename(path.rstrip("/")) or "download"

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
