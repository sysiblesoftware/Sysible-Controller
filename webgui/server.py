"""
Sysible Web GUI - a separate, browser-based front end to the Sysible
Controller, intended for Windows (and any) machines that can't run the
PySide6 desktop client but can reach the controller over the network.

Architecture (why it's shaped this way)
---------------------------------------
This is a thin **backend-for-frontend (BFF)**. It does NOT re-implement
any host-management logic. Instead it imports the desktop client's
existing, battle-tested helpers:

  * client.api               - admin login, agents, edition, base-URL/key
  * client._api_dispatch     - list_merged_hosts(), run_on_entry(),
                               poll_entry_result()  (agent-queue vs. SSH
                               dispatch hidden behind one call)
  * client._api_* (cmd_*)    - the hundreds of pure-Python shell-command
                               builders the desktop tools already use

The React SPA never builds shell commands and never sees the controller
API key. It sends {action, params, targets}; this service looks the
action up in webgui/actions.py, calls the matching cmd_* builder to get
the exact same shell string the desktop app would run, dispatches it
across the selected hosts, and returns per-host results. Reaching full
desktop parity is therefore a matter of registering more actions, not
re-writing dispatch logic per tool.

Auth: the browser logs in with the same administrator credentials the
desktop app uses. We verify them against the controller's /admin/login
(through client.api, which holds the API key server-side) and then set a
signed, http-only session cookie via Starlette's SessionMiddleware. The
API key stays on this server; it is never exposed to the browser.

Run:
    cd webgui
    pip install -r requirements.txt
    # the controller API key + base URL are read by client.api from the
    # usual env/files (SYSIBLE_API_BASE_URL, SYSIBLE_API_KEY / key file)
    SYSIBLE_WEBGUI_SECRET="<random>" uvicorn server:app --host 0.0.0.0 --port 8800

Serve the built SPA (frontend/dist) from the same service, or put both
behind a TLS-terminating reverse proxy. See README.md.
"""
import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path

# Make the repo root importable so `import client.*` works whether this
# service is launched from webgui/ or the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import (
    FastAPI, HTTPException, Request, Depends, WebSocket, WebSocketDisconnect,
    UploadFile, File, Form,
)
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from client import api
from client import _api_dispatch as dispatch
from webgui import actions


app = FastAPI(title="Sysible Web GUI")

# Signed http-only session cookie. Set SYSIBLE_WEBGUI_SECRET in the
# environment for a stable secret across restarts; a random per-process
# secret (the fallback) logs everyone out whenever the service restarts.
# (webgui_manager persists one to run/webgui.secret so the deployed
# service keeps sessions valid across restarts.)
_SECRET = os.getenv("SYSIBLE_WEBGUI_SECRET") or secrets.token_hex(32)

# Sessions expire so an unattended browser doesn't stay logged in forever.
# Default 12h; override with SYSIBLE_WEBGUI_SESSION_MAX_AGE (seconds).
try:
    _SESSION_MAX_AGE = int(os.getenv("SYSIBLE_WEBGUI_SESSION_MAX_AGE", "43200"))
except ValueError:
    _SESSION_MAX_AGE = 43200

