"""
Single place where the desktop client talks to the Sysible backend.

Every page should go through here instead of calling `requests`
directly, so the API base URL and the admin API key are only
configured in one spot.
"""

import base64
import json
import os
import random
import re
import secrets
import shlex
import string
from pathlib import Path
from urllib.parse import quote

import requests

BASE_URL = os.getenv("SYSIBLE_API_URL", "https://127.0.0.1:9000")

_API_KEY_FILE = Path(os.getenv("SYSIBLE_API_KEY_FILE", "/opt/sysible/api_key.txt"))

# =========================================================
# TLS
# The controller serves a self-signed cert (no public domain on a
# LAN deployment, so a CA-signed one isn't an option). Rather than
# disabling verification - which would accept ANY cert and defeat
# the point of TLS - pin the specific cert install_sysible.sh
# generated. If the GUI runs on a different machine than the
# controller, copy that machine's $BASE/certs/server.crt over and
# point SYSIBLE_CA_CERT at the local copy.
# =========================================================
_CA_CERT_FILE = os.getenv("SYSIBLE_CA_CERT", "/opt/sysible/certs/server.crt")

if BASE_URL.startswith("https://"):
    if os.path.exists(_CA_CERT_FILE):
        _VERIFY = _CA_CERT_FILE
    else:
        # Fail closed (default cert validation) rather than trusting
        # blindly - this will reject the self-signed cert until the
        # admin sets SYSIBLE_CA_CERT, which is the correct failure mode.
        print(
            f"[api] warning: no pinned CA cert found at {_CA_CERT_FILE} - "
            "set SYSIBLE_CA_CERT to a local copy of the controller's "
            "certs/server.crt. TLS verification will likely fail until then."
        )
        _VERIFY = True
else:
    _VERIFY = True


def _load_api_key():
    env_key = os.getenv("SYSIBLE_API_KEY")
    if env_key:
        return env_key.strip()

    if _API_KEY_FILE.exists():
        return _API_KEY_FILE.read_text().strip()

    return None


_API_KEY = _load_api_key()


def _headers():
    return {"X-API-Key": _API_KEY} if _API_KEY else {}


# A single reused Session (keep-alive connection pool) instead of a
# bare requests.get/request call per invocation. Over plain HTTP that
# distinction barely matters, but BASE_URL is HTTPS by default - every
# call used to pay a full new TCP handshake *and* TLS handshake (cert
# pinning verification included) on top of it. That's most of why the
# live SSH terminal (remote_administration_page.py) felt sluggish:
# it's polling /terminal/read every 300ms and posting a fresh
# /terminal/write for every single keystroke, each one synchronous on
# the GUI thread, so every keystroke paid that handshake cost before
# the keypress even reached the remote shell.
_SESSION = requests.Session()


def ping():
    """Lightweight, unauthenticated backend health check - hits the
    same "/" route `sysible_controller start` polls for readiness. Used by the
    GUI's watchdog to detect the backend going away (stopped or
    crashed) so it can close itself instead of sitting there showing
    stale data and failing requests. Never raises - just True/False.

    Short timeout on purpose: a stopped backend usually refuses the
    connection immediately, but if the process is hung rather than
    gone, this still bounds how long one watchdog tick can take so
    detection stays fast (see BACKEND_CHECK_INTERVAL_MS/
    BACKEND_FAILURE_THRESHOLD in client/main.py)."""
    try:
        r = _SESSION.get(f"{BASE_URL}/", timeout=2, verify=_VERIFY)
        return r.ok
    except requests.RequestException:
        return False


def _request(method, path, **kwargs):
    r = _SESSION.request(
        method,
        f"{BASE_URL}{path}",
        headers=_headers(),
        timeout=kwargs.pop("timeout", 15),
        verify=_VERIFY,
        **kwargs,
    )

    if not r.ok:
        # r.raise_for_status() alone only ever says e.g. "400 Client
        # Error: Bad Request for url: ..." - it throws away the
        # FastAPI {"detail": "..."} body, which is where the actual
        # reason lives (bad SSH password, unreachable host, remote
        # command's stderr, etc). Surface that instead.
        detail = None
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            pass

        raise requests.exceptions.HTTPError(
            f"{r.status_code} {detail or r.reason}", response=r
        )

    if not r.content:
        return None

    return r.json()


def _download_binary(path):
    """Like _request(), but for endpoints that return raw file bytes
    rather than JSON (r.json() would just throw on a zip/arbitrary
    file's content)."""
    r = _SESSION.get(
        f"{BASE_URL}{path}", headers=_headers(), timeout=30, verify=_VERIFY
    )

    if not r.ok:
        detail = None
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            pass

        raise requests.exceptions.HTTPError(
            f"{r.status_code} {detail or r.reason}", response=r
        )

    return r.content


# =========================================================
# AGENTS / HOST ENROLLMENT
# =========================================================
def generate_enroll_token():
    return _request("POST", "/admin/enroll-token/generate")


def get_agents():
    return _request("GET", "/agents").get("agents", [])


def disenroll_agent(host_id: str):
    return _request("DELETE", f"/agents/{host_id}")


def set_agent_environment(host_id: str, environment: str):
    return _request("POST", f"/agents/{host_id}/environment", json={"environment": environment})


# ---------------------------------------------------------
# AGENT-SIDE UNINSTALL (host-side systemd teardown, dispatched over the
# task queue from the GUI's "Disenroll Host" button)
#
# Mirrors the teardown steps baked into backend/agent_bundle.py's
# disenroll_agent.sh (stop/disable the systemd service, remove the
# unit file, the install dir, the pinned cert, and the local state
# file) - but runs remotely over the normal task-queue dispatch
# instead of requiring an operator to log into the host and run that
# script by hand. The two paths are independent of each other and
# either can be used: click Disenroll Host here, or run
# disenroll_agent.sh on the host itself.
#
# Paths/names below are plain string literals, not imported from
# backend/agent_bundle.py - client and backend only ever talk over
# HTTP, never share Python code - so they're kept in sync by hand. If
# agent_bundle.py's _SERVICE_NAME/_AGENT_INSTALL_DIR/etc. ever change,
# update these to match.
# ---------------------------------------------------------
_AGENT_SERVICE_NAME = "sysible-agent"
_AGENT_INSTALL_DIR = "/opt/sysible-agent"
_AGENT_CERT_DIR = "/etc/sysible"
_AGENT_CERT_PATH = f"{_AGENT_CERT_DIR}/controller.crt"
_AGENT_STATE_DIR = "/var/lib/sysible"
_AGENT_STATE_FILE = f"{_AGENT_STATE_DIR}/agent_state.json"


