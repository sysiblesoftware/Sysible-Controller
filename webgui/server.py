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


# docs_url/redoc_url/openapi_url disabled: this console is built to be
# network-reachable, so we don't publish the interactive Swagger/ReDoc consoles
# or the OpenAPI schema (a full map of the API surface) to anyone who can reach
# the port — matching the controller backend and the portal.
app = FastAPI(title="Sysible Web GUI", docs_url=None, redoc_url=None, openapi_url=None)

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
# Only trust X-Forwarded-For (for the login throttle's client-IP) when a trusted
# reverse proxy is actually in front; otherwise the header is client-spoofable.
_TRUST_PROXY = os.getenv("SYSIBLE_WEBGUI_TRUSTED_PROXY", "0") == "1"
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


def require_operator(request: Request):
    """Dependency for any write/dispatch route: 401 if not logged in, 403 for
    the read-only 'auditor' role. Superuser-only routes don't need this (they
    proxy through the controller's require_superuser, which already rejects an
    auditor); this guards the routes that superusers AND sysadmins may use but
    auditors may not (running tools, fleet actions, terminals, etc.). The
    controller also blocks command dispatch for auditors server-side, so this
    is the front-of-house half of a defence-in-depth pair."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if request.session.get("role") == "auditor":
        raise HTTPException(status_code=403, detail="Auditor accounts are read-only.")
    return user


def require_superuser_session(request: Request):
    """Dependency for superuser-only surfaces whose controller routes are NOT
    themselves require_superuser (the webserver portal and TLS-cert install are
    api-key-only on the controller). Those screens are superuser-only in the UI,
    but without this a sysadmin could still drive them via the API. A sysadmin
    reaches the controller ONLY through this BFF (the admin API key is root-only
    on the controller host), so enforcing the superuser/sysadmin split here is
    the effective control; the high-value writes also get controller-side
    require_superuser as defence-in-depth."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if request.session.get("role") != "superuser":
        raise HTTPException(status_code=403, detail="This action requires a superuser account.")
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


def _token_from_session(session):
    enc = (session or {}).get("token_enc")
    if not enc:
        return None
    c = _token_cipher()
    if not c:
        return None
    try:
        return c.decrypt(enc.encode()).decode()
    except Exception:
        return None


def _session_token(request: "Request"):
    return _token_from_session(getattr(request, "session", None))


# Run `fn` with the admin token set on client.api for its duration. Serialized
# so concurrent requests can't clobber the process-global token. Used both for
# superuser-gated routes AND for dispatch/terminal calls, so the controller can
# derive the run-as user (runuser -u <admin>) and attribute the activity feed.
def _with_token(token, fn):
    # Thread-scoped: the BFF serves many admins concurrently, and fleet-health
    # runs its probes in parallel threads. Setting the token per-thread (rather
    # than on the process-global) means a concurrent request can't leak its
    # token into another thread's call. token=None explicitly forces NO token
    # for this thread, so the read-only metrics probe truly runs as root and
    # never depends on the viewer having a host account (e.g. an auditor).
    api.set_admin_token_override(token)
    try:
        return fn()
    finally:
        api.clear_admin_token_override()


