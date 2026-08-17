"""
Single place where the web console talks to the Sysible backend.

Callers should go through here instead of calling `requests`
directly, so the API base URL and the admin API key are only
configured in one spot.
"""

import contextlib
import os
import shlex
import threading
from pathlib import Path
from urllib.parse import quote

import requests

BASE_URL = os.getenv("SYSIBLE_API_URL", "https://127.0.0.1:9000")

_API_KEY_FILE = Path(os.getenv("SYSIBLE_API_KEY_FILE", "/opt/sysible/api_key.txt"))

_CA_CERT_FILE = os.getenv("SYSIBLE_CA_CERT", "/opt/sysible/certs/server.crt")

# TLS trust is PINNED to the controller's cert. If the pin file is missing on an
# https:// endpoint we FAIL CLOSED — falling back to system-CA verification would
# silently drop pinning, so on a PKI-cert deployment any CA in the store could MITM
# the controller channel (which carries the admin API key and operator tokens). An
# operator who intends to trust the public store opts in with SYSIBLE_ALLOW_SYSTEM_CA=1.
_ALLOW_SYSTEM_CA = os.getenv("SYSIBLE_ALLOW_SYSTEM_CA", "0").strip().lower() in ("1", "true", "yes", "on")
if BASE_URL.startswith("https://"):
    if os.path.exists(_CA_CERT_FILE):
        _VERIFY = _CA_CERT_FILE
    elif _ALLOW_SYSTEM_CA:
        print(
            f"[api] warning: no pinned CA cert at {_CA_CERT_FILE}; "
            "SYSIBLE_ALLOW_SYSTEM_CA=1 set, verifying against the SYSTEM trust store "
            "(NOT pinned)."
        )
        _VERIFY = True
    else:
        # FAIL CLOSED: point verify at the (missing) pin path so requests refuses
        # to connect rather than silently downgrading to system-CA verification.
        # Import stays safe (no crash); only a real HTTPS call without a pin fails.
        _VERIFY = _CA_CERT_FILE
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

# RBAC identity token, set after admin_login(). Sent on every request so the
# controller can attribute actions to this admin and tag dispatched tasks
# with an unforgeable initiating username (the agent then runs commands as
# the matching local user). Process-local; cleared on logout.
_ADMIN_TOKEN = None

# Per-thread override of the admin token. The web BFF is a single shared process
# serving every administrator concurrently, so a process-global token is unsafe:
# one request (or fleet-health's parallel probe threads) can read another's
# token. The BFF therefore sets a THREAD-scoped token per request/worker. A
# thread-local value of None
# means "explicitly no token for this thread" (e.g. the read-only fleet-health
# metrics probe, which must run as root regardless of who is viewing) — distinct
# from "no override set", hence the sentinel.
_NO_OVERRIDE = object()
_token_override = threading.local()


def set_admin_token(token):
    global _ADMIN_TOKEN
    _ADMIN_TOKEN = token


def set_admin_token_override(token):
    """Set the admin token for THIS thread only (used by the multi-threaded BFF).
    Pass None to force no token for this thread. Always pair with
    clear_admin_token_override() in a finally."""
    _token_override.value = token


def clear_admin_token_override():
    if hasattr(_token_override, "value"):
        del _token_override.value


def _effective_admin_token():
    v = getattr(_token_override, "value", _NO_OVERRIDE)
    return _ADMIN_TOKEN if v is _NO_OVERRIDE else v


def _headers():
    h = {"X-API-Key": _API_KEY} if _API_KEY else {}
    tok = _effective_admin_token()
    if tok:
        h["X-Sysible-Admin-Token"] = tok
    return h


_SESSION = requests.Session()