def cmd_uninstall_agent_service() -> str:
    """Removes the systemd service (and everything else run_agent.sh
    installed) from whichever host this command is dispatched to.

    This command runs *as a task inside* the very sysible-agent
    service it's about to stop, so the actual `systemctl disable
    --now` can't happen inline - that would kill the agent process
    (and this command's own subprocess) before it has a chance to
    report success back to the controller. Instead it hands the
    teardown off to a short-lived transient unit via `systemd-run
    --on-active=`, which lives in its own cgroup outside
    sysible-agent.service, and returns immediately so the agent can
    report back right away."""
    teardown = (
        f"systemctl disable --now {_AGENT_SERVICE_NAME}.service 2>/dev/null || true; "
        f"rm -f /etc/systemd/system/{_AGENT_SERVICE_NAME}.service; "
        f"systemctl daemon-reload 2>/dev/null || true; "
        f"rm -rf {_AGENT_INSTALL_DIR}; "
        f"rm -f {_AGENT_CERT_PATH}; "
        f"rmdir --ignore-fail-on-non-empty {_AGENT_CERT_DIR} 2>/dev/null || true; "
        f"rm -f {_AGENT_STATE_FILE}; "
        f"rmdir --ignore-fail-on-non-empty {_AGENT_STATE_DIR} 2>/dev/null || true"
    )
    quoted = shlex.quote(teardown)

    return (
        f"if command -v systemd-run >/dev/null 2>&1; then "
        f"systemd-run --quiet --on-active=2 --unit={_AGENT_SERVICE_NAME}-uninstall "
        f"/bin/bash -c {quoted} "
        f"&& echo 'Uninstall scheduled - the systemd service and agent files "
        f"will be removed within a few seconds.' "
        f"|| echo 'WARNING: systemd-run failed to schedule teardown - service "
        f"left running. Run disenroll_agent.sh on this host instead.' >&2; "
        f"else echo 'WARNING: systemd-run not found - could not schedule "
        f"automatic teardown. Run disenroll_agent.sh on this host instead.' >&2; fi"
    )


# =========================================================
# ENVIRONMENTS (dev/stage/prod, etc.)
#
# An editable registry shared by agent hosts (Host Enrollment) and SSH
# hosts (Remote Administration) - both host kinds just store one of
# these names as a plain string; see set_agent_environment() above and
# set_host_environment() below.
# =========================================================
def list_environments():
    return _request("GET", "/environments").get("environments", [])


def create_environment(name: str):
    return _request("POST", "/environments", json={"name": name}).get("environments", [])


def delete_environment(name: str):
    return _request("DELETE", f"/environments/{name}").get("environments", [])


# =========================================================
# CONTROLLER CONFIGURATION
#
# Hostname/IP/port baked into agent bundles handed out by the Webserver
# Portal below - set once here so a downloaded agent never needs
# SYSIBLE_CONTROLLER configured by hand. Hostname and IP are tracked as
# two independent fields; address_mode ("hostname", "ip", or "all" -
# every IP detect_local_ips() finds on the controller, with the agent
# failing over between them) says which one actually gets used when a
# bundle is built.
# =========================================================
def get_controller_config():
    return _request("GET", "/controller-config")


def set_controller_config(hostname: str, ip: str, address_mode: str, port: int):
    return _request(
        "POST",
        "/controller-config",
        json={"hostname": hostname, "ip": ip, "address_mode": address_mode, "port": port},
    )


def get_license_config():
    """Currently just a license key an admin has typed in - no licensing
    model is enforced against it yet. Surfaced alongside VERSION
    (version.py) in Sysible Controller Settings' License & Version
    section."""
    return _request("GET", "/license-config")


def set_license_config(license_key: str):
    return _request("POST", "/license-config", json={"license_key": license_key})


def get_local_ips():
    """Every non-loopback IPv4 address detected on the controller right
    now - powers the IP picker/"All Detected IPs" option in Controller
    Configuration so the admin never has to run `ip addr`/`ifconfig` by
    hand."""
    return _request("GET", "/controller-config/local-ips").get("ips", [])


def download_agent_bundle(save_path):
    """Build a fresh agent bundle (new one-time enrollment token baked
    in) and save it locally - the same bundle the Webserver Portal
    hands a remote host operator, but available straight from this GUI
    without needing the portal running."""
    data = _download_binary("/controller-config/agent-bundle")
    Path(save_path).write_bytes(data)
    return save_path


# =========================================================
# ENVIRONMENTAL POLICIES
#
# Baseline password/lockout/sudo/umask settings for accounts on
# managed target hosts - System Administration > Environmental
# Policies pushes these to checked hosts, and this same object is the
# default `policy` passed to check_password_strength() /
# generate_strong_password() below, so a generated password always
# satisfies whatever's actually configured here. Separate from
# get_admin_password_policy() further down, which only governs this
# controller's own GUI-login administrator accounts.
# =========================================================
def get_environmental_policy():
    return _request("GET", "/environmental-policy")


def set_environmental_policy(policy: dict):
    return _request("POST", "/environmental-policy", json=policy)


# =========================================================
# WEBSERVER PORTAL
#
# A separate host-facing webserver (its own process/port, started and
# stopped on demand) that lets a remote host operator log in with a
# simple username/password and download a ready-to-run agent bundle.
# Unrelated to the admin API key or the enrollment-token system.
# =========================================================
def get_portal_status():
    return _request("GET", "/portal/status")


def start_portal():
    return _request("POST", "/portal/start")


def stop_portal():
    return _request("POST", "/portal/stop")


def set_portal_credentials(username: str, password: str, current_password: str = ""):
    return _request("POST", "/portal/credentials", json={
        "username": username,
        "password": password,
        "current_password": current_password,
    })


def remove_portal_credentials(current_password: str):
    return _request("DELETE", "/portal/credentials", json={
        "current_password": current_password,
    })


def get_portal_config():
    return _request("GET", "/portal/config")


def set_portal_port(port: int):
    return _request("POST", "/portal/config", json={"port": port})


def get_portal_login_history(limit: int = 200):
    return _request("GET", "/portal/login-history", params={"limit": limit}).get("history", [])


def get_portal_sessions():
    return _request("GET", "/portal/sessions").get("sessions", [])


def revoke_portal_session(session_id: int):
    return _request("POST", f"/portal/sessions/{session_id}/revoke")


# =========================================================
# ADMINISTRATORS
#
# Gates the desktop GUI itself - separate from the Webserver Portal
# credentials above (those are for a remote host operator in a
# browser). Multiple named admin accounts, each with their own
# password; a fresh install seeds a default admin/admin account
# forced to change its password on first login. Managed from the
# Sysible Controller Settings page.
# =========================================================
def admin_login(username: str, password: str):
    """Raises requests.exceptions.HTTPError on bad credentials (401) -
    callers should catch that and show an inline error rather than a
    crash, since a wrong password is an expected case here, not a bug.

    Returns {"status", "username", "must_change_password"}."""
    return _request("POST", "/admin/login", json={"username": username, "password": password})


def list_administrators():
    return _request("GET", "/admin/administrators").get("administrators", [])


def add_administrator(username: str, password: str, actor: str = ""):
    return _request(
        "POST",
        "/admin/administrators",
        json={"username": username, "password": password, "actor": actor},
    )


def remove_administrator(username: str, actor: str = ""):
    return _request("DELETE", f"/admin/administrators/{username}", params={"actor": actor})


def change_admin_credentials(username: str, current_password: str, new_username: str, new_password: str):
    return _request(
        "POST",
        "/admin/credentials",
        json={
            "username": username,
            "current_password": current_password,
            "new_username": new_username,
            "new_password": new_password,
        },
    )


def force_admin_password_change(username: str, current_password: str, new_password: str):
    return _request(
        "POST",
        "/admin/force-password-change",
        json={
            "username": username,
            "current_password": current_password,
            "new_password": new_password,
        },
    )