def _client_ip(request: Request) -> str:
    # Only honor X-Forwarded-For when we're explicitly told a trusted proxy sits
    # in front (SYSIBLE_WEBGUI_TRUSTED_PROXY=1) — otherwise a direct client could
    # spoof the header to evade the per-IP login throttle. Default to the real
    # socket peer.
    if _TRUST_PROXY:
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
    import requests
    ip = _client_ip(request)
    _throttle_check(ip)
    try:
        result = api.admin_login(body.username.strip(), body.password)
    except requests.exceptions.HTTPError as e:
        # The controller responded with an HTTP error. A 401 is genuinely bad
        # credentials; anything else (e.g. 429 throttle) is passed through so
        # it isn't mistaken for a wrong password.
        resp = getattr(e, "response", None)
        code = resp.status_code if resp is not None else 401
        if code == 401:
            _throttle_record_failure(ip)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        detail = None
        try:
            detail = resp.json().get("detail")
        except Exception:
            pass
        raise HTTPException(status_code=code, detail=detail or "Controller rejected the login.")
    except Exception as e:
        # Could NOT reach the controller (down, wrong base URL, unreadable API
        # key, or TLS verification failed) — this is NOT a wrong password, so
        # don't throttle and don't claim "invalid credentials". Surface the
        # real cause so it's diagnosable.
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the controller to verify the login: {e}. "
                   f"Check the controller is running and the web console can read "
                   f"its API key and TLS cert (see 'sysible_controller webgui logs').",
        )

    _login_attempts.pop(ip, None)  # clear on success
    # Drop any pre-login session identifier so a fixed/known cookie can't be
    # carried into the authenticated session (session-fixation hardening).
    request.session.clear()
    request.session["user"] = body.username.strip()
    request.session["role"] = result.get("role") or "superuser"
    # Per-admin opt-in for the Sysible Connect terminal's "Send sudo password"
    # button (granted by a superuser). Enforced server-side in the terminal ws
    # sudo handler below.
    request.session["sudo_connect"] = bool(result.get("sudo_connect"))
    # Keep the controller-issued admin token (encrypted) so the BFF can call
    # superuser-gated controller routes on this admin's behalf. Encrypted with
    # a server-side key, so it's unreadable from the cookie and never echoed.
    if result.get("token"):
        request.session["token_enc"] = _encrypt_token(result["token"])
    return {
        "username": body.username.strip(),
        "role": request.session["role"],
        "sudo_connect": request.session["sudo_connect"],
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
def sudo_set(body: SudoRequest, user: str = Depends(require_operator)):
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
def sudo_clear(body: SudoRequest, user: str = Depends(require_operator)):
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
    return {
        "username": user,
        "role": request.session.get("role") or "superuser",
        "sudo_connect": bool(request.session.get("sudo_connect")),
    }


class PathCriticalRequest(BaseModel):
    paths: list[str] = []


@app.post("/api/path-critical")
def path_critical(body: PathCriticalRequest, user: str = Depends(require_login)):
    """Classify whether any of `paths` is a system-critical file/mount, so the
    UI can warn (superuser) or block (sysadmin) before a delete/unmount/fstab
    removal. Single source of truth = client/system_paths (shared with the
    desktop GUI and the cmd_* builder backstop)."""
    from client import system_paths
    for p in body.paths:
        reason = system_paths.system_critical_reason(p)
        if reason:
            return {"critical": True, "path": system_paths.normalize(p), "reason": reason}
    return {"critical": False, "path": "", "reason": ""}


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

    # Agent heartbeat ages -> passive online/offline (no traffic to the host).
    # An agent heartbeats every ~1.5s; >20s stale = offline (matches desktop's
    # CHECKIN_ONLINE_SECONDS). SSH-only hosts can't be judged without an active
    # probe, so their `online` is null (use Check In / Ping for those).
    import time as _t
    now = _t.time()
    try:
        last_seen = {a.get("host_id"): a.get("last_seen") for a in api.get_agents()}
    except Exception:
        last_seen = {}

    out = []
    for e in entries:
        agent_id = None
        if e["kind"] == "agent":
            agent_id = e["id"]
        elif e["kind"] == "merged":
            agent_id = (e.get("agent_entry") or {}).get("id") or e["id"]
        ls = last_seen.get(agent_id) if agent_id else None
        online = None
        if agent_id is not None:
            online = bool(ls and (now - ls) <= 20)
        out.append({
            "id": e["id"],
            "label": e["label"],
            "kind": e["kind"],
            "type_text": e.get("type_text", ""),
            "address": e.get("address", ""),
            "environment": e.get("environment", ""),
            "has_agent": e["kind"] in ("agent", "merged"),
            "last_seen": ls,
            "online": online,
        })
    return {"hosts": out}


def _parse_sysmetrics(text):
    """Parse the one-line `SYSMETRICS|k=v|...` snapshot (client._api_dispatch
    .cmd_metrics_snapshot) into a dict, or None if not present."""
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


@app.get("/api/fleet-health")
def fleet_health(user: str = Depends(require_login)):
    """Snapshot health for the dashboard fleet overview: run the compact
    metrics command on every reachable host (in parallel) and return parsed
    per-host disk/mem/load/failed + a verdict. Offline agent hosts are reported
    without probing (so the sweep can't hang on them); read-only, dispatched
    without an admin token so it isn't attributed/logged as an operator action."""
    import concurrent.futures
    import time as _t
    try:
        entries = dispatch.list_merged_hosts(agent_only=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")

    try:
        last_seen = {a.get("host_id"): a.get("last_seen") for a in api.get_agents()}
    except Exception:
        last_seen = {}
    now = _t.time()
    cmd = api.cmd_metrics_snapshot()

    def agent_id_of(e):
        if e["kind"] == "agent":
            return e["id"]
        if e["kind"] == "merged":
            return (e.get("agent_entry") or {}).get("id") or e["id"]
        return None

    def probe(e):
        base = {"id": e.get("id"), "host": e.get("label"), "environment": e.get("environment") or "Unassigned"}
        aid = agent_id_of(e)
        ls = last_seen.get(aid) if aid else None
        online = (bool(ls and (now - ls) <= 20)) if aid else None
        # Don't probe an agent host we already know is offline — avoids waiting
        # out the task timeout on a host that won't answer.
        if aid is not None and not online:
            return {**base, "online": False, "ok": False, "verdict": "OFFLINE", "error": "offline"}
        # The metrics command is read-only and runs as the agent (root) / SSH
        # user with no sudo, so it must NOT be gated on a stored become-password.
        # Clear the password-sudo flag on a copy of the entry so run_on_entry
        # doesn't fail-fast on password-sudo hosts (the controller, where this
        # runs, has no operator become-password to supply).
        pe = {**e, "requires_sudo_password": False}
        if e.get("agent_entry"):
            pe["agent_entry"] = {**e["agent_entry"], "requires_sudo_password": False}
        r = _dispatch_one(pe, cmd, "command", None, None, None)  # no token: read-only, unlogged
        m = _parse_sysmetrics((r.get("stdout") or "") + "\n" + (r.get("stderr") or ""))
        return {
            **base,
            "online": True if m else online,
            "ok": bool(r.get("ok")) and m is not None,
            "error": None if m else (r.get("error") or "no metrics returned"),
            **(m or {}),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, max(1, len(entries)))) as ex:
        hosts = list(ex.map(probe, entries)) if entries else []
    return {"hosts": hosts}


@app.get("/api/fleet-metrics")
def fleet_metrics(window: int = 3600, user: str = Depends(require_login)):
    """Per-host performance time-series for the Performance view: load/mem/disk
    history reported by the agents on heartbeat, grouped by host with the
    environment label attached. Read-only; just proxies the controller."""
    try:
        return api.get_metrics_timeseries(window)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")


# ----------------------------------------------------------------------
# Host posture / compliance (Phase 1 — read-only, on-demand sweep)
# ----------------------------------------------------------------------
#
# Mirrors the fleet-health sweep: run the read-only posture command on hosts
# (tokenless, as root), parse the POSTURE|cat.key=value stream, and surface a
# per-host posture object plus a curated set of high-ticket flags the dashboard
# aggregates into a Compliance strip. Read-only throughout (require_login), so
# the auditor role can view it; nothing here dispatches a mutating command.

# Best-effort end-of-life table for the dashboard's "EOL / unsupported OS"
# signal. Keyed by os-release ID then VERSION_ID; value is the end-of-support
# date (ISO). A host past this date (or a version not in a known-supported set)
# flags as EOL. Intentionally small and conservative — unknowns never flag.
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


def _parse_posture(text):
    """Parse the `POSTURE|<cat>.<key>=<value>` stream from cmd_posture_snapshot
    into a nested {category: {key: value}} dict, or None if no lines present.
    Values are kept as strings (the gather command already single-lines them)."""
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


def _eol_status(distro, version):
    """Return True (EOL), False (supported), or None (unknown) for a distro."""
    if not distro or not version:
        return None
    table = _EOL_TABLE.get((distro or "").lower())
    if not table:
        return None
    eol = table.get(version) or table.get(version.split(".")[0])
    if not eol:
        return None  # unknown point release -> don't flag
    import datetime
    try:
        return datetime.date.today().isoformat() > eol
    except Exception:
        return None


def _posture_flags(p):
    """Compute the curated high-ticket compliance signals from a parsed posture
    dict. Each value is True (a problem), False (clean), or None (couldn't
    determine). The dashboard counts the True ones per signal across the fleet."""
    if not p:
        return {}
    g = lambda c, k, d=None: (p.get(c) or {}).get(k, d)

    def as_int(v):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None

    # SSH root login: "yes" is the risk; key-only forms are acceptable.
    rl = (g("ssh", "permit_root_login") or "").strip().lower()
    ssh_root = (rl == "yes") if rl else None

    # MAC: flag only when NEITHER SELinux nor AppArmor is actively enforcing.
    se = (g("mac", "selinux") or "").strip().lower()
    aa = (g("mac", "apparmor") or "").strip().lower()
    se_enf = se == "enforcing"
    aa_enf = aa == "enabled"
    mac_off = (not se_enf and not aa_enf) if (se or aa) else None

    fw = g("fw", "active")
    sync = (g("time", "synced") or "").strip().lower()
    uid0 = as_int(g("users", "uid0_count"))
    emptypw = as_int(g("users", "empty_pw_count"))
    cert30 = as_int(g("cert", "expiring_30d"))
    eol = _eol_status(g("os", "distro"), g("os", "version"))

    return {
        "reboot_required": (g("reboot", "required") == "1") if g("reboot", "required") is not None else None,
        "ssh_root_login": ssh_root,
        "firewall_disabled": (fw == "0") if fw is not None else None,
        "mac_not_enforcing": mac_off,
        "eol_os": eol,
        "risky_accounts": ((uid0 or 0) > 1 or (emptypw or 0) > 0)
        if (uid0 is not None or emptypw is not None) else None,
        "cert_expiring": (cert30 > 0) if cert30 is not None else None,
        "time_unsynced": (sync in ("no", "false", "0")) if sync else None,
    }


def _posture_command():
    return api.cmd_posture_snapshot()


def _probe_posture(e, cmd, last_seen, now):
    """Run the read-only posture command on one host entry and return its parsed
    posture + flags. Mirrors fleet_health.probe: offline agents are reported
    without probing; dispatch is tokenless (read-only, root, unattributed)."""
    base = {"id": e.get("id"), "host": e.get("label"),
            "environment": e.get("environment") or "Unassigned"}
    aid = None
    if e["kind"] == "agent":
        aid = e["id"]
    elif e["kind"] == "merged":
        aid = (e.get("agent_entry") or {}).get("id") or e["id"]
    ls = last_seen.get(aid) if aid else None
    online = (bool(ls and (now - ls) <= 20)) if aid else None
    if aid is not None and not online:
        return {**base, "online": False, "ok": False, "error": "offline", "posture": None, "flags": {}}
    pe = {**e, "requires_sudo_password": False}
    if e.get("agent_entry"):
        pe["agent_entry"] = {**e["agent_entry"], "requires_sudo_password": False}
    r = _dispatch_one(pe, cmd, "command", None, None, None)  # no token: read-only, unlogged
    p = _parse_posture((r.get("stdout") or "") + "\n" + (r.get("stderr") or ""))
    return {
        **base,
        "online": True if p else online,
        "ok": bool(r.get("ok")) and p is not None,
        "error": None if p else (r.get("error") or "no posture returned"),
        "posture": p,
        "flags": _posture_flags(p),
    }


# Small in-process cache so the dashboard doesn't force a full re-sweep on every
# load. {ts, hosts}. A sweep is on-demand: the cache is served unless a caller
# passes ?refresh=1 or it is older than _POSTURE_TTL.
_POSTURE_CACHE = {"ts": 0.0, "hosts": None}
_POSTURE_TTL = 300.0


@app.get("/api/fleet-posture")
def fleet_posture(refresh: int = 0, user: str = Depends(require_login)):
    """On-demand posture sweep across the fleet for the dashboard Compliance
    strip. Cached for a few minutes unless ?refresh=1. Read-only; tokenless
    dispatch, so visible to the auditor role and never attributed as an action."""
    import concurrent.futures
    import time as _t
    if not refresh and _POSTURE_CACHE["hosts"] is not None and (_t.time() - _POSTURE_CACHE["ts"]) < _POSTURE_TTL:
        return {"hosts": _POSTURE_CACHE["hosts"], "cached": True, "ts": _POSTURE_CACHE["ts"]}
    try:
        entries = dispatch.list_merged_hosts(agent_only=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    try:
        last_seen = {a.get("host_id"): a.get("last_seen") for a in api.get_agents()}
    except Exception:
        last_seen = {}
    now = _t.time()
    cmd = _posture_command()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, max(1, len(entries)))) as ex:
        hosts = list(ex.map(lambda e: _probe_posture(e, cmd, last_seen, now), entries)) if entries else []
    _POSTURE_CACHE["hosts"] = hosts
    _POSTURE_CACHE["ts"] = now
    return {"hosts": hosts, "cached": False, "ts": now}


