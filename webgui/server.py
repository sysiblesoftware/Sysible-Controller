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
import os
import secrets
import sys
from pathlib import Path

# Make the repo root importable so `import client.*` works whether this
# service is launched from webgui/ or the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from client import api
from client import _api_dispatch as dispatch
from webgui import actions


app = FastAPI(title="Sysible Web GUI")

# Signed http-only session cookie. Set SYSIBLE_WEBGUI_SECRET in the
# environment for a stable secret across restarts; a random per-process
# secret (the fallback) logs everyone out whenever the service restarts.
_SECRET = os.getenv("SYSIBLE_WEBGUI_SECRET") or secrets.token_hex(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SECRET,
    session_cookie="sysible_web",
    https_only=os.getenv("SYSIBLE_WEBGUI_HTTPS_ONLY", "0") == "1",
    same_site="lax",
)

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


@app.post("/api/login")
def login(body: LoginRequest, request: Request):
    """Verify credentials against the controller (client.api holds the
    API key) and, on success, store the username in the signed session
    cookie. Mirrors the desktop admin login exactly."""
    try:
        result = api.admin_login(body.username.strip(), body.password)
    except Exception:
        # client.api raises on a 401 from the controller.
        raise HTTPException(status_code=401, detail="Invalid username or password")

    request.session["user"] = body.username.strip()
    return {
        "username": body.username.strip(),
        "must_change_password": bool(result.get("must_change_password")),
    }


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"status": "ok"}


@app.get("/api/me")
def me(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user}


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
        results.append(_dispatch_one(entry, command, spec.kind))

    return {"action": action_name, "command": command, "results": results}


def _dispatch_one(entry, command, kind):
    """Run one command on one host and return a normalized result,
    polling agent tasks to completion (bounded) so the response is
    synchronous from the browser's point of view."""
    label = entry["label"]
    try:
        outcome = dispatch.run_on_entry(entry, command, kind=kind)
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