def get_admin_audit_log(limit: int = 200):
    return _request("GET", "/admin/audit-log", params={"limit": limit}).get("entries", [])


# =========================================================
# ADMINISTRATOR PASSWORD POLICY
#
# Governs only this controller's own GUI-login administrator
# accounts (Sysible Controller Settings) - separate from get_environmental_policy()
# above, which governs target Linux accounts on managed hosts.
# =========================================================
def get_admin_password_policy():
    return _request("GET", "/admin/password-policy")


def set_admin_password_policy(policy: dict):
    return _request("POST", "/admin/password-policy", json=policy)


# =========================================================
# WEBSERVER PORTAL FILE POOL
#
# Shared pool, not per-host (matches the portal's single shared
# login): "uploads" are files a host operator sent in through the
# portal, "downloads" are files the admin staged here for a host
# operator to grab next time they log in. See backend/portal_files.py.
# =========================================================
def list_portal_uploads():
    return _request("GET", "/portal/files/uploads").get("files", [])


def download_portal_upload(filename: str, save_path):
    """Fetches an uploaded file's bytes and writes them to save_path
    (a local path on the machine running the GUI). Returns save_path."""
    data = _download_binary(f"/portal/files/uploads/{quote(filename, safe='')}")
    Path(save_path).write_bytes(data)
    return save_path


def delete_portal_upload(filename: str):
    return _request("DELETE", f"/portal/files/uploads/{quote(filename, safe='')}")


def list_portal_downloads():
    return _request("GET", "/portal/files/downloads").get("files", [])


def stage_portal_download(local_path):
    """Uploads the file at local_path into the downloads pool, so it
    shows up for any host operator who logs into the portal."""
    local_path = Path(local_path)

    with open(local_path, "rb") as f:
        return _request(
            "POST", "/portal/files/downloads", files={"file": (local_path.name, f)}
        )


def delete_portal_download(filename: str):
    return _request("DELETE", f"/portal/files/downloads/{quote(filename, safe='')}")


# =========================================================
# USERS
# =========================================================
def list_users():
    return _request("GET", "/users")


def get_user(username: str):
    return _request("GET", f"/users/{username}")


def get_user_sessions(username: str):
    return _request("GET", f"/users/{username}/sessions")


def create_user(username: str, password: str = "", shell: str = "/bin/bash"):
    return _request("POST", "/users/", json={
        "username": username,
        "password": password,
        "shell": shell or "/bin/bash",
    })


def delete_user(username: str):
    return _request("DELETE", f"/users/{username}")


def lock_user(username: str):
    return _request("POST", f"/users/{username}/lock")


def unlock_user(username: str):
    return _request("POST", f"/users/{username}/unlock")


def toggle_sudo(username: str):
    return _request("POST", f"/users/{username}/sudo/toggle")


# =========================================================
# GROUPS
# =========================================================
def list_groups():
    return _request("GET", "/groups/")


def create_group(name: str):
    return _request("POST", "/groups/", json={"name": name})


def delete_group(name: str):
    return _request("DELETE", f"/groups/{name}")


def add_user_to_group(group: str, username: str):
    return _request("POST", f"/groups/{group}/users/{username}")


def remove_user_from_group(group: str, username: str):
    return _request("DELETE", f"/groups/{group}/users/{username}")


# =========================================================
# REMOTE HOSTS
#
# Enrollment is one call: enroll_ssh() uses a password exactly once
# to install Sysible's own controller SSH key on the target, then the
# host is reachable via exec_remote() with no further setup. There's
# no separate "generate a key" step exposed to callers - the backend
# creates and reuses one controller-wide key automatically.
# =========================================================
def list_hosts():
    return _request("GET", "/remote/hosts")


def delete_host(name: str):
    return _request("DELETE", f"/remote/hosts/{name}")


def get_controller_key():
    """Public key text only - for optional manual/advanced display in
    the GUI (e.g. to paste into a host's image out-of-band)."""
    return _request("GET", "/remote/controller-key").get("public_key")


def enroll_ssh(name: str, ip: str, username: str, password: str, environment: str = ""):
    return _request("POST", "/remote/enroll-ssh", json={
        "name": name,
        "ip": ip,
        "username": username or "root",
        "password": password,
        "environment": environment or "",
    })


def set_host_environment(name: str, environment: str):
    return _request("POST", f"/remote/hosts/{name}/environment", json={"environment": environment})


def exec_remote(name: str, cmd: str):
    return _request("POST", f"/remote/hosts/{name}/exec", json={"cmd": cmd})


# ---------------------------------------------------------
# Interactive terminal (persistent PTY-backed shell per SSH host -
# what the Remote Host Administration Terminal panel actually drives,
# in place of one-shot exec_remote() above, so sudo prompts and other
# interactive programs work as they would in a real terminal).
# ---------------------------------------------------------
def open_terminal(name: str):
    return _request("POST", f"/remote/hosts/{name}/terminal/open", timeout=20)


def write_terminal(name: str, data: str):
    return _request("POST", f"/remote/hosts/{name}/terminal/write", json={"data": data})


def read_terminal(name: str):
    return _request("GET", f"/remote/hosts/{name}/terminal/read")


def close_terminal(name: str):
    return _request("POST", f"/remote/hosts/{name}/terminal/close")


# =========================================================
# REMOTE HOST FILE TRANSFER
#
# SSH hosts get a real, size-unbounded transfer via the backend's SFTP
# routes (key-based, same controller key as exec_remote()/
# open_terminal() above). Agent hosts have no persistent connection -
# only the poll-based task queue (see FLEET USER MANAGEMENT below) -
# so transfer there reuses that same channel instead: the file's bytes
# travel base64-encoded inside a queued Python one-liner (upload) or
# printed back as base64 in the task's stdout (download). That's why
# agent-host transfers are capped at AGENT_FILE_TRANSFER_LIMIT_BYTES -
# the agent already truncates any single task's stdout/stderr at
# 200,000 characters (host_agent/agent.py's MAX_OUTPUT_BYTES) to
# protect itself against a runaway command, and this cap sits
# comfortably below the point where a base64'd file's text would hit
# that truncation and produce silently-corrupt output instead of an
# upfront, readable error.
# =========================================================
def upload_file_ssh(name: str, local_path, remote_path: str):
    """Upload local_path to remote_path (a full path, or an existing
    remote directory to upload into) on an SSH-enrolled host via SFTP.
    Returns the backend's {"remote_path", "size"} confirmation dict."""
    local_path = Path(local_path)

    with open(local_path, "rb") as f:
        return _request(
            "POST",
            f"/remote/hosts/{quote(name, safe='')}/files/upload",
            data={"remote_path": remote_path},
            files={"file": (local_path.name, f)},
            timeout=120,
        )


def download_file_ssh(name: str, remote_path: str, save_path):
    """Download remote_path from an SSH-enrolled host over SFTP and
    write it to save_path (a local path on the machine running the
    GUI). Returns save_path."""
    qpath = quote(remote_path, safe="")
    data = _download_binary(f"/remote/hosts/{quote(name, safe='')}/files/download?path={qpath}")
    Path(save_path).write_bytes(data)
    return save_path