@app.get("/api/host-posture/{host_id}")
def host_posture(host_id: str, user: str = Depends(require_login)):
    """Full read-only posture for a single host (the drill-down's Refresh).
    Always re-gathers (no cache) so the operator sees current state."""
    import time as _t
    try:
        entries = dispatch.list_merged_hosts(agent_only=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    entry = next((e for e in entries if str(e.get("id")) == str(host_id)), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        last_seen = {a.get("host_id"): a.get("last_seen") for a in api.get_agents()}
    except Exception:
        last_seen = {}
    return _probe_posture(entry, _posture_command(), last_seen, _t.time())


@app.get("/api/environments")
def environments(user: str = Depends(require_login)):
    try:
        return {"environments": api.list_environments()}
    except Exception:
        return {"environments": []}


class EnvironmentCreate(BaseModel):
    name: str


@app.post("/api/environments")
def create_environment(body: EnvironmentCreate, request: Request, user: str = Depends(require_login)):
    name = (body.name or "").strip()
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
    # Optional sudo password to use for THIS run on password-sudo hosts, for
    # operators who don't keep one stored. Transient: used only to elevate via
    # the agent's `sudo -S` (stdin), never persisted, logged, or echoed back.
    sudo_password: str = ""


@app.post("/api/fleet")
def fleet(body: FleetRequest, request: Request, user: str = Depends(require_operator)):
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

    desc = {"reboot": "Reboot host", "poweroff": "Power off host",
            "restart_agent": "Restart agent", "script": "Ran fleet script"}.get(body.action, body.action)
    token = _session_token(request)
    targets = body.targets or list(all_entries.keys())
    results = []
    for tid in targets:
        entry = all_entries.get(tid)
        if entry is None:
            results.append({"host": tid, "ok": False, "error": "host not found",
                            "stdout": "", "stderr": "", "code": None})
            continue
        # An inline password supplied for this run wins; otherwise fall back to
        # this admin's stored password (host scope over fleet default).
        become = body.sudo_password or sudo_store.resolve(user, entry.get("label", ""))
        results.append(_dispatch_one(entry, command, "command", become, token, desc))
    return {"action": body.action, "command": command, "results": results}


@app.post("/api/checkin")
def checkin(user: str = Depends(require_operator)):
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
def enroll_ssh(body: EnrollRequest, request: Request, user: str = Depends(require_operator)):
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
    """Disenroll an agent host. Matches the desktop Host Enrollment "Remove"
    flow (client/host_enrollment_page.py): first ask the host's agent to tear
    down its own systemd service + files (cmd_uninstall_agent_service), then
    drop the enrollment on the controller *regardless* of whether that teardown
    succeeded. Without the teardown step the web console used to leave the agent
    running and heartbeating with no controller record behind it.

    Like the desktop, if the host is flagged password-sudo but the admin has no
    stored sudo password, abort before disenrolling (there's nothing to elevate
    the teardown with) and tell them how to proceed - rather than silently
    dropping the record and orphaning a still-running service."""
    token = _session_token(request)

    # Find this host's agent dispatch entry so we can run the teardown on it.
    # agent_only=True: the teardown is an agent-service action; an SSH-only
    # "host" has no Sysible agent to uninstall.
    try:
        entry = next(
            (e for e in dispatch.list_merged_hosts(agent_only=True)
             if e.get("id") == host_id
             or (e.get("agent_entry") or {}).get("id") == host_id),
            None,
        )
    except Exception:
        entry = None

    teardown = None
    if entry is not None:
        agent_entry = entry.get("agent_entry") or entry
        if agent_entry.get("requires_sudo_password") and not sudo_store.resolve(
                user, entry.get("label", "")):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{entry.get('label', host_id)}' is set to require a sudo password, "
                    "so tearing down its agent service needs your stored sudo password - "
                    "but none is saved. Set it from the \"Sudo Password\" button in the "
                    "header and disenroll again, or run disenroll_agent.sh on the host "
                    "directly."
                ),
            )
        become = sudo_store.resolve(user, entry.get("label", ""))
        # Best-effort: a failed/timed-out teardown (e.g. the host is offline)
        # must not block removing the enrollment record - mirror the desktop's
        # "removed regardless" behaviour. Surface the outcome to the caller.
        teardown = _dispatch_one(
            agent_entry, api.cmd_uninstall_agent_service(), "command",
            become, token, "Uninstall agent service (disenroll)")

    result = _wrap(lambda: _as_admin(
        request, lambda: api.disenroll_agent(host_id) or {"removed": True}))
    if isinstance(result, dict):
        result["teardown"] = teardown
    return result


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
    return _with_token(_session_token(request), fn)


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
def list_admins(request: Request, user: str = Depends(require_login)):
    # Superuser-gated on the controller — pass the admin token so a sysadmin
    # can't read the administrator roster.
    return _wrap(lambda: _as_admin(request, lambda: {"administrators": api.list_administrators()}))


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