# Mark the cookie Secure when we're behind TLS (set by webgui_manager when the
# controller has certs). same_site="strict" is right for an admin console:
# the cookie is never attached to cross-site requests, which closes CSRF on
# the state-changing POSTs without needing a separate token.
_HTTPS_ONLY = os.getenv("SYSIBLE_WEBGUI_HTTPS_ONLY", "0") == "1"
app.add_middleware(
    SessionMiddleware,
    secret_key=_SECRET,
    session_cookie="sysible_web",
    https_only=_HTTPS_ONLY,
    same_site="strict",
    max_age=_SESSION_MAX_AGE,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Defense-in-depth headers for a console that may be exposed on a
    network. CSP keeps everything same-origin (the SPA bundles all its
    own assets); 'unsafe-inline' style is needed because xterm.js injects
    inline styles, and connect-src 'self' covers the same-origin
    terminal websocket."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if _HTTPS_ONLY:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


_FRONTEND_DIST = _REPO_ROOT / "webgui" / "frontend" / "dist"


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------
def require_login(request: Request):
    """Dependency: 401 unless the session cookie carries a logged-in
    admin username. Every /api route except /api/login uses this."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


# In-memory login throttle: slows password guessing against an exposed
# console without a datastore. Per client IP, allow a burst then lock out
# for a cooldown. Successful login clears the counter. State is per-process;
# the deployed service is a single uvicorn process, so that's sufficient.
_LOGIN_MAX_ATTEMPTS = int(os.getenv("SYSIBLE_WEBGUI_LOGIN_MAX_ATTEMPTS", "8"))
_LOGIN_WINDOW_S = int(os.getenv("SYSIBLE_WEBGUI_LOGIN_WINDOW", "300"))
_login_attempts: dict[str, list] = {}

# The controller admin token is a privileged bearer credential. We keep it in
# the session cookie but ENCRYPTED with a server-side key (the same 0600 key
# the sudo store uses): the browser only ever sees ciphertext (can't read the
# token out of the cookie), and because the key is persisted on disk this
# survives a controller restart — unlike an in-memory store, which would log
# everyone out of superuser actions on every restart.
def _token_cipher():
    try:
        from cryptography.fernet import Fernet
        from webgui import sudo_store
        key = sudo_store._get_key()
        return Fernet(key) if key else None
    except Exception:
        return None


def _encrypt_token(token: str):
    c = _token_cipher()
    if not c or not token:
        return None
    try:
        return c.encrypt(token.encode()).decode()
    except Exception:
        return None


def _session_token(request: "Request"):
    enc = request.session.get("token_enc")
    if not enc:
        return None
    c = _token_cipher()
    if not c:
        return None
    try:
        return c.decrypt(enc.encode()).decode()
    except Exception:
        return None


def _client_ip(request: Request) -> str:
    # Behind a reverse proxy the real IP is in X-Forwarded-For; fall back to
    # the socket peer. (Spoofable if not behind a trusted proxy, but the
    # throttle is best-effort hardening, not an access control.)
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _throttle_check(ip: str):
    import time
    now = time.time()
    hits = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_S]
    _login_attempts[ip] = hits
    if len(hits) >= _LOGIN_MAX_ATTEMPTS:
        retry = int(_LOGIN_WINDOW_S - (now - hits[0]))
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in {max(retry, 1)} seconds.",
        )


def _throttle_record_failure(ip: str):
    import time
    _login_attempts.setdefault(ip, []).append(time.time())


@app.post("/api/login")
def login(body: LoginRequest, request: Request):
    """Verify credentials against the controller (client.api holds the
    API key) and, on success, store the username in the signed session
    cookie. Mirrors the desktop admin login exactly."""
    ip = _client_ip(request)
    _throttle_check(ip)
    try:
        result = api.admin_login(body.username.strip(), body.password)
    except Exception:
        # client.api raises on a 401 from the controller.
        _throttle_record_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    _login_attempts.pop(ip, None)  # clear on success
    # Drop any pre-login session identifier so a fixed/known cookie can't be
    # carried into the authenticated session (session-fixation hardening).
    request.session.clear()
    request.session["user"] = body.username.strip()
    request.session["role"] = result.get("role") or "superuser"
    # Keep the controller-issued admin token (encrypted) so the BFF can call
    # superuser-gated controller routes on this admin's behalf. Encrypted with
    # a server-side key, so it's unreadable from the cookie and never echoed.
    if result.get("token"):
        request.session["token_enc"] = _encrypt_token(result["token"])
    return {
        "username": body.username.strip(),
        "role": request.session["role"],
        "must_change_password": bool(result.get("must_change_password")),
    }


@app.get("/api/health")
def health():
    """Unauthenticated liveness probe used by the controller's
    webgui_manager to confirm the service actually came up."""
    return {"status": "ok"}


# ----------------------------------------------------------------------
# Sudo (become) password — encrypted at rest on the controller, per admin.
# ----------------------------------------------------------------------
from webgui import sudo_store  # noqa: E402


class SudoRequest(BaseModel):
    password: str = ""
    scope: str = sudo_store.ALL   # "__all__" (fleet default) or a host label


@app.get("/api/sudo")
def sudo_status(user: str = Depends(require_login)):
    """Which scopes the current admin has a stored sudo password for, so the
    dialog can show 'a password is stored for this scope'. Never returns the
    password itself."""
    return {
        "encryption_available": sudo_store.encryption_available(),
        "scopes": sudo_store.scopes_set(user),
        "all_scope": sudo_store.ALL,
    }


@app.post("/api/sudo")
def sudo_set(body: SudoRequest, user: str = Depends(require_login)):
    if not body.password:
        raise HTTPException(status_code=400, detail="Password is required.")
    ok = sudo_store.set_password(user, body.scope or sudo_store.ALL, body.password)
    if not ok:
        raise HTTPException(
            status_code=503,
            detail="Encryption isn't available on the controller, so the password "
                   "was not stored (it is never written in clear text).",
        )
    return {"stored": True, "scope": body.scope or sudo_store.ALL}


@app.delete("/api/sudo")
def sudo_clear(body: SudoRequest, user: str = Depends(require_login)):
    sudo_store.clear(user, body.scope or sudo_store.ALL)
    return {"cleared": True, "scope": body.scope or sudo_store.ALL}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"status": "ok"}


@app.get("/api/me")
def me(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user, "role": request.session.get("role") or "superuser"}


# ----------------------------------------------------------------------
# Read-only data the dashboard + host picker need
# ----------------------------------------------------------------------
@app.get("/api/edition")
def edition(user: str = Depends(require_login)):
    try:
        return api.get_edition() or {}
    except Exception:
        return {}


@app.get("/api/hosts")
def hosts(user: str = Depends(require_login)):
    """All enrolled hosts (agent + SSH, duplicates merged) in the shape
    the desktop host picker uses. agent_only=False so SSH-only hosts
    show too; the UI marks which tools require an agent."""
    try:
        entries = dispatch.list_merged_hosts(agent_only=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    # Hand back only what the browser needs to display + later target.
    out = []
    for e in entries:
        out.append({
            "id": e["id"],
            "label": e["label"],
            "kind": e["kind"],
            "type_text": e.get("type_text", ""),
            "address": e.get("address", ""),
            "environment": e.get("environment", ""),
            "has_agent": e["kind"] in ("agent", "merged"),
        })
    return {"hosts": out}


@app.get("/api/environments")
def environments(user: str = Depends(require_login)):
    try:
        return {"environments": api.list_environments()}
    except Exception:
        return {"environments": []}


@app.post("/api/environments")
def create_environment(body: dict, request: Request, user: str = Depends(require_login)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Environment name is required.")
    return _wrap(lambda: _as_admin(request, lambda: api.create_environment(name)))


@app.delete("/api/environments/{name}")
def delete_environment(name: str, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.delete_environment(name)))


# ----------------------------------------------------------------------
# Sysible Connect — fleet actions, check-in, SSH enrollment, host admin
# ----------------------------------------------------------------------
def _superuser_request(method, path, request: Request, **kw):
    """Call a superuser-gated controller route on behalf of the logged-in
    admin, using the token captured at login. client.api holds the API key;
    we add the admin token header the controller's require_superuser wants."""
    import requests
    token = _session_token(request)
    if not token:
        raise HTTPException(status_code=403, detail="This action requires a superuser login.")
    headers = dict(api._headers())
    headers["X-Sysible-Admin-Token"] = token
    try:
        r = api._SESSION.request(method, f"{api.BASE_URL}{path}", headers=headers,
                                 timeout=kw.pop("timeout", 30), verify=api._VERIFY, **kw)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    if not r.ok:
        detail = None
        try:
            detail = r.json().get("detail")
        except Exception:
            pass
        raise HTTPException(status_code=r.status_code, detail=detail or r.reason)
    return r.json() if r.content else {}


_FLEET_COMMANDS = {
    "reboot": (dispatch.cmd_reboot_host, "REBOOT"),
    "poweroff": (dispatch.cmd_poweroff_host, "power off"),
    "restart_agent": (dispatch.cmd_restart_agent, "restart the agent on"),
}


class FleetRequest(BaseModel):
    action: str                 # reboot | poweroff | restart_agent | script
    command: str = ""           # used when action == "script"
    targets: list[str] = []     # host ids; empty = all hosts


@app.post("/api/fleet")
def fleet(body: FleetRequest, user: str = Depends(require_login)):
    """Run a fleet action across the selected hosts (or all hosts)."""
    if body.action == "script":
        command = body.command.strip()
        if not command:
            raise HTTPException(status_code=400, detail="A script/command is required.")
    else:
        spec = _FLEET_COMMANDS.get(body.action)
        if not spec:
            raise HTTPException(status_code=400, detail=f"Unknown fleet action: {body.action}")
        command = spec[0]()

    try:
        all_entries = {e["id"]: e for e in dispatch.list_merged_hosts(agent_only=False)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")

    targets = body.targets or list(all_entries.keys())
    results = []
    for tid in targets:
        entry = all_entries.get(tid)
        if entry is None:
            results.append({"host": tid, "ok": False, "error": "host not found",
                            "stdout": "", "stderr": "", "code": None})
            continue
        become = sudo_store.resolve(user, entry.get("label", ""))
        results.append(_dispatch_one(entry, command, "command", become))
    return {"action": body.action, "command": command, "results": results}


@app.post("/api/checkin")
def checkin(user: str = Depends(require_login)):
    """Lightweight reachability probe: run `true` on every host and report
    which answered (mirrors the desktop 'Check In / Ping')."""
    try:
        entries = dispatch.list_merged_hosts(agent_only=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    out = []
    for entry in entries:
        r = _dispatch_one(entry, "true", "command", None)
        out.append({"host": entry["label"], "id": entry["id"],
                    "reachable": bool(r.get("ok")) or r.get("code") == 0,
                    "detail": r.get("error") or ""})
    return {"results": out}


@app.get("/api/controller-key")
def controller_key(user: str = Depends(require_login)):
    try:
        return api._request("GET", "/remote/controller-key")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class EnrollRequest(BaseModel):
    name: str
    ip: str
    username: str
    password: str
    environment: str = ""


@app.post("/api/enroll-ssh")
def enroll_ssh(body: EnrollRequest, request: Request, user: str = Depends(require_login)):
    payload = {"name": body.name, "ip": body.ip, "username": body.username,
               "password": body.password}
    if body.environment:
        payload["environment"] = body.environment
    return _superuser_request("POST", "/remote/enroll-ssh", request, json=payload)


class HostEnvRequest(BaseModel):
    environment: str = ""


@app.post("/api/host/{host_id}/environment")
def set_host_environment(host_id: str, body: HostEnvRequest, request: Request,
                         user: str = Depends(require_login)):
    # Superuser-gated on the controller — pass the admin token via _as_admin.
    return _wrap(lambda: _as_admin(request, lambda: api.set_agent_environment(host_id, body.environment)))


@app.delete("/api/host/{host_id}")
def remove_host(host_id: str, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.disenroll_agent(host_id) or {"removed": True}))


class SudoRequiredRequest(BaseModel):
    required: bool


@app.post("/api/host/{host_id}/sudo")
def set_host_sudo(host_id: str, body: SudoRequiredRequest, request: Request,
                  user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_sudo_password_required(host_id, body.required)))


@app.get("/api/environment-sudo-defaults")
def env_sudo_defaults(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_environment_sudo_defaults())


class EnvSudoDefaultRequest(BaseModel):
    name: str
    required: bool


@app.post("/api/environment-sudo-default")
def set_env_sudo_default(body: EnvSudoDefaultRequest, request: Request,
                         user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_environment_sudo_default(body.name, body.required)))