# Optional CLIENT certificate for mutual TLS. When the controller is started with
# --mtls it requires callers to present a cert signed by its client-CA; set
# SYSIBLE_CLIENT_CERT (plus SYSIBLE_CLIENT_KEY, unless the cert file already bundles
# the key) so this BFF/CLI presents one. Attached at the session level so it covers
# ping(), every _request(), and _download_binary(). OFF by default: with neither var
# set the session is byte-for-byte the previous behaviour (server-auth TLS only), so
# a non-mTLS controller is unaffected — and a client cert can be pre-provisioned
# before the server enforces mTLS, enabling a two-phase rollout. _VERIFY (server-auth
# pinning) is orthogonal and unchanged.
_CLIENT_CERT = os.getenv("SYSIBLE_CLIENT_CERT")
_CLIENT_KEY = os.getenv("SYSIBLE_CLIENT_KEY")
if _CLIENT_CERT and os.path.exists(_CLIENT_CERT):
    if _CLIENT_KEY and os.path.exists(_CLIENT_KEY):
        _SESSION.cert = (_CLIENT_CERT, _CLIENT_KEY)
    else:
        _SESSION.cert = _CLIENT_CERT
elif _CLIENT_CERT:
    print(f"[api] warning: SYSIBLE_CLIENT_CERT set but not found at {_CLIENT_CERT}; "
          "not presenting a client certificate.")


def ping():
    try:
        r = _SESSION.get(f"{BASE_URL}/", timeout=2, verify=_VERIFY)
        return r.ok
    except requests.RequestException:
        return False


def _request(method, path, **kwargs):
    r = _SESSION.request(
        method, f"{BASE_URL}{path}", headers=_headers(),
        timeout=kwargs.pop("timeout", 15), verify=_VERIFY, **kwargs,
    )
    if not r.ok:
        detail = None
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            pass
        raise requests.exceptions.HTTPError(f"{r.status_code} {detail or r.reason}", response=r)
    if not r.content:
        return None
    return r.json()


def _download_binary(path):
    r = _SESSION.get(f"{BASE_URL}{path}", headers=_headers(), timeout=30, verify=_VERIFY)
    if not r.ok:
        detail = None
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            pass
        raise requests.exceptions.HTTPError(f"{r.status_code} {detail or r.reason}", response=r)
    return r.content


def generate_enroll_token():
    return _request("POST", "/admin/enroll-token/generate")


def reissue_enroll_token(host_id):
    """Mint a host-bound REISSUE token that authorizes re-enrolling ONE existing host
    (e.g. after a reinstall that wiped the agent's saved secret). Ordinary re-enroll of
    an existing host is refused without it, so a bearer token can't hijack a host."""
    return _request("POST", "/admin/enroll-token/reissue", json={"host_id": host_id})


def get_agents():
    return _request("GET", "/agents").get("agents", [])


def log_action(host: str, description: str):
    """Record ONE attributed activity-feed entry — a grouped summary for a
    multi-host tool run, so the feed shows "List disks · dev1, prod1" instead of
    one row per host. See backend POST /activity."""
    return _request("POST", "/activity", json={"host": host, "description": description})


def get_metrics_timeseries(window=3600):
    """Per-host performance time-series (load/mem/disk) for the last `window`
    seconds. Returns {"hosts": [...], "window": int, "now": float}."""
    return _request("GET", f"/metrics/timeseries?window={int(window)}")


def get_host_snapshot(host_id):
    """Latest rich detail snapshot (per-core CPU, memory breakdown, per-interface
    network, per-mount disk, top processes) for one host's metrics drill-down.
    Returns {"host_id", "ts", "snapshot": {...}|None}."""
    from urllib.parse import quote
    return _request("GET", f"/metrics/snapshot/{quote(str(host_id), safe='')}")


def get_fleet_health_readings():
    """Latest fleet-health reading per host from heartbeat data (disk/mem/load +
    failed-units/systemd/OOM), so the dashboard can render health without a live
    probe. Returns {"hosts": {host_id: {...}}, "now": float}."""
    return _request("GET", "/metrics/fleet-health")


def get_edition():
    """Edition + host-cap info, e.g. {"edition": "community", "host_limit": 10,
    "host_count": 3}. host_limit is None on an unlimited (Enterprise) build.
    On error returns {} (not a host_limit of None) so callers can tell
    "backend didn't answer" apart from "explicitly unlimited"."""
    try:
        return _request("GET", "/edition")
    except Exception:
        return {}


def disenroll_agent(host_id: str, purge_token: bool = False):
    # purge_token=True (Force Delete) also kills the bound enrollment token so a
    # zombie agent can't re-enroll onto the same host_id and resurrect the record.
    path = f"/agents/{host_id}" + ("?purge_token=1" if purge_token else "")
    return _request("DELETE", path)