AGENT_FILE_TRANSFER_LIMIT_BYTES = 140_000


def _build_agent_upload_script(remote_path: str, filename: str, data: bytes) -> str:
    """If remote_path is an existing remote directory, upload into it
    under the original filename - same "drop onto a folder" behavior
    as _resolve_remote_upload_path() on the SSH/SFTP side above, just
    re-implemented inline since this runs as a one-off script on the
    agent host instead of in this process."""
    encoded = base64.b64encode(data).decode()
    return (
        "import base64, os\n"
        "remote_path = " + repr(remote_path) + "\n"
        "if os.path.isdir(remote_path):\n"
        "    remote_path = os.path.join(remote_path, " + repr(filename) + ")\n"
        "data = base64.b64decode(" + repr(encoded) + ")\n"
        "with open(remote_path, 'wb') as f:\n"
        "    f.write(data)\n"
        "print(remote_path)\n"
    )


def _build_agent_download_script(remote_path: str) -> str:
    return (
        "import base64, os, sys\n"
        "path = " + repr(remote_path) + "\n"
        "if not os.path.isfile(path):\n"
        "    print('not a file or not found: ' + path, file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "size = os.path.getsize(path)\n"
        "limit = " + str(AGENT_FILE_TRANSFER_LIMIT_BYTES) + "\n"
        "if size > limit:\n"
        "    print('file too large for agent transfer (%d bytes, limit %d)' % (size, limit), file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "with open(path, 'rb') as f:\n"
        "    print(base64.b64encode(f.read()).decode())\n"
    )


def queue_agent_upload(host_id: str, local_path, remote_path: str):
    """Queue a file upload to an agent-enrolled host. Returns
    {"task_id", "error"} - poll with poll_agent_upload(). Refuses
    upfront (no task queued) rather than truncating silently if the
    local file is over AGENT_FILE_TRANSFER_LIMIT_BYTES."""
    local_path = Path(local_path)
    size = local_path.stat().st_size

    if size > AGENT_FILE_TRANSFER_LIMIT_BYTES:
        return {
            "task_id": None,
            "error": (
                f"File is {size} bytes - agent-host uploads are limited to "
                f"{AGENT_FILE_TRANSFER_LIMIT_BYTES} bytes. SSH hosts have no such limit."
            ),
        }

    script = _build_agent_upload_script(remote_path, local_path.name, local_path.read_bytes())
    cmd = _wrap_python_script(script)

    task_ids = queue_command_on_hosts([host_id], cmd, kind="upload_file")
    task_id = task_ids.get(host_id)

    return {"task_id": task_id, "error": None if task_id is not None else "failed to queue upload"}


def poll_agent_upload(host_id: str, task_id):
    """None while pending. Once resolved: {"error": None, "remote_path"}
    on success, or {"error": "...", "remote_path": None} on failure."""
    raw = get_result_by_task(host_id, task_id)
    output = parse_task_output(raw)

    if output is None:
        return None

    if output.get("returncode") == 0:
        return {"error": None, "remote_path": (output.get("stdout") or "").strip()}

    return {
        "error": output.get("stderr") or f"upload exited {output.get('returncode')}",
        "remote_path": None,
    }


def queue_agent_download(host_id: str, remote_path: str):
    """Queue a file download from an agent-enrolled host. Returns
    {"task_id", "error"} - poll with poll_agent_download(), which also
    writes the decoded bytes to a local path once ready."""
    script = _build_agent_download_script(remote_path)
    cmd = _wrap_python_script(script)

    task_ids = queue_command_on_hosts([host_id], cmd, kind="download_file")
    task_id = task_ids.get(host_id)

    return {"task_id": task_id, "error": None if task_id is not None else "failed to queue download"}


def poll_agent_download(host_id: str, task_id, save_path):
    """None while pending. Once resolved, decodes the agent's base64
    stdout and writes it to save_path, returning {"error": None}, or
    {"error": "..."} if the remote read or local save failed."""
    raw = get_result_by_task(host_id, task_id)
    output = parse_task_output(raw)

    if output is None:
        return None

    if output.get("returncode") != 0:
        return {"error": output.get("stderr") or f"download exited {output.get('returncode')}"}

    try:
        data = base64.b64decode((output.get("stdout") or "").strip())
    except (ValueError, TypeError) as e:
        return {"error": f"could not decode file data from agent: {e}"}

    try:
        Path(save_path).write_bytes(data)
    except OSError as e:
        return {"error": f"could not save file locally: {e}"}

    return {"error": None}


# =========================================================
# FLEET USER MANAGEMENT (via enrolled hosts)
#
# These mirror the USERS / GROUPS actions above, but instead of acting
# on the controller's own machine they queue a shell command for one
# or more *enrolled agents* (see AGENTS / HOST ENROLLMENT) to run and
# report back. Dispatch is fire-and-forget from the caller's point of
# view - use sync_hosts()/get_sync_result() or get_result_by_task() to
# read back what happened.
#
# Password changes never put a plaintext password into a queued
# command: agent_tasks.command is stored in plain SQLite and is
# readable through the admin API, so passwords are hashed client-side
# (see _hash_password) before they're embedded in a `usermod -p ...`
# string.
# =========================================================

def queue_command_on_hosts(host_ids, command: str, kind: str = "command"):
    """Queue `command` on every host_id in host_ids. Returns {host_id: task_id}
    (task_id is None for any host the queue call failed for)."""
    task_ids = {}

    for host_id in host_ids:
        try:
            result = _request(
                "POST",
                f"/agents/{host_id}/tasks",
                json={"command": command, "kind": kind},
            )
            task_ids[host_id] = result.get("task_id") if result else None
        except requests.RequestException:
            task_ids[host_id] = None

    return task_ids


def get_result_by_task(host_id: str, task_id: int):
    """Look up the raw agent_results row for one specific task, or None
    if the agent hasn't reported back yet."""
    if task_id is None:
        return None

    results = _request(
        "GET", f"/agents/{host_id}/results", params={"task_id": task_id}
    ).get("results", [])

    return results[0] if results else None


def get_latest_result(host_id: str, kind: str = None):
    """Look up the most recent agent_results row for a host, optionally
    filtered to a given task kind (e.g. 'sync_users')."""
    params = {"kind": kind} if kind else {}
    results = _request(
        "GET", f"/agents/{host_id}/results", params=params
    ).get("results", [])

    return results[0] if results else None


def parse_task_output(raw_result):
    """An agent_results row's `result` field is the JSON-stringified
    {stdout, stderr, returncode} the agent's subprocess produced.
    Returns that dict, or None if the row hasn't arrived / isn't valid
    JSON yet."""
    if not raw_result or not raw_result.get("result"):
        return None

    try:
        return json.loads(raw_result["result"])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------