# ----------------------------------------------------------------------
# Superuser-token helper: set the admin token (captured at login) on the
# shared client.api for the duration of one controller call. Serialized so
# concurrent requests can't clobber the process-global token.
# ----------------------------------------------------------------------
import threading as _threading  # noqa: E402
_ADMIN_TOKEN_LOCK = _threading.Lock()


def _as_admin(request: Request, fn):
    token = _session_token(request)
    with _ADMIN_TOKEN_LOCK:
        api.set_admin_token(token)
        try:
            return fn()
        finally:
            api.set_admin_token(None)


def _wrap(fn):
    """Run a controller call, turning any non-HTTP error into a 502 so a
    controller hiccup surfaces cleanly instead of as an opaque 500."""
    try:
        return fn()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ----------------------------------------------------------------------
# Live Activity & Logs
# ----------------------------------------------------------------------
@app.get("/api/activity")
def activity(limit: int = 200, since_id: int = 0, request: Request = None, user: str = Depends(require_login)):
    # /activity-log is superuser-gated on the controller — pass the admin token.
    return _wrap(lambda: _as_admin(request, lambda: {"activity": api.get_activity_log(limit=limit, since_id=since_id)}))


@app.get("/api/controller-log")
def controller_log(lines: int = 400, request: Request = None, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.get_controller_log(lines=lines)))


