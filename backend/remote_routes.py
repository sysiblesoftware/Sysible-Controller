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
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from backend.auth import require_superuser

from backend.models.remote_models import (
    AddHostRequest,
    EnrollSSHRequest,
    ExecRequest,
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
    # Return name/ip/user/environment only — NOT key_path. The controller's private
    # SSH key path adds no operator value to the inventory view and needn't be
    # disclosed to every API-key holder (it hinted at the on-disk key location).
    # exec_remote/_connect_sftp read key_path straight from the local hosts.json
    # (load_hosts()), never from this response, so dropping it changes no behaviour.
    hosts = load_hosts()
    out = {}
    for name, h in hosts.items():
        if isinstance(h, dict):
            out[name] = {k: v for k, v in h.items() if k != "key_path"}
        else:
            out[name] = h
    return out


@router.get("/agent-bundle")
def download_agent_bundle(environment: str = ""):
    """Mint a fresh one-time AGENT enrollment bundle (zip) for a trusted machine peer
    to install on a host it owns — e.g. SLEP enrolling the VMs it just built. This is
    the agent (pull) enrollment path: the target runs run_agent.sh from the zip and
    self-enrolls over its own outbound channel, so nothing here reaches into the host
    and no human superuser console token is needed (unlike POST /hosts, which is the
    Sysible-Connect SSH-transport path). Authenticated by the machine API key — the
    whole /remote router requires X-API-Key. Each call bakes a NEW single-use token,
    so a caller fetches one bundle PER host it enrolls.

    `environment` (optional query param) stamps the bundle so the host lands directly
    in that Controller environment on enroll — this is how SLEP builds VMs straight
    into a chosen environment instead of "Unassigned". It's accepted only if it names a
    real, existing environment (an unknown value is ignored → the host stays
    unassigned rather than spawning a phantom group off a typo)."""
    from backend.db import get_controller_config, list_environments
    from backend.agent_bundle import mint_agent_bundle, bundle_addresses
    config = get_controller_config()
    addresses = bundle_addresses(config)
    if not addresses:
        raise HTTPException(
            status_code=409,
            detail="The controller has no configured address, so an agent bundle "
                   "can't be built. Set one in Controller Configuration first.")
    env = (environment or "").strip()
    if env:
        known = {(e.get("name") if isinstance(e, dict) else e) for e in list_environments()}
        if env not in known:
            env = ""   # ignore an unknown environment rather than creating a phantom group
    filename, zip_bytes = mint_agent_bundle(addresses, config["port"], environment=env)
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


def _internal_exec_allowed(request: Request) -> bool:
    """Whether a TOKENLESS exec is permitted — i.e. the caller is a genuine
    controller-internal principal, NOT an API-key holder coming in off the
    network. The co-located BFF reaches the backend over 127.0.0.1:9000 for its
    background probes (posture/metrics sweeps, user-list sync, scheduled jobs), so
    a loopback peer is trusted. uvicorn on :9000 runs WITHOUT proxy-header trust
    (docker/supervisord.conf), so request.client.host is the real socket peer and
    a LAN caller can't spoof it to loopback. A split BFF/backend topology (BFF on a
    different host) can opt in explicitly with SYSIBLE_REMOTE_INTERNAL_EXEC=1 —
    mirroring require_remote_file_access's SYSIBLE_REMOTE_FILE_API flag."""
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost"):
        return True
    return os.getenv("SYSIBLE_REMOTE_INTERNAL_EXEC", "").strip().lower() in ("1", "true", "yes")


def _authorize_exec(request: Request):
    """Authorize a /hosts/{name}/exec call and return the attributed admin
    username, or None for an authorized tokenless INTERNAL caller. Raises 401/403
    otherwise. Exec on an enrolled host is arbitrary command execution as the SSH
    login user (root), so authorization must match that blast radius:

      * a resolvable OPERATOR admin token (superuser or sysadmin) → the attributed
        path (runs AS that admin, per-user least privilege, audited). A read-only
        auditor is refused (403); an invalid/expired token is 401.
      * NO token → allowed ONLY for a genuine controller-internal caller
        (_internal_exec_allowed): the co-located BFF's background probes. Any other
        tokenless caller — e.g. an API-key holder hitting the LAN-exposed :9000 —
        is refused (403). This closes the previous "no token => run body.cmd as
        root, unattributed" bypass that any machine-key holder could reach.
    """
    token = request.headers.get("X-Sysible-Admin-Token")
    if token:
        from backend.db import resolve_admin_token
        admin = resolve_admin_token(token)
        if not admin:
            raise HTTPException(status_code=401, detail="Invalid or expired admin token")
        if admin.get("role") == "auditor":
            raise HTTPException(status_code=403, detail="Auditor accounts are read-only.")
        return admin["username"]
    if _internal_exec_allowed(request):
        return None
    raise HTTPException(
        status_code=403,
        detail="Remote command execution requires an operator login token.")


@router.post("/hosts/{name}/exec")
def exec_remote(name: str, body: ExecRequest, request: Request):
    hosts = load_hosts()

    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")

    # Authorize BEFORE doing anything (see _authorize_exec): an operator token, or
    # a genuine controller-internal tokenless caller — nothing else. Read-only
    # auditors are rejected regardless of the client-supplied body.log (a caller
    # could otherwise set log=False to slip past both the role check and the audit
    # write). The tokenless=root branch is no longer reachable by an API-key holder
    # off the network.
    admin = _authorize_exec(request)

    # Activity feed: record admin-initiated SSH exec (identity from the token),
    # unless this is a background/internal read (body.log=False, e.g. the user-list
    # sync) which isn't an operator action. Tokenless internal probes carry no admin.
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
# FILE TRANSFER (SFTP - key-based, same controller key as exec_remote()
# above). Each call opens its own short-lived SSH+SFTP connection and
# closes it when done - uploads/downloads are one-shot, not an ongoing
# interactive session, so there's nothing to keep alive between calls.
# =========================================================
def _connect_sftp(name: str):
    """Open a fresh SSH connection + SFTP client for an enrolled SSH
    host, using the same stored controller key as exec_remote()
    above - never password auth. Caller is responsible
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


def require_remote_file_access(request: Request):
    """Authorize file transfer over the /remote API. Default: a superuser admin token
    (the standing rule). F1 opt-in: when the operator sets SYSIBLE_REMOTE_FILE_API=1 on
    the controller, the machine API key alone is accepted (the /remote router already
    verified X-API-Key) — so a trusted machine peer like Sysible Connect can transfer
    files with its scoped key instead of a human superuser token. Off by default → no
    behaviour change."""
    tok = request.headers.get("X-Sysible-Admin-Token")
    if tok:
        require_superuser(x_admin_token=tok)   # raises unless a valid superuser
        return
    if os.getenv("SYSIBLE_REMOTE_FILE_API", "").lower() in ("1", "true", "yes"):
        return
    raise HTTPException(
        status_code=403,
        detail="File transfer requires a superuser session, or the machine-key file API "
               "to be enabled on the controller (set SYSIBLE_REMOTE_FILE_API=1).")


@router.post("/hosts/{name}/files/upload", dependencies=[Depends(require_remote_file_access)])
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


@router.get("/hosts/{name}/files/download", dependencies=[Depends(require_remote_file_access)])
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