# Full sync (read): gather live user/group/session state
# ---------------------------------------------------------
_SYNC_USERS_SCRIPT = """
import pwd, grp, subprocess, json

def _groups(username):
    try:
        out = subprocess.run(["id", "-nG", username], capture_output=True, text=True).stdout
        return out.split()
    except Exception:
        return []

def _locked(username):
    try:
        out = subprocess.run(["passwd", "-S", username], capture_output=True, text=True).stdout
        fields = out.split()
        return len(fields) > 1 and fields[1].startswith("L")
    except Exception:
        return False

def _sessions():
    sessions = []
    try:
        out = subprocess.run(["who"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                sessions.append({"username": parts[0], "tty": parts[1]})
    except Exception:
        pass
    return sessions

users = []
for u in pwd.getpwall():
    if u.pw_uid < 1000:
        continue
    groups = _groups(u.pw_name)
    users.append({
        "username": u.pw_name,
        "uid": u.pw_uid,
        "gid": u.pw_gid,
        "home": u.pw_dir,
        "shell": u.pw_shell,
        "groups": groups,
        "sudo": "sudo" in groups,
        "locked": _locked(u.pw_name),
    })

groups = []
for g in grp.getgrall():
    groups.append({"name": g.gr_name, "gid": g.gr_gid, "members": g.gr_mem})

print(json.dumps({"users": users, "groups": groups, "sessions": _sessions()}))
"""


def _wrap_python_script(script: str) -> str:
    """Base64-wrap a Python script as a single shell command, so it
    survives JSON, SQLite, and shell=True without any quoting/escaping
    headaches."""
    encoded = base64.b64encode(script.encode()).decode()
    return f'python3 -c "import base64;exec(base64.b64decode(\'{encoded}\').decode())"'


def build_user_sync_command() -> str:
    return _wrap_python_script(_SYNC_USERS_SCRIPT)


def sync_hosts(host_ids):
    """Queue the user/group/session inventory script on each host_id.
    Returns {host_id: task_id} - poll get_result_by_task(host_id, task_id)
    or get_sync_result(host_id) until it resolves."""
    return queue_command_on_hosts(host_ids, build_user_sync_command(), kind="sync_users")


def get_sync_result(host_id: str, task_id: int = None):
    """Return the parsed {"users": [...], "groups": [...], "sessions": [...]}
    inventory for a host, or None if the sync hasn't completed yet.
    Pass task_id to wait for one specific sync; omit it to just read
    whatever the most recent sync_users result was."""
    raw = (
        get_result_by_task(host_id, task_id)
        if task_id is not None
        else get_latest_result(host_id, kind="sync_users")
    )

    output = parse_task_output(raw)

    if not output or output.get("returncode") != 0:
        return None

    try:
        return json.loads(output["stdout"])
    except (TypeError, ValueError, KeyError):
        return None


# ---------------------------------------------------------
# Password hashing (never send plaintext to a host)
# ---------------------------------------------------------
# ---------------------------------------------------------
# PASSWORD STRENGTH / GENERATION / POLICY PRESETS
#
# Client-side only - none of this touches a host. The point is to
# catch a weak password in the GUI before it's ever hashed and queued
# (see cmd_set_password below), instead of letting a host's own
# pam_pwquality silently accept whatever pwquality.conf happens to be
# set to (or, on a host where the policy below was never applied,
# accept anything at all).
# ---------------------------------------------------------
PASSWORD_POLICY_PRESETS = {
    "Basic": {
        "minlen": 8, "retry": 3, "dcredit": 0, "ucredit": 0,
        "lcredit": 0, "ocredit": 0, "deny": 5, "unlock_time": 600,
    },
    "Standard": {
        "minlen": 12, "retry": 3, "dcredit": -1, "ucredit": -1,
        "lcredit": -1, "ocredit": 0, "deny": 5, "unlock_time": 900,
    },
    "Strict": {
        "minlen": 16, "retry": 3, "dcredit": -1, "ucredit": -1,
        "lcredit": -1, "ocredit": -1, "deny": 3, "unlock_time": 1800,
    },
}


_DEFAULT_PASSWORD_POLICY = {
    "minlen": 12, "dcredit": -1, "ucredit": -1, "lcredit": -1, "ocredit": -1,
}


def check_password_strength(password: str, policy: dict = None):
    """Returns (ok, message). message explains the first unmet
    requirement when ok is False, and is "" when ok is True.

    `policy` takes the same shape as the password sub-object of
    get_environmental_policy() (minlen/dcredit/ucredit/lcredit/ocredit,
    pwquality's negative-means-required convention) - callers should
    pass that in so the GUI enforces whatever's actually configured on
    the Environmental Policies page. Falls back to a
    "Standard"-preset-equivalent baseline (12+ chars, all four
    character classes) when no policy is given."""
    policy = policy or _DEFAULT_PASSWORD_POLICY
    minlen = policy.get("minlen", 12)
    if len(password) < minlen:
        return False, f"Password must be at least {minlen} characters long."
    if policy.get("lcredit", 0) < 0 and not re.search(r"[a-z]", password):
        return False, "Password must include at least one lowercase letter."
    if policy.get("ucredit", 0) < 0 and not re.search(r"[A-Z]", password):
        return False, "Password must include at least one uppercase letter."
    if policy.get("dcredit", 0) < 0 and not re.search(r"[0-9]", password):
        return False, "Password must include at least one digit."
    if policy.get("ocredit", 0) < 0 and not re.search(r"[^A-Za-z0-9]", password):
        return False, "Password must include at least one symbol (e.g. ! @ # $ %)."
    return True, ""


def generate_strong_password(length: int = 16, policy: dict = None) -> str:
    """Random password guaranteed to pass check_password_strength(password,
    policy) - one guaranteed character from each character class the
    policy actually requires (credit < 0), the rest drawn from the
    full pool, then shuffled (with the crypto-secure `secrets` module,
    not `random`) so the guaranteed characters aren't always sitting
    in the same first few positions.

    Same `policy` shape as check_password_strength - pass in
    get_environmental_policy()["password"] so a generated password
    always satisfies whatever's actually configured."""
    policy = policy or _DEFAULT_PASSWORD_POLICY
    length = max(length, policy.get("minlen", 12))
    lower, upper, digits = string.ascii_lowercase, string.ascii_uppercase, string.digits
    symbols = "!@#$%^&*()-_=+[]{}"
    pool = lower + upper + digits + symbols

    required_pools = []
    if policy.get("lcredit", 0) < 0:
        required_pools.append(lower)
    if policy.get("ucredit", 0) < 0:
        required_pools.append(upper)
    if policy.get("dcredit", 0) < 0:
        required_pools.append(digits)
    if policy.get("ocredit", 0) < 0:
        required_pools.append(symbols)

    chars = [secrets.choice(p) for p in required_pools]
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]

    rng = random.SystemRandom()
    rng.shuffle(chars)
    return "".join(chars)


def _hash_password(password: str):
    """Hash client-side so the plaintext password never gets embedded
    in a queued shell command. Returns None if the `crypt` module isn't
    available (e.g. the GUI is running on Windows) - callers must
    handle that rather than falling back to plaintext."""
    try:
        import crypt
        return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    except ImportError:
        return None


# ---------------------------------------------------------
# Command builders (write actions, run via shlex.quote-safe shell)
# ---------------------------------------------------------
def cmd_create_user(username: str, password: str = "", shell: str = "/bin/bash") -> str:
    cmd = f"useradd -m -s {shlex.quote(shell or '/bin/bash')} {shlex.quote(username)}"

    if password:
        cmd += " && " + cmd_set_password(username, password)

    return cmd