def get_enrollment_pause():
    """Whether new agent enrollment is currently paused (runaway kill-switch)."""
    return _request("GET", "/admin/enrollment-pause")


def set_enrollment_pause(paused: bool, actor: str = ""):
    """Pause/resume all new agent enrollment. Superuser-only on the controller."""
    return _request("POST", "/admin/enrollment-pause",
                    json={"paused": bool(paused), "actor": actor})


def get_enroll_allowlist():
    """The enrollment source-IP allowlist (empty == all sources allowed)."""
    return _request("GET", "/admin/enroll-allowlist")


def add_enroll_allowlist(cidr: str, note: str = ""):
    """Add an allowed source CIDR/IP to the enrollment allowlist. Superuser-only."""
    return _request("POST", "/admin/enroll-allowlist",
                    json={"cidr": cidr, "note": note})


def remove_enroll_allowlist(entry_id):
    """Remove an enrollment-allowlist entry by id. Superuser-only."""
    return _request("DELETE", f"/admin/enroll-allowlist/{entry_id}")


def revoke_agent(host_id: str):
    """Hard lock-out: revoke the host's agent secret so it can't heartbeat,
    poll, or report until an admin re-enrolls it. Superuser-only on the backend.
    Used to cut off a host suspected of being compromised/tampered."""
    return _request("POST", f"/agents/{host_id}/revoke")


def resume_agent(host_id: str):
    """Clear an integrity quarantine: re-seal the host's baseline to its current
    measurements and resume dispatching tasks to it. Superuser-only."""
    return _request("POST", f"/agents/{host_id}/resume")


def restore_agent(host_id: str):
    """Undo a hard REVOCATION in place, keeping the existing agent secret so a
    still-installed agent resumes immediately — no re-enroll. Superuser-only."""
    return _request("POST", f"/agents/{host_id}/restore")


def set_agent_environment(host_id: str, environment: str):
    # Idempotent DB-only write. Allow a longer read timeout than the 15s default:
    # the controller's SQLite busy_timeout is 30s, so under write contention a
    # legitimate write can take up to ~30s — with the default 15s the console
    # would give up first and surface a spurious "read timeout=15" for an
    # assignment that actually succeeds a moment later. 35s clears busy_timeout.
    return _request("POST", f"/agents/{host_id}/environment",
                    json={"environment": environment}, timeout=35)


_AGENT_SERVICE_NAME = "sysible-agent"
_AGENT_INSTALL_DIR = "/opt/sysible-agent"
_AGENT_CERT_DIR = "/etc/sysible"
_AGENT_CERT_PATH = f"{_AGENT_CERT_DIR}/controller.crt"
_AGENT_STATE_DIR = "/var/lib/sysible"
_AGENT_STATE_FILE = f"{_AGENT_STATE_DIR}/agent_state.json"


def cmd_uninstall_agent_service() -> str:
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
    # IMPORTANT: this must exit NON-ZERO when systemd-run can't schedule the
    # teardown. systemd-run needs root; when the agent runs this as the
    # operator's (non-root) local user it hits polkit ("Interactive
    # authentication required"). The agent only retries a command under sudo if
    # the first, unprivileged attempt FAILS - so if we swallowed that failure
    # into an exit 0 (as this used to), the agent would think it succeeded and
    # never escalate, and the service would be left running. Exiting 1 (with the
    # polkit error still on stderr) is what triggers the agent's sudo retry.
    return (
        f"if ! command -v systemd-run >/dev/null 2>&1; then "
        f"echo 'WARNING: systemd-run not found - could not schedule automatic "
        f"teardown. Run disenroll_agent.sh on this host instead.' >&2; exit 1; fi; "
        f"if systemd-run --quiet --on-active=2 --unit={_AGENT_SERVICE_NAME}-uninstall "
        f"/bin/bash -c {quoted}; then "
        f"echo 'Uninstall scheduled - the systemd service and agent files "
        f"will be removed within a few seconds.'; "
        f"else "
        f"echo 'WARNING: systemd-run failed to schedule teardown - service "
        f"left running. Run disenroll_agent.sh on this host instead.' >&2; exit 1; fi"
    )