class AdminPasswordReset(BaseModel):
    new_password: str


@app.post("/api/admins/{username}/password")
def reset_admin_password(username: str, body: AdminPasswordReset, request: Request,
                         user: str = Depends(require_login)):
    return _wrap(lambda: _as_admin(request, lambda: api.reset_administrator_password(
        username, body.new_password, actor=user)))


class AdminSudoConnect(BaseModel):
    allowed: bool


@app.post("/api/admins/{username}/sudo-connect")
def set_admin_sudo_connect(username: str, body: AdminSudoConnect, request: Request,
                           user: str = Depends(require_login)):
    # Superuser-gated on the controller (via the admin token in _as_admin):
    # grant/revoke the account's Sysible Connect "Send sudo password" button.
    return _wrap(lambda: _as_admin(request, lambda: api.set_administrator_sudo_connect(
        username, body.allowed, actor=user)))


class AdminRole(BaseModel):
    role: str


@app.post("/api/admins/{username}/role")
def set_admin_role(username: str, body: AdminRole, request: Request,
                   user: str = Depends(require_login)):
    # Superuser-gated on the controller (via the admin token in _as_admin):
    # promote/demote the account's role. The controller refuses to demote the
    # last superuser and enforces seat caps.
    return _wrap(lambda: _as_admin(request, lambda: api.set_administrator_role(
        username, body.role, actor=user)))


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
def audit_log(limit: int = 200, request: Request = None, user: str = Depends(require_login)):
    # Superuser-gated on the controller — login attempts + admin changes.
    return _wrap(lambda: _as_admin(request, lambda: {"audit": api.get_admin_audit_log(limit=limit)}))