def cmd_delete_user(username: str) -> str:
    return f"userdel -r {shlex.quote(username)}"


def cmd_lock_user(username: str) -> str:
    return f"usermod -L {shlex.quote(username)}"


def cmd_unlock_user(username: str) -> str:
    return f"usermod -U {shlex.quote(username)}"


def cmd_set_sudo(username: str, enable: bool) -> str:
    if enable:
        return f"usermod -aG sudo {shlex.quote(username)}"

    return f"deluser {shlex.quote(username)} sudo"


def cmd_set_password(username: str, password: str) -> str:
    """Hashes `password` locally and embeds only the hash - never the
    plaintext - in the queued command."""
    hashed = _hash_password(password)

    if hashed is None:
        raise RuntimeError(
            "Password hashing unavailable on this client (the `crypt` "
            "module requires a POSIX system) - refusing to queue a "
            "remote password change rather than send it as plaintext."
        )

    return f"usermod -p {shlex.quote(hashed)} {shlex.quote(username)}"


def cmd_create_group(name: str) -> str:
    return f"groupadd {shlex.quote(name)}"


def cmd_delete_group(name: str) -> str:
    return f"groupdel {shlex.quote(name)}"


def cmd_add_user_to_group(group: str, username: str) -> str:
    return f"usermod -aG {shlex.quote(group)} {shlex.quote(username)}"


def cmd_remove_user_from_group(group: str, username: str) -> str:
    return f"gpasswd -d {shlex.quote(username)} {shlex.quote(group)}"


# ---------------------------------------------------------
# USER ADMINISTRATION (extended) - session termination, forced/aging
# password policy, account expiration, /etc/passwd detail edits,
# privileged-user auditing, and host-wide PAM password/lockout policy.
# All still plain shell strings dispatched the same way as the basic
# create/lock/delete builders above.
# ---------------------------------------------------------
def cmd_kill_user_sessions(username: str) -> str:
    """Ends every active login session for a user right now, without
    locking or deleting the account - they can log back in immediately
    afterward. Prefers loginctl (systemd-logind) since it tears each
    session down cleanly; falls back to pkill -KILL -u on hosts
    without systemd-logind."""
    user = shlex.quote(username)
    return (
        f"if command -v loginctl >/dev/null 2>&1; then "
        f"loginctl terminate-user {user} 2>&1; "
        f"else pkill -KILL -u {user} 2>&1; fi; "
        f"echo 'Done (no error above means it worked, or there were no active sessions).'"
    )


def cmd_force_password_reset(username: str) -> str:
    """Forces the user to set a new password the next time they log in,
    by invalidating the recorded last-changed date (chage -d 0) so PAM
    treats the current password as already expired."""
    return f"chage -d 0 {shlex.quote(username)}"


def cmd_set_password_aging(username: str, max_days=None, min_days=None, warn_days=None) -> str:
    """Configures /etc/shadow's password-aging fields via chage: max
    days before a password must be changed, min days before it's
    allowed to be changed again, and how many days ahead of expiry to
    start warning the user. Any argument left as None is not touched."""
    user = shlex.quote(username)
    parts = ["chage"]
    if max_days is not None:
        parts += ["-M", str(int(max_days))]
    if min_days is not None:
        parts += ["-m", str(int(min_days))]
    if warn_days is not None:
        parts += ["-W", str(int(warn_days))]
    if len(parts) == 1:
        raise ValueError("Specify at least one of max / min / warn days")
    parts.append(user)
    return " ".join(parts)


def cmd_set_account_expiration(username: str, expire_date: str = "") -> str:
    """Sets (or, if expire_date is blank, clears) the account expiration
    date in /etc/shadow via chage -E. Expects YYYY-MM-DD; chage itself
    rejects anything else and that error surfaces back through the
    normal dispatch error path rather than being validated twice."""
    user = shlex.quote(username)
    date = expire_date.strip()
    if not date:
        return f"chage -E -1 {user}"
    return f"chage -E {shlex.quote(date)} {user}"


def cmd_set_user_shell(username: str, shell: str) -> str:
    return f"usermod -s {shlex.quote(shell)} {shlex.quote(username)}"


def cmd_set_user_comment(username: str, comment: str) -> str:
    """Sets the GECOS "full name" field in /etc/passwd."""
    return f"usermod -c {shlex.quote(comment)} {shlex.quote(username)}"


def cmd_audit_privileged_users() -> str:
    """Read-only privileged-account report: members of the sudo/wheel/
    admin groups, any non-root account sharing UID 0, and any sudoers
    NOPASSWD entries - the things worth checking when auditing who
    actually has root on a box."""
    return (
        "echo '== sudo/wheel/admin group members ==' && "
        "for g in sudo wheel admin; do "
        "getent group \"$g\" 2>/dev/null | awk -F: '{print $1\": \"$4}'; "
        "done; "
        "echo && echo '== Accounts with UID 0 besides root ==' && "
        "(awk -F: '($3==0 && $1!=\"root\"){print $1}' /etc/passwd || true); "
        "echo && echo '== sudoers NOPASSWD entries ==' && "
        "(grep -rH 'NOPASSWD' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | grep -v '^[^:]*:#' || echo 'None found.')"
    )


def cmd_list_groups_with_members() -> str:
    """Read-only dump of every group and its member list - the full
    /etc/group picture, for when Group Management's add/remove-one-
    user-at-a-time view isn't enough to see the whole layout at once."""
    return "getent group | awk -F: '{print $1\": \"$4}'"


def _set_security_conf_keys(path: str, settings: dict, sep: str = " = ") -> str:
    """Shared updater for simple key/value config files - originally
    written for the "key = value" files under /etc/security/
    (pwquality.conf, faillock.conf), and reused for /etc/login.defs
    (umask policy below) which separates key/value with whitespace
    instead of "=" - pass sep=" " for that case. Updates a key in
    place if it's already present (even commented out), otherwise
    appends it, so re-running this multiple times never leaves
    duplicate or stale lines behind. The match regex accepts either
    style of existing separator so this is backward compatible with
    files only ever written in "=" form."""
    quoted_path = shlex.quote(path)
    steps = [f"touch {quoted_path}"]
    for key, value in settings.items():
        steps.append(
            f"(grep -qE '^[[:space:]]*#?[[:space:]]*{key}([[:space:]]|=)' {quoted_path} "
            f"&& sed -i -E 's/^[[:space:]]*#?[[:space:]]*{key}([[:space:]]|=).*/{key}{sep}{value}/' {quoted_path} "
            f"|| echo '{key}{sep}{value}' >> {quoted_path})"
        )
    return " && ".join(steps)


def cmd_set_password_quality_policy(minlen=None, retry=None, dcredit=None, ucredit=None, lcredit=None, ocredit=None) -> str:
    """Sets password-complexity requirements enforced by pam_pwquality
    (Debian's libpam-pwquality / RHEL's pam_pwquality - both already
    wired into the default password stack on essentially every modern
    distro) by editing the config file the module reads,
    /etc/security/pwquality.conf, rather than touching /etc/pam.d/*
    directly - so this can only loosen or tighten what's required, not
    break the PAM stack itself. Any argument left as None is not
    touched. Credit values follow pwquality's own convention: a
    positive number requires at least that many characters of that
    class; a negative number instead counts toward minlen (e.g.
    ucredit=-1 means "at least one uppercase letter is required")."""
    raw = {
        "minlen": minlen, "retry": retry, "dcredit": dcredit,
        "ucredit": ucredit, "lcredit": lcredit, "ocredit": ocredit,
    }
    settings = {k: int(v) for k, v in raw.items() if v is not None}
    if not settings:
        raise ValueError("Specify at least one password-quality setting")
    return _set_security_conf_keys("/etc/security/pwquality.conf", settings)