# ----------------------------------------------------------------------
# Settings — administrators, password policy, controller config, license, audit
# ----------------------------------------------------------------------
@app.get("/api/admins")
def list_admins(user: str = Depends(require_login)):
    return _wrap(lambda: {"administrators": api.list_administrators()})


class AdminCreate(BaseModel):
    username: str
    password: str
    role: str = "sysadmin"


@app.post("/api/admins")
def add_admin(body: AdminCreate, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.add_administrator(
        body.username, body.password, actor=user, role=body.role)))


@app.delete("/api/admins/{username}")
def del_admin(username: str, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.remove_administrator(username, actor=user)))


@app.get("/api/password-policy")
def get_policy(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_admin_password_policy())


@app.post("/api/password-policy")
def set_policy(body: dict, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_admin_password_policy(body)))


@app.get("/api/controller-config")
def get_cfg(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_controller_config())


class ControllerCfg(BaseModel):
    hostname: str = ""
    ip: str = ""
    address_mode: str = "hostname"
    port: int = 9000


@app.post("/api/controller-config")
def set_cfg(body: ControllerCfg, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_controller_config(
        body.hostname, body.ip, body.address_mode, body.port)))


@app.get("/api/audit-log")
def audit_log(limit: int = 200, user: str = Depends(require_login)):
    return _wrap(lambda: {"audit": api.get_admin_audit_log(limit=limit)})


