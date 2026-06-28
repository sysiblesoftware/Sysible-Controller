"""
Start/stop/status/diagnostics control for the Sysible Web GUI.

The Web GUI (webgui/) is a separate process on its own port, managed the
same way the Webserver Portal is (see backend/portal_manager.py): a
PID-file under run/webgui.pid, the actually-bound port in a sidecar
run/webgui.port, and logs in logs/webgui.log. This lets the desktop
dashboard's "Browser Access" tile start it, stop it, see whether it's
healthy, and diagnose why it won't start - without the admin needing a
shell on the controller.

Like the portal, it's served over HTTPS using the same self-signed cert
the controller uses, so the admin password the browser sends at login
isn't on the wire in the clear on a LAN. The signing secret for the
browser session cookie is generated once and persisted under
run/webgui.secret so restarts don't log everyone out.
"""
import os
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_WEBGUI_PORT = int(os.getenv("SYSIBLE_WEBGUI_PORT", "8800"))

RUN_DIR = Path(os.getenv("SYSIBLE_RUN_DIR", str(PROJECT_ROOT / "run")))
WEBGUI_PID_FILE = RUN_DIR / "webgui.pid"
WEBGUI_PORT_FILE = RUN_DIR / "webgui.port"
WEBGUI_SECRET_FILE = RUN_DIR / "webgui.secret"

LOG_DIR = Path(os.getenv("SYSIBLE_LOG_DIR", str(PROJECT_ROOT / "logs")))
WEBGUI_LOG_FILE = LOG_DIR / "webgui.log"

CERT_FILE = Path(os.getenv("SYSIBLE_CERT_FILE", str(PROJECT_ROOT / "certs" / "server.crt")))
KEY_FILE = Path(os.getenv("SYSIBLE_KEY_FILE", str(PROJECT_ROOT / "certs" / "server.key")))

FRONTEND_DIST = PROJECT_ROOT / "webgui" / "frontend" / "dist"

STARTUP_TIMEOUT_S = 8
STARTUP_POLL_INTERVAL_S = 0.2