@app.get("/api/license")
def license_config(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_license_config())


class ChangeCreds(BaseModel):
    current_password: str
    new_username: str = ""
    new_password: str = ""


@app.post("/api/admin/change-credentials")
def change_credentials(body: ChangeCreds, request: Request, user: str = Depends(require_login)):
    new_user = (body.new_username or "").strip() or user
    return _wrap(lambda: _as_admin(request, lambda: api.change_admin_credentials(
        user, body.current_password, new_user, body.new_password)))


@app.get("/api/local-ips")
def local_ips(user: str = Depends(require_login)):
    return _wrap(lambda: {"ips": api.get_local_ips()})


@app.get("/api/tls-info")
def tls_info(user: str = Depends(require_login)):
    return _wrap(lambda: api.get_tls_info())


@app.get("/api/trust-certificate")
def trust_certificate(user: str = Depends(require_login)):
    tmpdir = Path(tempfile.mkdtemp(prefix="sysible-trust-"))
    dest = tmpdir / "sysible-trust.crt"
    try:
        api.download_trust_certificate(str(dest))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch trust cert: {e}")

    def _cleanup():
        try:
            dest.unlink(missing_ok=True); tmpdir.rmdir()
        except Exception:
            pass
    return FileResponse(str(dest), filename="sysible-trust.crt",
                        media_type="application/x-x509-ca-cert", background=BackgroundTask(_cleanup))