def list_environments():
    return _request("GET", "/environments").get("environments", [])


def create_environment(name: str):
    return _request("POST", "/environments", json={"name": name}).get("environments", [])


def delete_environment(name: str):
    return _request("DELETE", f"/environments/{name}").get("environments", [])


def get_controller_config():
    return _request("GET", "/controller-config")


def set_controller_config(hostname: str, ip: str, address_mode: str, port: int):
    return _request(
        "POST", "/controller-config",
        json={"hostname": hostname, "ip": ip, "address_mode": address_mode, "port": port},
    )


def get_version():
    """Which directory + commit the live controller is running (deployment guard)."""
    return _request("GET", "/version")


def controller_restart():
    """Restart the controller backend service (no update). Returns immediately;
    systemd bounces sysible-backend and it comes back on its own. The web console
    is a separate service and stays up, so the operator's session survives."""
    return _request("POST", "/controller/restart", timeout=30)


def controller_update():
    """Trigger an in-place controller self-update (git pull + redeploy + restart)
    on the controller host. The controller launches it as a detached transient
    unit and returns immediately; the backend and web console then restart."""
    return _request("POST", "/controller/update", timeout=30)


def rebuild_webgui():
    """Rebuild the web console front end (npm install + npm run build) on the controller
    host. Synchronous; returns each build step's output. Generous timeout — the build can
    take a while on first run."""
    return _request("POST", "/controller/webgui/rebuild", timeout=900)


def controller_update_status():
    """Outcome of the most recent controller self-update:
    {"status": running|success|failed|none|unknown, "message", "version", "ts",
    "age_seconds"}. Lets the caller confirm the update actually applied (or show
    why it didn't) instead of inferring success from a service restart."""
    return _request("GET", "/controller/update-status", timeout=15)


def controller_update_log(lines: int = 400):
    """Recent output of the controller self-update (the commands it's running:
    git pull, rsync, rebuild, restart). Returns {"log": "..."}."""
    return _request("GET", "/controller/update-log", params={"lines": lines}, timeout=15)


def update_agents():
    """Push the controller's current agent to every managed host over the
    existing task channel. Returns {"queued", "version", "message"}. Agents
    apply it on their next check-in and restart with the new code."""
    return _request("POST", "/agents/update", timeout=30)


def get_update_status():
    """Whether a software update is available for the controller (git-behind its
    remote) and/or agents (reported build hash != controller's current agent
    build). Returns {"controller": {...}, "agents": {...}}. Does a live git
    fetch on the controller, so call on demand, not on a poll."""
    return _request("GET", "/update-status", timeout=45)


def get_license_config():
    return _request("GET", "/license-config")


def set_license_config(license_key: str):
    return _request("POST", "/license-config", json={"license_key": license_key})


def get_local_ips():
    return _request("GET", "/controller-config/local-ips").get("ips", [])


def download_agent_bundle(save_path):
    data = _download_binary("/controller-config/agent-bundle")
    Path(save_path).write_bytes(data)
    return save_path


def get_tls_info():
    return _request("GET", "/controller-config/tls/info")


def install_tls_certificate(cert_path, key_path, chain_path=None):
    """Uploads a cert/key(/optional chain) for the controller to
    validate and install as its TLS identity (see
    backend/tls_manager.py) - the backend restarts itself right after a
    successful install, so the caller should emit
    client/events.py's bus.backend_restart_expected signal first so the
    GUI's backend watchdog doesn't mistake the restart for a crash."""
    cert_path, key_path = Path(cert_path), Path(key_path)
    with contextlib.ExitStack() as stack:
        files = {
            "cert_file": (cert_path.name, stack.enter_context(open(cert_path, "rb"))),
            "key_file": (key_path.name, stack.enter_context(open(key_path, "rb"))),
        }
        if chain_path:
            chain_path = Path(chain_path)
            files["chain_file"] = (chain_path.name, stack.enter_context(open(chain_path, "rb")))
        return _request("POST", "/controller-config/tls/install", files=files, timeout=30)