@app.get("/api/license")
def license_config(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_license_config())


@app.get("/api/environmental-policy")
def get_env_policy(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_environmental_policy())


@app.post("/api/environmental-policy")
def set_env_policy(body: dict, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_environmental_policy(body)))


# ----------------------------------------------------------------------
# User & Group Administration — live per-host user/group/session data
# ----------------------------------------------------------------------
class UserSyncRequest(BaseModel):
    host_id: str


@app.post("/api/users/sync")
def users_sync(body: UserSyncRequest, user: str = Depends(require_login)):
    """Pull the live user/group/session inventory from one host (mirrors the
    desktop 'Sync Checked Hosts'). SSH hosts answer synchronously; agent hosts
    are polled here to a timeout so the browser gets one response."""
    try:
        entries = {e["id"]: e for e in dispatch.list_merged_hosts(agent_only=False)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    entry = entries.get(body.host_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="host not found")

    res = dispatch.sync_entry_users(entry)
    if res.get("error"):
        raise HTTPException(status_code=502, detail=res["error"])
    if res.get("sync"):
        return {"host": entry["label"], "data": res.get("data") or {}}

    import time as _t
    task_id = res.get("task_id")
    deadline = _t.time() + float(os.getenv("SYSIBLE_WEBGUI_TASK_TIMEOUT", "60"))
    while _t.time() < deadline:
        polled = dispatch.poll_entry_sync_result(entry, task_id)
        if polled is not None:
            if polled.get("error"):
                raise HTTPException(status_code=502, detail=polled["error"])
            return {"host": entry["label"], "data": polled.get("data") or {}}
        _t.sleep(1.0)
    raise HTTPException(status_code=504, detail="timed out waiting for the agent to report users")


# ----------------------------------------------------------------------
# Webserver Portal
# ----------------------------------------------------------------------
@app.get("/api/portal/status")
def portal_status(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_portal_status())


@app.post("/api/portal/start")
def portal_start(request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.start_portal()))