def _read_pid():
    if not WEBGUI_PID_FILE.exists():
        return None
    try:
        return int(WEBGUI_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _read_running_port():
    if not WEBGUI_PORT_FILE.exists():
        return None
    try:
        return int(WEBGUI_PORT_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _log_tail(n=25):
    try:
        return "\n".join(WEBGUI_LOG_FILE.read_text().splitlines()[-n:])
    except OSError:
        return ""


def _get_or_create_secret():
    """Persist the cookie-signing secret so restarting the service doesn't
    invalidate everyone's session."""
    try:
        if WEBGUI_SECRET_FILE.exists():
            s = WEBGUI_SECRET_FILE.read_text().strip()
            if s:
                return s
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        WEBGUI_SECRET_FILE.write_text(secret + "\n")
        os.chmod(WEBGUI_SECRET_FILE, 0o600)
    except OSError:
        pass
    return secret


def _tls_available():
    return CERT_FILE.exists() and KEY_FILE.exists()


def _scheme():
    return "https" if _tls_available() else "http"


def _wait_for_health(port, deadline):
    scheme = _scheme()
    url = f"{scheme}://127.0.0.1:{port}/api/health"
    ctx = None
    if scheme == "https":
        ctx = ssl.create_default_context(cafile=str(CERT_FILE))
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1, context=ctx) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(STARTUP_POLL_INTERVAL_S)
    return False


def diagnostics():
    """Everything the dashboard needs to tell the admin *why* the Web GUI
    is or isn't usable, checked cheaply and without starting anything."""
    checks = []

    built = FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists()
    checks.append({
        "name": "Front end built",
        "ok": built,
        "detail": str(FRONTEND_DIST) if built else
                  "Not built - run: cd webgui/frontend && npm install && npm run build",
    })

    try:
        import importlib.util
        deps_ok = all(importlib.util.find_spec(m) is not None
                      for m in ("fastapi", "uvicorn", "itsdangerous", "multipart"))
        deps_detail = "fastapi, uvicorn, itsdangerous, python-multipart present" if deps_ok \
            else "Missing one of: fastapi, uvicorn, itsdangerous, python-multipart"
    except Exception as e:
        deps_ok, deps_detail = False, str(e)
    checks.append({"name": "Python dependencies", "ok": deps_ok, "detail": deps_detail})

    server_ok = (PROJECT_ROOT / "webgui" / "server.py").exists()
    checks.append({"name": "Service code present", "ok": server_ok,
                   "detail": "webgui/server.py" if server_ok else "webgui/server.py missing"})

    checks.append({
        "name": "TLS certificate",
        "ok": _tls_available(),
        "detail": f"Serving HTTPS using {CERT_FILE}" if _tls_available() else
                  "No controller cert found - would serve plain HTTP (run install_sysible.sh)",
    })

    return {"checks": checks, "all_ok": all(c["ok"] for c in checks if c["name"] != "TLS certificate")}


def install_dependencies():
    """Install/build everything the Web GUI needs, from the dashboard, for
    admins who didn't (or couldn't) run install_sysible.sh: the Python
    service deps into this interpreter's environment, and the React front
    end via npm. Each step's combined output is returned so the GUI can
    show exactly what happened (and any failure). Idempotent - safe to
    re-run; pip is a no-op when satisfied and the npm build just rebuilds."""
    steps = []

    # 1) Python dependencies (webgui/requirements.txt) into this venv.
    req = PROJECT_ROOT / "webgui" / "requirements.txt"
    if req.exists():
        steps.append(_run_step(
            "Install Python dependencies (pip)",
            [sys.executable, "-m", "pip", "install", "-r", str(req)],
            cwd=PROJECT_ROOT, timeout=300,
        ))
    else:
        steps.append({"name": "Install Python dependencies (pip)", "ok": False,
                      "output": f"{req} not found"})

    # 2) Front end (npm install + build), if Node is available.
    frontend = PROJECT_ROOT / "webgui" / "frontend"
    if frontend.exists():
        npm = shutil.which("npm")
        if not npm:
            steps.append({
                "name": "Build front end (npm)", "ok": False,
                "output": ("Node.js / npm is not installed on the controller. Install "
                           "Node.js 18+ (e.g. your distro's nodejs package), then run "
                           "this again."),
            })
        else:
            inst = _run_step("npm install", [npm, "install", "--no-audit", "--no-fund"],
                             cwd=frontend, timeout=600)
            steps.append(inst)
            if inst["ok"]:
                steps.append(_run_step("npm run build", [npm, "run", "build"],
                                       cwd=frontend, timeout=600))

    return {"ok": all(s["ok"] for s in steps), "steps": steps,
            "diagnostics": diagnostics()}


def _run_step(name, cmd, cwd, timeout):
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        # Keep the tail - npm output can be huge.
        out = "\n".join(out.splitlines()[-40:])
        return {"name": name, "ok": proc.returncode == 0, "output": out.strip()}
    except subprocess.TimeoutExpired:
        return {"name": name, "ok": False, "output": f"Timed out after {timeout}s."}
    except Exception as e:
        return {"name": name, "ok": False, "output": str(e)}


def status():
    pid = _read_pid()
    running = _is_alive(pid)
    if not running:
        WEBGUI_PID_FILE.unlink(missing_ok=True)
        WEBGUI_PORT_FILE.unlink(missing_ok=True)
        pid = None
    return {
        "running": running,
        "port": _read_running_port() if running else None,
        "configured_port": DEFAULT_WEBGUI_PORT,
        "scheme": _scheme(),
        "pid": pid if running else None,
    }


def start(port=None):
    current = status()
    if current["running"]:
        return current

    BLOCKER_NAMES = ("Front end built", "Python dependencies", "Service code present")

    def _blockers():
        return [c for c in diagnostics()["checks"]
                if not c["ok"] and c["name"] in BLOCKER_NAMES]

    blockers = _blockers()
    install_log = None
    if blockers:
        # Self-heal: build the front end / install the deps automatically so
        # the admin only ever has to click Start. "Service code present" is
        # the one thing we can't fix (the files just aren't there), so don't
        # bother trying to install in that case.
        if any(b["name"] == "Service code present" for b in blockers):
            return {
                "running": False, "port": None, "configured_port": DEFAULT_WEBGUI_PORT,
                "scheme": _scheme(), "pid": None,
                "error": "Can't start the Web GUI - service code is missing:\n- " +
                         "\n- ".join(f"{c['name']}: {c['detail']}" for c in blockers),
            }
        install = install_dependencies()
        install_log = install.get("steps")
        blockers = _blockers()
        if blockers:
            detail = "\n- ".join(f"{c['name']}: {c['detail']}" for c in blockers)
            failed = [s for s in (install_log or []) if not s["ok"]]
            extra = ""
            if failed:
                extra = "\n\nSetup output:\n" + "\n".join(
                    f"[{s['name']}]\n{s['output']}" for s in failed)
            return {
                "running": False, "port": None, "configured_port": DEFAULT_WEBGUI_PORT,
                "scheme": _scheme(), "pid": None,
                "error": "Couldn't prepare the Web GUI automatically:\n- " + detail + extra,
            }

    port = port or DEFAULT_WEBGUI_PORT
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["SYSIBLE_WEBGUI_SECRET"] = _get_or_create_secret()
    cmd = [sys.executable, "-m", "uvicorn", "webgui.server:app",
           "--host", "0.0.0.0", "--port", str(port), "--log-level", "info"]
    if _tls_available():
        env["SYSIBLE_WEBGUI_HTTPS_ONLY"] = "1"
        cmd += ["--ssl-keyfile", str(KEY_FILE), "--ssl-certfile", str(CERT_FILE)]

    log_fh = open(WEBGUI_LOG_FILE, "a")
    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT_ROOT), env=env,
        stdout=log_fh, stderr=log_fh, start_new_session=True,
    )
    WEBGUI_PID_FILE.write_text(str(proc.pid))
    WEBGUI_PORT_FILE.write_text(str(port))

    if _wait_for_health(port, time.time() + STARTUP_TIMEOUT_S):
        return status()

    if proc.poll() is not None:
        code = proc.poll()
        WEBGUI_PID_FILE.unlink(missing_ok=True)
        WEBGUI_PORT_FILE.unlink(missing_ok=True)
        return {
            "running": False, "port": None, "configured_port": DEFAULT_WEBGUI_PORT,
            "scheme": _scheme(), "pid": None,
            "error": f"Web GUI process exited immediately (code {code}). Last log lines:\n{_log_tail()}",
        }
    # Alive but slow to answer - don't kill a slow first import.
    return status()


def stop():
    pid = _read_pid()
    if not _is_alive(pid):
        WEBGUI_PID_FILE.unlink(missing_ok=True)
        WEBGUI_PORT_FILE.unlink(missing_ok=True)
        return status()
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + 4
    while time.time() < deadline and _is_alive(pid):
        time.sleep(0.15)
    # If it ignored SIGTERM, escalate to SIGKILL so we don't leave an
    # orphaned uvicorn holding the port (and then report a stale pidfile as
    # "stopped" while it's actually still serving).
    if _is_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        hard = time.time() + 2
        while time.time() < hard and _is_alive(pid):
            time.sleep(0.1)
    WEBGUI_PID_FILE.unlink(missing_ok=True)
    WEBGUI_PORT_FILE.unlink(missing_ok=True)
    return status()