def regenerate_self_signed_cert():
    """Ask the controller to regenerate its self-signed cert for the CURRENT
    hostname/IP and restart to serve it (the backend bounces itself right after,
    same as install_tls_certificate)."""
    return _request("POST", "/controller-config/tls/regenerate-self-signed", timeout=30)


def migrate_agents(new_url: str, host_ids):
    """Same-identity migration: re-point the given agents at a NEW controller address
    (shared-database failover / IP change / DNS cutover). The controller enqueues a
    re-point-and-restart task per host. Superuser-gated on the controller."""
    return _request("POST", "/controller/migrate-agents",
                    json={"new_url": new_url, "host_ids": list(host_ids or [])})


def download_trust_certificate(save_path):
    data = _download_binary("/controller-config/tls/trust-bundle")
    Path(save_path).write_bytes(data)
    return save_path


def get_environmental_policy():
    return _request("GET", "/environmental-policy")


def set_environmental_policy(policy: dict):
    return _request("POST", "/environmental-policy", json=policy)


def set_sudo_password_required(host_id: str, required: bool):
    return _request("POST", f"/agents/{host_id}/sudo-password-required",
                    json={"required": bool(required)})


def get_environment_sudo_defaults():
    return _request("GET", "/environments/sudo-defaults").get("defaults", {})


def set_environment_sudo_default(name: str, required: bool):
    return _request("POST", f"/environments/{name}/sudo-default",
                    json={"required": bool(required)})


def get_activity_log(limit: int = 200, since_id: int = 0):
    return _request("GET", "/activity-log",
                    params={"limit": limit, "since_id": since_id}).get("entries", [])


def get_controller_log(lines: int = 400):
    return _request("GET", "/controller-log", params={"lines": lines}).get("log", "")


def health_warnings():
    """Operational warning banners (stale pinned TLS cert, mass host silence)."""
    return _request("GET", "/admin/health-warnings")


def get_portal_status():
    return _request("GET", "/portal/status")


def start_portal():
    return _request("POST", "/portal/start")


def stop_portal():
    return _request("POST", "/portal/stop")


def set_portal_credentials(username: str, password: str, current_password: str = ""):
    return _request("POST", "/portal/credentials", json={
        "username": username, "password": password, "current_password": current_password,
    })


def remove_portal_credentials(current_password: str):
    return _request("DELETE", "/portal/credentials", json={"current_password": current_password})


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


def admin_setup_required():
    return _request("GET", "/admin/setup-required").get("setup_required", False)


def admin_setup(username: str, password: str):
    result = _request("POST", "/admin/setup", json={"username": username, "password": password})
    # Creating the first admin logs them straight in - capture the RBAC token
    # the backend now returns so superuser actions work immediately, exactly
    # as admin_login() does. Without this the GUI held no token after setup.
    if isinstance(result, dict) and result.get("token"):
        set_admin_token(result["token"])
    return result


def admin_login(username: str, password: str):
    result = _request("POST", "/admin/login", json={"username": username, "password": password})
    # Capture the RBAC identity token so every subsequent request is
    # attributed to this admin (and dispatched tasks run as their local user).
    if isinstance(result, dict) and result.get("token"):
        set_admin_token(result["token"])
    return result


def admin_logout():
    """Revoke this session's RBAC token server-side and clear it locally.
    Best-effort: the local token is cleared even if the network call fails,
    so the GUI never stays 'logged in' on the client after a logout."""
    try:
        _request("POST", "/admin/logout")
    except Exception:
        pass
    finally:
        set_admin_token(None)


def whoami():
    """Resolve this session's admin token to its LIVE {username, role}, or raise
    if the controller rejects it (account removed, role changed, expired). The
    console uses this to drop a stale session promptly instead of trusting the
    signed cookie until token expiry."""
    return _request("GET", "/admin/whoami")


def list_administrators():
    return _request("GET", "/admin/administrators").get("administrators", [])


def add_administrator(username: str, password: str, actor: str = "", role: str = "sysadmin"):
    return _request(
        "POST", "/admin/administrators",
        json={"username": username, "password": password, "actor": actor, "role": role},
    )


def remove_administrator(username: str, actor: str = ""):
    return _request("DELETE", f"/admin/administrators/{username}", params={"actor": actor})


