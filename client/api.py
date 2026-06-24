"""
Single place where the desktop client talks to the Sysible backend.

Every page should go through here instead of calling `requests`
directly, so the API base URL and the admin API key are only
configured in one spot.
"""

import contextlib
import os
import shlex
from pathlib import Path
from urllib.parse import quote

import requests

BASE_URL = os.getenv("SYSIBLE_API_URL", "https://127.0.0.1:9000")

_API_KEY_FILE = Path(os.getenv("SYSIBLE_API_KEY_FILE", "/opt/sysible/api_key.txt"))

_CA_CERT_FILE = os.getenv("SYSIBLE_CA_CERT", "/opt/sysible/certs/server.crt")

if BASE_URL.startswith("https://"):
    if os.path.exists(_CA_CERT_FILE):
        _VERIFY = _CA_CERT_FILE
    else:
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

# RBAC identity token, set after admin_login(). Sent on every request so the
# controller can attribute actions to this admin and tag dispatched tasks
# with an unforgeable initiating username (the agent then runs commands as
# the matching local user). Process-local; cleared on logout.
_ADMIN_TOKEN = None


def set_admin_token(token):
    global _ADMIN_TOKEN
    _ADMIN_TOKEN = token


def _headers():
    h = {"X-API-Key": _API_KEY} if _API_KEY else {}
    if _ADMIN_TOKEN:
        h["X-Sysible-Admin-Token"] = _ADMIN_TOKEN
    return h


_SESSION = requests.Session()


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


def get_agents():
    return _request("GET", "/agents").get("agents", [])


def get_edition():
    """Edition + host-cap info, e.g. {"edition": "community", "host_limit": 10,
    "host_count": 3}. host_limit is None on an unlimited (Enterprise) build.
    On error returns {} (not a host_limit of None) so callers can tell
    "backend didn't answer" apart from "explicitly unlimited"."""
    try:
        return _request("GET", "/edition")
    except Exception:
        return {}


def disenroll_agent(host_id: str):
    return _request("DELETE", f"/agents/{host_id}")


def set_agent_environment(host_id: str, environment: str):
    return _request("POST", f"/agents/{host_id}/environment", json={"environment": environment})


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


def get_activity_log(limit: int = 200, since_id: int = 0):
    return _request("GET", "/activity-log",
                    params={"limit": limit, "since_id": since_id}).get("entries", [])


def get_controller_log(lines: int = 400):
    return _request("GET", "/controller-log", params={"lines": lines}).get("log", "")


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
    return _request("POST", "/admin/setup", json={"username": username, "password": password})


def admin_login(username: str, password: str):
    result = _request("POST", "/admin/login", json={"username": username, "password": password})
    # Capture the RBAC identity token so every subsequent request is
    # attributed to this admin (and dispatched tasks run as their local user).
    if isinstance(result, dict) and result.get("token"):
        set_admin_token(result["token"])
    return result


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