def cmd_set_account_lockout_policy(deny=None, unlock_time=None) -> str:
    """Sets failed-login lockout behavior enforced by pam_faillock by
    editing /etc/security/faillock.conf (same update-in-place-or-
    append approach as cmd_set_password_quality_policy above). deny =
    failed attempts before locking the account; unlock_time = seconds
    before an automatic unlock (0 means an admin must clear it
    manually with `faillock --user <name> --reset`)."""
    raw = {"deny": deny, "unlock_time": unlock_time}
    settings = {k: int(v) for k, v in raw.items() if v is not None}
    if not settings:
        raise ValueError("Specify at least one of deny / unlock_time")
    return _set_security_conf_keys("/etc/security/faillock.conf", settings)


def cmd_set_umask_policy(value: str) -> str:
    """Sets the system-wide default umask by editing /etc/login.defs'
    UMASK key - read by useradd when creating new home directories and
    by login/su for interactive sessions on essentially every modern
    distro. Same update-in-place-or-append approach as
    _set_security_conf_keys above, just with login.defs' "KEY value"
    spacing (sep=" ") instead of pwquality/faillock's "key = value"."""
    value = value.strip()
    if not re.fullmatch(r"[0-7]{3,4}", value):
        raise ValueError("Umask must be an octal value like 027 or 0027")
    return _set_security_conf_keys("/etc/login.defs", {"UMASK": value}, sep=" ")


def cmd_set_sudo_policy(timestamp_timeout=None, require_password=None, group: str = "sudo") -> str:
    """Writes a validated drop-in to /etc/sudoers.d/ rather than editing
    /etc/sudoers directly - the standard, safer way to customize sudo
    behavior, since a bad edit to /etc/sudoers itself can lock root out
    entirely. `group` is the admin group sudo rules apply to ("sudo"
    on Debian/Ubuntu, "wheel" on RHEL/Fedora). timestamp_timeout is
    minutes a sudo session stays valid before re-prompting for a
    password (sudoers' own unit); require_password=False instead
    installs a NOPASSWD rule for the group. Either argument left as
    None is not touched. The drop-in is written to a temp file and
    checked with `visudo -c -f` *before* being installed, so a
    malformed policy never reaches a file sudo actually reads."""
    lines = []
    if timestamp_timeout is not None:
        lines.append(f"Defaults timestamp_timeout={int(timestamp_timeout)}")
    if require_password is not None:
        rule = "ALL=(ALL:ALL) ALL" if require_password else "ALL=(ALL:ALL) NOPASSWD: ALL"
        lines.append(f"%{group} {rule}")
    if not lines:
        raise ValueError("Specify at least one of timestamp_timeout / require_password")

    body = "\n".join(lines) + "\n"
    tmp = "/tmp/.sysible_sudoers_policy"
    dest = "/etc/sudoers.d/sysible-policy"
    quoted_tmp = shlex.quote(tmp)
    quoted_dest = shlex.quote(dest)
    return (
        f"cat > {quoted_tmp} <<'SYSIBLE_EOF'\n{body}SYSIBLE_EOF\n"
        f"&& chmod 440 {quoted_tmp} "
        f"&& visudo -c -f {quoted_tmp} "
        f"&& mv {quoted_tmp} {quoted_dest} "
        f"&& chown root:root {quoted_dest} "
        f"&& chmod 440 {quoted_dest}"
    )


# =========================================================
# SYSTEM ADMINISTRATION (dual-host helpers)
#
# The System Administration page needs to run the same action against
# a mix of agent-enrolled hosts (async task queue - see FLEET USER
# MANAGEMENT above) and SSH-enrolled hosts (synchronous one-shot exec -
# see REMOTE HOSTS above) from a single button click. These helpers
# merge both host kinds into one list and hide the two different
# dispatch mechanisms behind one call, so a page only has to branch on
# the *result shape* it gets back ("sync": True/False), not re-derive
# host-kind branching at every call site.
# =========================================================
def merge_duplicate_host_entries(entries):
    """Collapse an agent entry and an SSH entry that share the exact
    same hostname into a single display row.

    Without this, the same physical machine enrolled both ways (agent +
    SSH) shows up as two separate, near-identical rows - same name,
    same environment, differing only in Type/Address - which reads as
    a confusing duplicate rather than one host reachable two ways.
    Shared by every page that lists hosts via list_merged_hosts()
    below (System Health & Logs, User & Group Administration, Service
    Management) - originally written for, and still also used directly
    by, Remote Host Administration's own host table."""
    by_label = {}
    order = []

    for e in entries:
        by_label.setdefault(e["label"], []).append(e)
        if e["label"] not in order:
            order.append(e["label"])

    merged = []

    for label in order:
        group = by_label[label]
        agent_entry = next((e for e in group if e["kind"] == "agent"), None)
        ssh_entry = next((e for e in group if e["kind"] == "ssh"), None)

        if agent_entry and ssh_entry:
            merged.append({
                "kind": "merged",
                "id": label,
                "label": label,
                "type_text": "Agent + SSH",
                "address": f"agent: {agent_entry['address']}   |   ssh: {ssh_entry['address']}",
                "environment": agent_entry.get("environment") or ssh_entry.get("environment") or "",
                "agent_entry": agent_entry,
                "ssh_entry": ssh_entry,
            })
            # Extremely unlikely (e.g. two agents reporting the same
            # hostname), but don't silently drop anything beyond the
            # first agent/SSH pair - list it on its own row instead.
            for extra in group:
                if extra is not agent_entry and extra is not ssh_entry:
                    merged.append(extra)
        else:
            merged.extend(group)

    return merged


def _underlying_entry(entry):
    """A "merged" entry (merge_duplicate_host_entries() above) has both
    an agent and an SSH connection for the same physical host - prefer
    SSH (a real, synchronous connection, and the only option that
    supports an actual interactive terminal) the same way Remote Host
    Administration's terminal session picker does, falling back to
    agent only if SSH is somehow missing. Entries that aren't merged
    pass through untouched."""
    if entry["kind"] == "merged":
        return entry["ssh_entry"] or entry["agent_entry"]
    return entry