@app.post("/api/portal/stop")
def portal_stop(request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.stop_portal()))


class PortalPort(BaseModel):
    port: int


@app.post("/api/portal/config")
def portal_cfg(body: PortalPort, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_portal_port(body.port)))


class PortalCreds(BaseModel):
    username: str
    password: str
    current_password: str = ""


@app.post("/api/portal/credentials")
def portal_creds(body: PortalCreds, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_portal_credentials(
        body.username, body.password, body.current_password)))


class PortalRemoveCreds(BaseModel):
    current_password: str = ""


@app.delete("/api/portal/credentials")
def portal_remove_creds(body: PortalRemoveCreds, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.remove_portal_credentials(body.current_password)))


@app.get("/api/portal/login-history")
def portal_login_history(limit: int = 200, user: str = Depends(require_login)):
    return _wrap(lambda: {"history": api.get_portal_login_history(limit)})


@app.get("/api/portal/sessions")
def portal_sessions(user: str = Depends(require_login)):
    return _wrap(lambda: {"sessions": api.get_portal_sessions()})


@app.post("/api/portal/sessions/{session_id}/revoke")
def portal_revoke_session(session_id: int, request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.revoke_portal_session(session_id)))


# ----------------------------------------------------------------------
# Host Enrollment
# ----------------------------------------------------------------------
@app.get("/api/agents")
def agents(user: str = Depends(require_login)):
    return _wrap(lambda: {"agents": api.get_agents()})


@app.post("/api/enroll-token")
def enroll_token(request: Request, user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.generate_enroll_token()))


@app.get("/api/agent-bundle")
def agent_bundle(user: str = Depends(require_login)):
    """Download the ready-to-run agent bundle (tar.gz) the desktop's Host
    Enrollment page hands out."""
    tmpdir = Path(tempfile.mkdtemp(prefix="sysible-bundle-"))
    dest = tmpdir / "sysible-agent.tar.gz"
    try:
        api.download_agent_bundle(str(dest))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not build bundle: {e}")

    def _cleanup():
        try:
            dest.unlink(missing_ok=True); tmpdir.rmdir()
        except Exception:
            pass
    return FileResponse(str(dest), filename="sysible-agent.tar.gz",
                        media_type="application/gzip", background=BackgroundTask(_cleanup))


# ----------------------------------------------------------------------
# Tool catalog + the generic dispatch engine
# ----------------------------------------------------------------------
@app.get("/api/tools")
def tools_catalog(user: str = Depends(require_login)):
    """The machine-readable action catalog the SPA renders tool forms
    from: every registered action with its params. This is the single
    source of truth shared by both ends - add an action in actions.py
    and it appears in the web UI automatically."""
    return {"tools": actions.catalog()}


class RunRequest(BaseModel):
    targets: list[str]          # host ids/labels as returned by /api/hosts
    params: dict = {}