def change_admin_credentials(username: str, current_password: str, new_username: str, new_password: str):
    return _request(
        "POST", "/admin/credentials",
        json={
            "username": username, "current_password": current_password,
            "new_username": new_username, "new_password": new_password,
        },
    )


def force_admin_password_change(username: str, current_password: str, new_password: str):
    return _request(
        "POST", "/admin/force-password-change",
        json={"username": username, "current_password": current_password, "new_password": new_password},
    )


def reset_administrator_password(username: str, new_password: str, actor: str = ""):
    return _request(
        "POST", f"/admin/administrators/{username}/password",
        json={"new_password": new_password, "actor": actor},
    )


def set_administrator_sudo_connect(username: str, allowed: bool, actor: str = ""):
    """Superuser-only: grant/revoke this admin's use of the Sysible Connect
    terminal's 'Send sudo password' button."""
    return _request(
        "POST", f"/admin/administrators/{quote(username)}/sudo-connect",
        json={"allowed": bool(allowed), "actor": actor},
    )


def set_administrator_role(username: str, role: str, actor: str = ""):
    """Superuser-only: promote/demote an administrator's role
    ('superuser' | 'sysadmin' | 'auditor')."""
    return _request(
        "POST", f"/admin/administrators/{quote(username)}/role",
        json={"role": role, "actor": actor},
    )


def get_admin_audit_log(limit: int = 200):
    return _request("GET", "/admin/audit-log", params={"limit": limit}).get("entries", [])


def get_admin_password_policy():
    return _request("GET", "/admin/password-policy")


def set_admin_password_policy(policy: dict):
    return _request("POST", "/admin/password-policy", json=policy)


def list_portal_uploads():
    return _request("GET", "/portal/files/uploads").get("files", [])


def download_portal_upload(filename: str, save_path):
    data = _download_binary(f"/portal/files/uploads/{quote(filename, safe='')}")
    Path(save_path).write_bytes(data)
    return save_path


def delete_portal_upload(filename: str):
    return _request("DELETE", f"/portal/files/uploads/{quote(filename, safe='')}")


def list_portal_downloads():
    return _request("GET", "/portal/files/downloads").get("files", [])


def stage_portal_download(local_path):
    local_path = Path(local_path)
    with open(local_path, "rb") as f:
        return _request("POST", "/portal/files/downloads", files={"file": (local_path.name, f)})


def delete_portal_download(filename: str):
    return _request("DELETE", f"/portal/files/downloads/{quote(filename, safe='')}")


# ---------------------------------------------------------------------------
# The rest of this module's public surface (users/groups/remote hosts/file
# transfer/fleet user mgmt/password helpers, system-administration dual-host
# dispatch + health&logs, process & service management, cron & timers, host
# software & repository management, network management, and file system
# management) lives in sibling _api_*.py modules and is re-exported here via
# wildcard import so every existing call site (api.list_merged_hosts(),
# api.cmd_ping(), etc.) keeps working unmodified. Split out purely to keep
# individual file sizes manageable - this is still one logical module.
# ---------------------------------------------------------------------------
from client._api_users import *  # noqa: E402,F401,F403
from client._api_dispatch import *  # noqa: E402,F401,F403
from client._api_process_service import *  # noqa: E402,F401,F403
from client._api_automation import *  # noqa: E402,F401,F403
from client._api_repo import *  # noqa: E402,F401,F403
from client._api_network import *  # noqa: E402,F401,F403
from client._api_filesystem import *  # noqa: E402,F401,F403
from client._api_filesystem_mount import *  # noqa: E402,F401,F403
from client._api_storage import *  # noqa: E402,F401,F403
from client._api_firewall import *  # noqa: E402,F401,F403
from client._api_security import *  # noqa: E402,F401,F403
from client._api_backup import *  # noqa: E402,F401,F403
from client._api_boot import *  # noqa: E402,F401,F403
from client._api_timesync import *  # noqa: E402,F401,F403
from client._api_certs import *  # noqa: E402,F401,F403
from client._api_containers import *  # noqa: E402,F401,F403
from client._api_directory import *  # noqa: E402,F401,F403
from client._api_subscriptions import *  # noqa: E402,F401,F403