def list_merged_hosts():
    """Agent hosts + SSH hosts as one list of dicts: {"kind": "agent"|
    "ssh"|"merged", "id", "label", "type_text", "address",
    "environment"} (a "merged" entry additionally carries "agent_entry"/
    "ssh_entry" - see merge_duplicate_host_entries()) - the same shape
    Remote Administration builds internally for its own host list,
    exposed here so other pages (System Administration) can target
    both kinds, with duplicates already collapsed, without
    re-implementing the merge."""
    entries = []

    try:
        agents = get_agents()
    except Exception:
        agents = []

    for a in agents:
        host_id = a.get("host_id")
        entries.append({
            "kind": "agent",
            "id": host_id,
            "label": a.get("hostname") or host_id,
            "type_text": "Agent",
            "address": a.get("ip") or host_id,
            "environment": a.get("environment") or "",
        })

    try:
        ssh_hosts = list_hosts()
    except Exception:
        ssh_hosts = {}

    for name, h in (ssh_hosts or {}).items():
        entries.append({
            "kind": "ssh",
            "id": name,
            "label": name,
            "type_text": "SSH",
            "address": f"{h.get('user', 'root')}@{h.get('ip', '?')}",
            "environment": h.get("environment") or "",
        })

    return merge_duplicate_host_entries(entries)


def run_on_entry(entry, command: str, kind: str = "command"):
    """Run `command` on one merged-host entry (as produced by
    list_merged_hosts()). SSH executes synchronously over exec_remote()
    - the result is ready immediately. Agent dispatch is async - only a
    task_id comes back, and the caller must poll poll_entry_result()
    until it resolves. Always returns a dict with a "sync" flag so
    callers can branch on which case they got:
      {"sync": True,  "stdout", "stderr", "code", "error"}   (ssh, done)
      {"sync": False, "task_id", "error"}                    (agent, pending)
    """
    entry = _underlying_entry(entry)

    if entry["kind"] == "ssh":
        try:
            result = exec_remote(entry["id"], command)
            return {
                "sync": True,
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "code": result.get("code"),
                "error": None,
            }
        except Exception as e:
            return {"sync": True, "stdout": "", "stderr": "", "code": None, "error": str(e)}

    task_ids = queue_command_on_hosts([entry["id"]], command, kind=kind)
    task_id = task_ids.get(entry["id"])
    return {
        "sync": False,
        "task_id": task_id,
        "error": None if task_id is not None else "failed to queue task",
    }


def poll_entry_result(entry, task_id):
    """For an agent entry previously dispatched via run_on_entry() -
    returns the same {"stdout","stderr","code","error"} shape once the
    agent has reported back, or None if it's still pending."""
    entry = _underlying_entry(entry)
    raw = get_result_by_task(entry["id"], task_id)
    output = parse_task_output(raw)

    if output is None:
        return None

    return {
        "stdout": output.get("stdout", ""),
        "stderr": output.get("stderr", ""),
        "code": output.get("returncode"),
        "error": None,
    }


def sync_entry_users(entry):
    """Mirrors sync_hosts()/get_sync_result() (agent task-queue) but for
    a single merged entry of either kind. SSH executes synchronously, so
    the parsed {"users","groups","sessions"} dict is ready right away;
    agent dispatch only returns a task_id - poll with
    poll_entry_sync_result()."""
    entry = _underlying_entry(entry)

    if entry["kind"] == "ssh":
        try:
            result = exec_remote(entry["id"], build_user_sync_command())
        except Exception as e:
            return {"sync": True, "data": None, "error": str(e)}

        if result.get("code") != 0:
            return {
                "sync": True,
                "data": None,
                "error": result.get("stderr") or f"sync exited {result.get('code')}",
            }

        try:
            return {"sync": True, "data": json.loads(result["stdout"]), "error": None}
        except (TypeError, ValueError, KeyError):
            return {"sync": True, "data": None, "error": "could not parse sync output"}

    task_ids = sync_hosts([entry["id"]])
    task_id = task_ids.get(entry["id"])
    return {
        "sync": False,
        "task_id": task_id,
        "error": None if task_id is not None else "failed to queue sync",
    }


def poll_entry_sync_result(entry, task_id):
    """Agent-side counterpart to sync_entry_users()'s synchronous SSH
    branch - returns {"data": {...}, "error": None} once the agent has
    reported back, or None if the result hasn't arrived yet."""
    entry = _underlying_entry(entry)
    raw = get_result_by_task(entry["id"], task_id)
    output = parse_task_output(raw)

    if output is None:
        return None

    if output.get("returncode") != 0:
        return {"data": None, "error": output.get("stderr") or "sync failed"}

    try:
        return {"data": json.loads(output["stdout"]), "error": None}
    except (TypeError, ValueError, KeyError):
        return {"data": None, "error": "could not parse sync output"}


# ---------------------------------------------------------
# System Health & Logs (read-only command builders - safe to run on
# either host kind via run_on_entry() above; nothing here ever embeds
# a secret, so there's no password-hashing concern like the FLEET USER
# MANAGEMENT write actions above).
# ---------------------------------------------------------
def cmd_disk_usage() -> str:
    # -T adds the filesystem-type column (e.g. ext4, squashfs, iso9660)
    # so a human looking at the raw report can immediately tell a snap
    # package or mounted install ISO apart from a real disk - the same
    # distinction cmd_health_check() below makes automatically when
    # deciding what counts toward a CRITICAL verdict.
    return "df -hT"


def cmd_memory_cpu_snapshot() -> str:
    """Leads with a computed "Memory usage: NN% (OK/WARNING/CRITICAL)"
    line, same idea as cmd_health_check()'s disk/load scoring - free -h
    alone makes the operator do the percentage math and decide for
    themselves whether it's fine; this does that math up front so the
    one stat that actually matters is visible without reading the rest
    of the table."""
    return (
        "mem_pct=$(LANG=C free -m 2>/dev/null | awk 'NR==2 && $2>0 {printf \"%.0f\", $3/$2*100}'); "
        "mem_status=OK; "
        "if [ -n \"$mem_pct\" ]; then "
        "if [ \"$mem_pct\" -ge 90 ] 2>/dev/null; then mem_status=CRITICAL; "
        "elif [ \"$mem_pct\" -ge 75 ] 2>/dev/null; then mem_status=WARNING; fi; "
        "fi; "
        "echo \"Memory usage: ${mem_pct:-unknown}% ($mem_status)\" && echo "
        "&& echo '== Memory ==' && free -h && echo "
        "&& echo '== Load / Uptime ==' && uptime && echo "
        "&& echo '== Top CPU Processes ==' "
        "&& ps -eo pid,comm,%cpu,%mem --sort=-%cpu | head -n 11"
    )


def cmd_find_large_files(path: str = "/", top_n: int = 20) -> str:
    path = path.strip() if path and path.strip() else "/"
    top_n = max(1, int(top_n))
    return (
        f"find {shlex.quote(path)} -xdev -type f -printf '%s %p\\n' 2>/dev/null "
        f"| sort -rn | head -n {top_n} "
        "| awk '{printf \"%.1f MB  %s\\n\", $1/1024/1024, $2}'"
    )


def cmd_failed_services() -> str:
    """Empty output from `systemctl --failed` is ambiguous - it could
    mean "no failed services" (good) or get misread as "this command
    didn't work". Say which one it actually is instead of returning
    nothing."""
    return (
        "if ! command -v systemctl >/dev/null 2>&1; then "
        "echo 'systemctl not available on this host'; "
        "else "
        "out=$(systemctl --failed --no-legend 2>/dev/null); "
        "if [ -z \"$out\" ]; then echo 'No failed services.'; else echo \"$out\"; fi; "
        "fi"
    )


def cmd_uptime() -> str:
    return "uptime"
