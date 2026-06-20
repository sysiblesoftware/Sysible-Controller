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
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from backend.models.remote_models import (
    AddHostRequest,
    EnrollSSHRequest,
    ExecRequest,
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


def _get_terminal_session(name):
    with _TERMINAL_SESSIONS_LOCK:
        return _TERMINAL_SESSIONS.get(name)


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


@router.get("/controller-key")
def get_controller_key():
    """Public key text only - safe to display/copy in the GUI for
    advanced/manual installs (e.g. baking it into a host's image)."""
    return {"public_key": _ensure_controller_key()}


# =========================================================
# HOST MANAGEMENT
# =========================================================
@router.post("/hosts")
def add_host(body: AddHostRequest):
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


@router.delete("/hosts/{name}")
def delete_host(name: str):
    hosts = load_hosts()
    hosts.pop(name, None)
    save_hosts(hosts)
    return {"deleted": True}


@router.post("/hosts/{name}/environment")
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
@router.post("/enroll-ssh")
def enroll_ssh(body: EnrollSSHRequest):
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

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

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
def exec_remote(name: str, body: ExecRequest):
    hosts = load_hosts()

    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")

    host = hosts[name]
    target = f"{host['user']}@{host['ip']}"
    key_path = host.get("key_path") or str(CONTROLLER_KEY_PATH)

    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", key_path,
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
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
@router.post("/hosts/{name}/terminal/open")
def open_terminal(name: str):
    hosts = load_hosts()

    if name not in hosts:
        raise HTTPException(status_code=404, detail="host not found")

    existing = _get_terminal_session(name)
    if existing is not None:
        existing_transport = existing["client"].get_transport()
        if not existing["channel"].closed and existing_transport is not None and existing_transport.is_active():
            return {"host": name, "opened": True, "reused": True}

        # Stale session - channel.closed only flips when *this* side
        # explicitly closed the channel; it says nothing about a dead
        # transport (remote reboot, network drop, idle timeout, or the
        # previous GUI process going away without ever reaching
        # /terminal/close - e.g. `sysible_controller stop`/a kill
        # instead of a clean host switch in the UI - leaves this exact
        # session sitting here, since sessions are keyed by host name
        # and live in the backend process, not the GUI). Blindly
        # reusing it is what made the terminal look "connected" while
        # doing nothing: /terminal/read kept long-polling and
        # answering {"data": "", "closed": False} forever, and
        # /terminal/write's channel.send() can succeed locally into an
        # already-dead transport without raising anything the
        # OSError-only catch below would see. Drop it and open a fresh
        # session instead of trusting it.
        with _TERMINAL_SESSIONS_LOCK:
            _TERMINAL_SESSIONS.pop(name, None)
        _close_session(existing)

    try:
        import paramiko
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="paramiko is not installed - interactive terminal is unavailable"
        )

    host = hosts[name]
    key_path = host.get("key_path") or str(CONTROLLER_KEY_PATH)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            host["ip"],
            username=host.get("user", "root"),
            key_filename=key_path,
            timeout=10,
        )
        channel = client.invoke_shell(term="xterm", width=120, height=32)
        channel.settimeout(0.0)  # non-blocking - /terminal/read polls instead of blocking
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
        old = _TERMINAL_SESSIONS.get(name)
        if old is not None:
            _close_session(old)
        _TERMINAL_SESSIONS[name] = {
            "client": client,
            "channel": channel,
            "lock": threading.Lock(),
        }

    return {"host": name, "opened": True, "reused": False}


@router.post("/hosts/{name}/terminal/write")
def write_terminal(name: str, body: TerminalWriteRequest):
    session = _get_terminal_session(name)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="no open terminal session for this host - call /terminal/open first"
        )

    with session["lock"]:
        try:
            session["channel"].send(body.data)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Could not write to terminal: {e}")

    return {"host": name, "written": len(body.data)}


@router.get("/hosts/{name}/terminal/read")
def read_terminal(name: str):
    session = _get_terminal_session(name)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="no open terminal session for this host - call /terminal/open first"
        )

    channel = session["channel"]
    chunks = []

    with session["lock"]:
        try:
            if not channel.recv_ready() and not channel.recv_stderr_ready():
                # Nothing buffered yet - wait briefly for either some
                # data or the channel closing, instead of answering
                # empty immediately. channel.fileno() is paramiko's
                # internal event pipe made for exactly this purpose,
                # not a real socket, so select() on it is the
                # documented way to wait without spinning.
                select.select([channel], [], [], TERMINAL_LONG_POLL_S)

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

    return {"host": name, "data": "".join(chunks), "closed": closed}


@router.post("/hosts/{name}/terminal/close")
def close_terminal(name: str):
    with _TERMINAL_SESSIONS_LOCK:
        session = _TERMINAL_SESSIONS.pop(name, None)

    if session is not None:
        _close_session(session)

    return {"host": name, "closed": True}


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

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            host["ip"],
            username=host.get("user", "root"),
            key_filename=key_path,
            timeout=10,
        )
        sftp = client.open_sftp()
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