@app.post("/api/tool/{action_name}")
def run_tool(action_name: str, body: RunRequest, user: str = Depends(require_login)):
    """Build the shell command for `action_name` via the desktop client's
    cmd_* builder, then dispatch it across every selected target and
    return per-host results. Synchronous: agent tasks are polled here up
    to a timeout so the browser gets one clean response."""
    spec = actions.get(action_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown action: {action_name}")

    try:
        command = spec.build(body.params)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing parameter: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not build command: {e}")

    # Resolve target ids -> merged-host entries (so dispatch knows agent
    # vs. SSH for each).
    try:
        all_entries = {e["id"]: e for e in dispatch.list_merged_hosts(agent_only=False)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")

    results = []
    for target in body.targets:
        entry = all_entries.get(target)
        if entry is None:
            results.append({"host": target, "ok": False, "error": "host not found",
                            "stdout": "", "stderr": "", "code": None})
            continue
        # Resolve this admin's sudo password for the target (host scope wins
        # over fleet default); only used if the host is flagged sudo-required.
        become = sudo_store.resolve(user, entry.get("label", ""))
        results.append(_dispatch_one(entry, command, spec.kind, become))

    return {"action": action_name, "command": command, "results": results}


def _dispatch_one(entry, command, kind, become_password=None):
    """Run one command on one host and return a normalized result,
    polling agent tasks to completion (bounded) so the response is
    synchronous from the browser's point of view."""
    label = entry["label"]
    try:
        outcome = dispatch.run_on_entry(entry, command, kind=kind,
                                        become_password=become_password)
    except Exception as e:
        return {"host": label, "ok": False, "error": str(e),
                "stdout": "", "stderr": "", "code": None}

    if outcome.get("error"):
        return {"host": label, "ok": False, "error": outcome["error"],
                "stdout": "", "stderr": "", "code": None}

    if outcome.get("sync"):
        return _normalize(label, outcome)

    # Agent task: poll until the agent reports back, or time out.
    import time
    task_id = outcome.get("task_id")
    deadline = time.time() + float(os.getenv("SYSIBLE_WEBGUI_TASK_TIMEOUT", "60"))
    while time.time() < deadline:
        polled = dispatch.poll_entry_result(entry, task_id)
        if polled is not None:
            return _normalize(label, polled)
        time.sleep(1.0)
    return {"host": label, "ok": False, "error": "timed out waiting for agent",
            "stdout": "", "stderr": "", "code": None}


def _normalize(label, r):
    code = r.get("code")
    return {
        "host": label,
        "ok": (code == 0) if code is not None else (not r.get("stderr")),
        "error": r.get("error"),
        "stdout": r.get("stdout", ""),
        "stderr": r.get("stderr", ""),
        "code": code,
    }


# ----------------------------------------------------------------------
# File transfer (Sysible Connect) - browser <-> host over SSH
# ----------------------------------------------------------------------
# Reuses the desktop client's SSH transfer helpers. Upload: the browser's
# multipart file is spooled to a temp file, pushed with upload_file_ssh,
# then the temp file is removed. Download: download_file_ssh writes to a
# temp file which is streamed back as an attachment and cleaned up after.
@app.post("/api/files/upload")
async def files_upload(
    host: str = Form(...),
    remote_path: str = Form(...),
    file: UploadFile = File(...),
    user: str = Depends(require_login),
):
    tmp = Path(tempfile.mkdtemp(prefix="sysible-up-")) / (file.filename or "upload.bin")
    try:
        data = await file.read()
        tmp.write_bytes(data)
        try:
            result = await asyncio.to_thread(api.upload_file_ssh, host, str(tmp), remote_path)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Upload failed: {e}")
        return {"host": host, "remote_path": remote_path,
                "filename": file.filename, "bytes": len(data), "result": result}
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except Exception:
            pass


@app.get("/api/files/download")
async def files_download(host: str, path: str, user: str = Depends(require_login)):
    tmpdir = Path(tempfile.mkdtemp(prefix="sysible-dn-"))
    filename = os.path.basename(path.rstrip("/")) or "download.bin"
    dest = tmpdir / filename
    try:
        await asyncio.to_thread(api.download_file_ssh, host, path, str(dest))
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"Download failed: {e}")

    def _cleanup():
        try:
            dest.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass

    return FileResponse(str(dest), filename=filename,
                        media_type="application/octet-stream",
                        background=BackgroundTask(_cleanup))


# ----------------------------------------------------------------------
# Sysible Connect - browser terminal over a websocket
# ----------------------------------------------------------------------
# The controller exposes an SSH PTY as a poll-based HTTP API (open ->
# read/write/resize -> close) via client.api. xterm.js in the browser
# wants a stream, so this websocket bridges the two: a background task
# polls read_terminal() and pushes output frames to the browser, while
# inbound frames are written/resized/closed straight through. All
# client.api calls are blocking (requests), so they run in threads to
# keep the event loop free.
#
# Wire protocol (JSON text frames):
#   server -> browser: {"t":"ready"} | {"t":"o","d":<output>} | {"t":"closed"} | {"t":"error","d":msg}
#   browser -> server: {"t":"open","host":<id>,"cols":N,"rows":N}
#                      {"t":"i","d":<keystrokes>} | {"t":"r","cols":N,"rows":N}
@app.websocket("/api/terminal/ws")
async def terminal_ws(ws: WebSocket):
    # Auth: SessionMiddleware populates the websocket scope's session the
    # same way it does for HTTP, so the login cookie gates this too.
    if not ws.scope.get("session", {}).get("user"):
        await ws.close(code=1008)
        return
    await ws.accept()

    session_id = None
    reader = None
    try:
        # First frame must open a terminal on a chosen host.
        first = await ws.receive_json()
        if first.get("t") != "open" or not first.get("host"):
            await ws.send_json({"t": "error", "d": "expected open frame with host"})
            await ws.close()
            return
        host = first["host"]
        try:
            opened = await asyncio.to_thread(api.open_terminal, host)
            session_id = opened["session_id"]
        except Exception as e:
            await ws.send_json({"t": "error", "d": f"could not open terminal: {e}"})
            await ws.close()
            return

        # Initial size, if provided.
        if first.get("cols") and first.get("rows"):
            try:
                await asyncio.to_thread(api.resize_terminal, session_id,
                                        int(first["cols"]), int(first["rows"]))
            except Exception:
                pass
        await ws.send_json({"t": "ready"})

        async def pump_output():
            """Poll the controller for new PTY output and push it to the
            browser. Light idle backoff keeps latency low while typing
            without busy-spinning an idle shell."""
            idle = 0
            while True:
                try:
                    res = await asyncio.to_thread(api.read_terminal, session_id)
                except Exception as e:
                    await ws.send_json({"t": "error", "d": str(e)})
                    return
                data = res.get("data", "")
                if data:
                    await ws.send_json({"t": "o", "d": data})
                    idle = 0
                else:
                    idle = min(idle + 1, 6)
                if res.get("closed"):
                    await ws.send_json({"t": "closed"})
                    return
                await asyncio.sleep(0.03 + idle * 0.02)   # 30ms..150ms

        async def pump_input():
            """Keystrokes / resize from the browser -> host."""
            while True:
                msg = await ws.receive_json()
                t = msg.get("t")
                if t == "i":
                    await asyncio.to_thread(api.write_terminal, session_id, msg.get("d", ""))
                elif t == "r" and msg.get("cols") and msg.get("rows"):
                    await asyncio.to_thread(api.resize_terminal, session_id,
                                            int(msg["cols"]), int(msg["rows"]))

        reader = asyncio.create_task(pump_output())
        writer = asyncio.create_task(pump_input())
        # Whichever finishes first (shell exits / output side closes, or the
        # browser disconnects) ends the session; cancel the other.
        done, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if reader is not None:
            reader.cancel()
        if session_id is not None:
            try:
                await asyncio.to_thread(api.close_terminal, session_id)
            except Exception:
                pass


# ----------------------------------------------------------------------
# Serve the built SPA (optional - can also be served by a reverse proxy)
# ----------------------------------------------------------------------
if _FRONTEND_DIST.exists():
    # Mount hashed assets, then fall through to index.html for client-side
    # routing on every non-/api path.
    app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        index = _FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"detail": "frontend not built"}, status_code=404)
else:
    @app.get("/")
    def root_placeholder():
        return {
            "service": "Sysible Web GUI",
            "status": "frontend not built",
            "hint": "cd webgui/frontend && npm install && npm run build",
        }