@app.post("/api/tls-certificate")
async def install_certificate(request: Request, cert: UploadFile = File(...),
                              key: UploadFile = File(...), chain: UploadFile = File(None),
                              user: str = Depends(require_superuser_session)):
    tmp = Path(tempfile.mkdtemp(prefix="sysible-cert-"))
    paths = {}
    try:
        for name, up in (("cert", cert), ("key", key), ("chain", chain)):
            if up is not None:
                p = tmp / (up.filename or f"{name}.pem")
                p.write_bytes(await up.read())
                paths[name] = str(p)
        return _wrap(lambda: _as_admin(request, lambda: api.install_tls_certificate(
            paths["cert"], paths["key"], paths.get("chain"))))
    finally:
        for p in tmp.glob("*"):
            try: p.unlink()
            except Exception: pass
        try: tmp.rmdir()
        except Exception: pass


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
def users_sync(body: UserSyncRequest, user: str = Depends(require_operator)):
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
# Service Management — live installed/running services on one host
# ----------------------------------------------------------------------
class ServicesListRequest(BaseModel):
    host_id: str
    running: bool = False


@app.post("/api/services/list")
def services_list(body: ServicesListRequest, request: Request, user: str = Depends(require_operator)):
    try:
        entries = {e["id"]: e for e in dispatch.list_merged_hosts(agent_only=False)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    entry = entries.get(body.host_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="host not found")
    spec = actions.get("svc_list_running" if body.running else "svc_list")
    cmd = spec.build({})
    r = _dispatch_one(entry, cmd, "command", None, _session_token(request))
    if r.get("error") and not r.get("stdout"):
        raise HTTPException(status_code=502, detail=r["error"])
    names, seen = [], set()
    for ln in (r.get("stdout") or "").splitlines():
        ln = ln.strip()
        if not ln or ln.lower().startswith("systemctl not"):
            continue
        name = ln.split()[0]
        if name and name not in seen:
            seen.add(name); names.append(name)
    return {"host": entry["label"], "services": names}


# ----------------------------------------------------------------------
# Host Software — live installed-packages list + upload-and-install
# ----------------------------------------------------------------------
class PkgListRequest(BaseModel):
    host_id: str


@app.post("/api/packages/list")
def packages_list(body: PkgListRequest, request: Request, user: str = Depends(require_operator)):
    try:
        entries = {e["id"]: e for e in dispatch.list_merged_hosts(agent_only=False)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    entry = entries.get(body.host_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="host not found")
    from client import _api_automation
    cmd = _api_automation.cmd_list_installed_packages()
    r = _dispatch_one(entry, cmd, "command", None, _session_token(request))
    if r.get("error") and not r.get("stdout"):
        raise HTTPException(status_code=502, detail=r["error"])
    pkgs, seen = [], set()
    for ln in (r.get("stdout") or "").splitlines():
        ln = ln.strip()
        if not ln or "Neither dpkg" in ln:
            continue
        if ln not in seen:
            seen.add(ln); pkgs.append(ln)
    return {"host": entry["label"], "packages": pkgs}


@app.post("/api/packages/install-local")
async def packages_install_local(request: Request, file: UploadFile = File(...),
                                 targets: str = Form(""), user: str = Depends(require_operator)):
    import json as _json
    tids = _json.loads(targets) if targets else []
    if not tids:
        raise HTTPException(status_code=400, detail="No target hosts selected.")
    tmp = Path(tempfile.mkdtemp(prefix="sysible-pkg-"))
    fname = file.filename or "package.bin"
    local = tmp / fname
    remote = "/tmp/" + fname
    local.write_bytes(await file.read())
    spec = actions.get("pkg_install_local")
    cmd = spec.build({"remote_path": remote})
    token = _session_token(request)
    try:
        entries = {e["id"]: e for e in dispatch.list_merged_hosts(agent_only=False)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Controller unreachable: {e}")
    results = []
    for tid in tids:
        entry = entries.get(tid)
        if entry is None:
            results.append({"host": tid, "ok": False, "error": "host not found",
                            "stdout": "", "stderr": "", "code": None})
            continue
        try:
            await asyncio.to_thread(api.upload_file_ssh, tid, str(local), remote)
        except Exception as e:
            results.append({"host": entry["label"], "ok": False, "error": f"upload failed: {e}",
                            "stdout": "", "stderr": "", "code": None})
            continue
        become = sudo_store.resolve(user, entry.get("label", ""))
        results.append(_dispatch_one(entry, cmd, "command", become, token, f"Install local package {fname}"))
    try:
        local.unlink(missing_ok=True); tmp.rmdir()
    except Exception:
        pass
    return {"results": results}


# ----------------------------------------------------------------------
# Webserver Portal
# ----------------------------------------------------------------------
@app.get("/api/portal/status")
def portal_status(user: str = Depends(require_superuser_session)):
    return _wrap(lambda: api.get_portal_status())


@app.post("/api/portal/start")
def portal_start(request: Request, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: _as_admin(request, lambda: api.start_portal()))


@app.post("/api/portal/stop")
def portal_stop(request: Request, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: _as_admin(request, lambda: api.stop_portal()))


class PortalPort(BaseModel):
    port: int


@app.post("/api/portal/config")
def portal_cfg(body: PortalPort, request: Request, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_portal_port(body.port)))


class PortalCreds(BaseModel):
    username: str
    password: str
    current_password: str = ""


@app.post("/api/portal/credentials")
def portal_creds(body: PortalCreds, request: Request, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: _as_admin(request, lambda: api.set_portal_credentials(
        body.username, body.password, body.current_password)))


class PortalRemoveCreds(BaseModel):
    current_password: str = ""


@app.delete("/api/portal/credentials")
def portal_remove_creds(body: PortalRemoveCreds, request: Request, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: _as_admin(request, lambda: api.remove_portal_credentials(body.current_password)))


@app.get("/api/portal/login-history")
def portal_login_history(limit: int = 200, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: {"history": api.get_portal_login_history(limit)})


@app.get("/api/portal/sessions")
def portal_sessions(user: str = Depends(require_superuser_session)):
    return _wrap(lambda: {"sessions": api.get_portal_sessions()})


@app.post("/api/portal/sessions/{session_id}/revoke")
def portal_revoke_session(session_id: int, request: Request, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: _as_admin(request, lambda: api.revoke_portal_session(session_id)))


# Portal file management: files host operators uploaded, and files staged for
# them to download.
@app.get("/api/portal/uploads")
def portal_uploads(user: str = Depends(require_superuser_session)):
    return _wrap(lambda: {"files": api.list_portal_uploads()})


@app.get("/api/portal/uploads/{filename}")
def portal_upload_download(filename: str, user: str = Depends(require_superuser_session)):
    tmp = Path(tempfile.mkdtemp(prefix="sysible-pu-"))
    # Never trust the routed value for a filesystem write — strip any path
    # components so a crafted name can't escape the temp dir.
    dest = tmp / Path(filename).name
    try:
        api.download_portal_upload(filename, str(dest))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    def _cleanup():
        try:
            dest.unlink(missing_ok=True); tmp.rmdir()
        except Exception:
            pass
    return FileResponse(str(dest), filename=filename, media_type="application/octet-stream",
                        background=BackgroundTask(_cleanup))


@app.delete("/api/portal/uploads/{filename}")
def portal_upload_delete(filename: str, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: api.delete_portal_upload(filename) or {"deleted": True})


@app.get("/api/portal/downloads")
def portal_downloads(user: str = Depends(require_superuser_session)):
    return _wrap(lambda: {"files": api.list_portal_downloads()})


@app.post("/api/portal/downloads")
async def portal_download_stage(file: UploadFile = File(...), user: str = Depends(require_superuser_session)):
    tmp = Path(tempfile.mkdtemp(prefix="sysible-pd-"))
    p = tmp / (file.filename or "file.bin")
    try:
        p.write_bytes(await file.read())
        return _wrap(lambda: api.stage_portal_download(str(p)) or {"staged": True})
    finally:
        try:
            p.unlink(missing_ok=True); tmp.rmdir()
        except Exception:
            pass


@app.delete("/api/portal/downloads/{filename}")
def portal_download_delete(filename: str, user: str = Depends(require_superuser_session)):
    return _wrap(lambda: api.delete_portal_download(filename) or {"deleted": True})


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
def run_tool(action_name: str, body: RunRequest, request: Request, user: str = Depends(require_operator)):
    """Build the shell command for `action_name` via the desktop client's
    cmd_* builder, then dispatch it across every selected target and
    return per-host results. Synchronous: agent tasks are polled here up
    to a timeout so the browser gets one clean response."""
    spec = actions.get(action_name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown action: {action_name}")

    # The system-critical override (allow_critical) may only ever be honoured for
    # a superuser. Strip it for anyone else so a sysadmin can't bypass the
    # builder's critical-path block by crafting the request; the builder then
    # refuses a critical path with a clear error.
    if body.params.get("allow_critical") and request.session.get("role") != "superuser":
        body.params = {**body.params, "allow_critical": False}

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

    # Human description for the activity feed (else the controller logs a
    # generic "ran a script"). Use the action label + a representative param.
    desc = spec.label
    for p in spec.params:
        v = body.params.get(p.name)
        if v and getattr(p, "type", "text") in ("text", "select", "number"):
            desc = f"{spec.label}: {v}"
            break

    token = _session_token(request)
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
        results.append(_dispatch_one(entry, command, spec.kind, become, token, desc))

    return {"action": action_name, "command": command, "results": results}


def _dispatch_one(entry, command, kind, become_password=None, token=None, description=None):
    """Run one command on one host and return a normalized result,
    polling agent tasks to completion (bounded) so the response is
    synchronous from the browser's point of view.

    The dispatch itself runs with the admin token set (token!=None) so the
    controller can derive the run-as user (runuser -u <admin>) and attribute
    the activity feed; `description` is the human label recorded in that feed.
    The subsequent agent-result polling does not need the token, so it isn't
    held during the (bounded) poll loop."""
    label = entry["label"]
    try:
        outcome = _with_token(token, lambda: dispatch.run_on_entry(
            entry, command, kind=kind, become_password=become_password, description=description))
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
    user: str = Depends(require_operator),
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
async def files_download(host: str, path: str, user: str = Depends(require_operator)):
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
    # Defense-in-depth against cross-site websocket hijacking: reject a
    # handshake whose Origin isn't our own host. (The SameSite=Strict session
    # cookie already prevents a cross-site handshake from carrying the login,
    # but an explicit Origin check is the canonical CSWSH control.)
    origin = ws.headers.get("origin")
    if origin:
        host = ws.headers.get("host", "")
        try:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != host:
                await ws.close(code=1008)
                return
        except Exception:
            await ws.close(code=1008)
            return
    # Auth: SessionMiddleware populates the websocket scope's session the
    # same way it does for HTTP, so the login cookie gates this too.
    _sess = ws.scope.get("session", {}) or {}
    if not _sess.get("user"):
        await ws.close(code=1008)
        return
    # Read-only 'auditor' accounts cannot open an interactive terminal.
    if _sess.get("role") == "auditor":
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
        # Open the terminal WITH the admin token set, so the controller runs
        # the shell as the admin's user (runuser -u <admin>) instead of the
        # raw SSH login (root). The token is only needed at open time.
        ws_token = _token_from_session(ws.scope.get("session"))
        try:
            opened = await asyncio.to_thread(lambda: _with_token(ws_token, lambda: api.open_terminal(host)))
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

        ws_user = (ws.scope.get("session") or {}).get("user")
        ws_label = first.get("label") or host

        async def pump_input():
            """Keystrokes / resize / send-sudo-password from the browser -> host."""
            from webgui import sudo_store
            while True:
                msg = await ws.receive_json()
                t = msg.get("t")
                if t == "i":
                    await asyncio.to_thread(api.write_terminal, session_id, msg.get("d", ""))
                elif t == "r" and msg.get("cols") and msg.get("rows"):
                    await asyncio.to_thread(api.resize_terminal, session_id,
                                            int(msg["cols"]), int(msg["rows"]))
                elif t == "sudo":
                    # Opt-in, granted per-account by a superuser. Enforce it
                    # server-side (the button is also hidden client-side): never
                    # inject the password for an account that hasn't been granted.
                    if not (ws.scope.get("session") or {}).get("sudo_connect"):
                        await ws.send_json({"t": "o", "d":
                            "\r\n[sudo on Connect is not enabled for your account — ask a superuser to grant it in Settings → Administrators]\r\n"})
                        continue
                    # Inject the operator's stored sudo password (host scope wins
                    # over fleet default) + Enter — for an interactive sudo prompt.
                    pw = sudo_store.resolve(ws_user, ws_label)
                    if pw:
                        await asyncio.to_thread(api.write_terminal, session_id, pw + "\n")
                    else:
                        await ws.send_json({"t": "o", "d":
                            "\r\n[no sudo password stored — set it via 'Sudo Password' in the header]\r\n"})

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
        # Serve real root-level static files from dist (the logo, favicon, etc.)
        # that aren't under /assets — otherwise the catch-all would hand back
        # index.html and the <img> would 404. Resolve and confine to dist so a
        # crafted path can't escape it.
        if full_path:
            candidate = (_FRONTEND_DIST / full_path).resolve()
            try:
                candidate.relative_to(_FRONTEND_DIST.resolve())
                if candidate.is_file():
                    return FileResponse(candidate)
            except ValueError:
                pass
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
